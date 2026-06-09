#!/usr/bin/env python3
"""
mcp-ssh-winrm — MCP server for Windows administration from Linux.

Architecture:
  Linux (Claude Code) → SSH → Windows Management Server → PSRemoting → Target hosts

Tools:
  run_powershell(script, computer?)  — run PS on mgmt server or via PSRemoting on target
  check_winrm(computer)              — test WinRM connectivity (TCP 5985)
"""

import asyncio
import configparser
import os
import secrets
import tempfile
from pathlib import Path

import asyncssh
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_LOCATIONS = [
    os.environ.get("WINRM_MCP_CONFIG", ""),
    os.path.expanduser("~/.config/mcp-ssh-winrm/config.ini"),
    os.path.join(os.path.dirname(__file__), "..", "..", "config.ini"),
]


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    for path in _CONFIG_LOCATIONS:
        if path and Path(path).exists():
            cfg.read(path)
            return cfg
    raise FileNotFoundError(
        "Config not found. Copy config.example.ini to one of:\n"
        + "\n".join(f"  {p}" for p in _CONFIG_LOCATIONS if p)
    )


# ---------------------------------------------------------------------------
# SSH + PowerShell client
# ---------------------------------------------------------------------------

class WinRMClient:
    def __init__(self, cfg: configparser.ConfigParser):
        ssh = cfg["ssh"]
        self.host = ssh["host"]
        self.port = ssh.getint("port", fallback=22)
        self.user = ssh.get("user", fallback="") or None
        key_path = ssh.get("key", fallback="")
        self.key = os.path.expanduser(key_path) if key_path else None
        self.temp_dir = ssh.get("temp_dir", fallback=r"C:\Temp\mcp-ssh-winrm")

        domain = cfg["domain"] if cfg.has_section("domain") else {}
        self.domain_user = domain.get("user", "")
        pwd_env = domain.get("password_env", "WINRM_MCP_PASSWORD")
        self.domain_password = os.environ.get(pwd_env, "")

    def _ssh_kwargs(self) -> dict:
        kwargs: dict = {
            "host": self.host,
            "port": self.port,
            "known_hosts": None,
            "config": [os.path.expanduser("~/.ssh/config")],
        }
        if self.user:
            kwargs["username"] = self.user
        if self.key:
            kwargs["client_keys"] = [self.key]
        return kwargs

    def _wrap_invoke_command(self, script: str, computer: str) -> str:
        """Wrap script in Invoke-Command for PSRemoting to a remote computer."""
        if not self.domain_user or not self.domain_password:
            raise ValueError(
                "domain.user and WINRM_MCP_PASSWORD env var are required for PSRemoting"
            )
        # Escape single quotes in password
        safe_password = self.domain_password.replace("'", "''")
        safe_user = self.domain_user.replace("'", "''")
        return (
            f"$_pass = ConvertTo-SecureString '{safe_password}' -AsPlainText -Force\n"
            f"$_cred = New-Object System.Management.Automation.PSCredential('{safe_user}', $_pass)\n"
            f"Invoke-Command -ComputerName '{computer}' -Credential $_cred -ScriptBlock {{\n"
            f"{script}\n"
            f"}}"
        )

    async def run_powershell(self, script: str, computer: str | None = None) -> str:
        token = secrets.token_hex(8)
        remote_ps1 = f"{self.temp_dir}\\winrm_{token}.ps1"

        full_script = self._wrap_invoke_command(script, computer) if computer else script

        async with asyncssh.connect(**self._ssh_kwargs()) as conn:
            await conn.run(f'cmd /c mkdir "{self.temp_dir}" 2>nul', check=False)

            async with conn.start_sftp_client() as sftp:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".ps1", delete=False, encoding="utf-8"
                ) as f:
                    f.write(full_script)
                    local_tmp = f.name
                try:
                    # Windows OpenSSH SFTP expects /C:/path/... format
                    drive, rest = remote_ps1[0], remote_ps1[2:].replace("\\", "/")
                    remote_ps1_sftp = f"/{drive}:{rest}"
                    await sftp.put(local_tmp, remote_ps1_sftp)
                finally:
                    os.unlink(local_tmp)

            ps_exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
            result = await conn.run(
                f'{ps_exe} -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{remote_ps1}"',
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")

            await conn.run(f'cmd /c del /f /q "{remote_ps1}" 2>nul', check=False)

        return output.strip()

    async def check_winrm(self, computer: str) -> str:
        script = (
            f"$r = Test-NetConnection -ComputerName '{computer}' -Port 5985"
            f" -WarningAction SilentlyContinue\n"
            f"'WinRM 5985 on ' + '{computer}' + ': ' + $r.TcpTestSucceeded"
        )
        return await self.run_powershell(script)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("mcp-ssh-winrm")
_client: WinRMClient | None = None


def get_client() -> WinRMClient:
    global _client
    if _client is None:
        _client = WinRMClient(load_config())
    return _client


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_powershell",
            description=(
                "Run a PowerShell script on the Windows management server, "
                "or on a remote Windows computer via PSRemoting (WinRM/Invoke-Command). "
                "Returns stdout+stderr combined."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "PowerShell script body to execute.",
                    },
                    "computer": {
                        "type": "string",
                        "description": (
                            "Optional. Target computer hostname or IP. "
                            "Script runs via Invoke-Command (PSRemoting) on this host. "
                            "Omit to run directly on the management server."
                        ),
                    },
                },
                "required": ["script"],
            },
        ),
        Tool(
            name="check_winrm",
            description=(
                "Test WinRM connectivity to a remote Windows computer (TCP port 5985). "
                "Returns True/False."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "computer": {
                        "type": "string",
                        "description": "Hostname or IP of the target computer.",
                    },
                },
                "required": ["computer"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    client = get_client()

    if name == "run_powershell":
        output = await client.run_powershell(
            script=arguments["script"],
            computer=arguments.get("computer"),
        )
        return [TextContent(type="text", text=output or "(no output)")]

    if name == "check_winrm":
        output = await client.check_winrm(arguments["computer"])
        return [TextContent(type="text", text=output or "(no output)")]

    raise ValueError(f"Unknown tool: {name}")


def main() -> None:
    asyncio.run(_run())


async def _run() -> None:
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    main()
