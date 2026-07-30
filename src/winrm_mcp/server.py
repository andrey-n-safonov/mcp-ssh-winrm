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
import re
import secrets
import tempfile
from pathlib import Path

import asyncssh
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

_ANSI_ESCAPE = re.compile(r'\x1b(?:\[[0-9;]*[A-Za-z]|\][^\x07]*\x07|[()][AB012])')

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
        extra = ssh.get("extra_ps_module_paths", "")
        self.extra_ps_module_paths = [p.strip() for p in extra.split(";") if p.strip()]
        self.command_timeout = ssh.getfloat("command_timeout", fallback=300.0)
        self.cleanup_timeout = ssh.getfloat("cleanup_timeout", fallback=15.0)
        self.connect_retries = ssh.getint("connect_retries", fallback=2)
        self.connect_retry_delay = ssh.getfloat("connect_retry_delay", fallback=3.0)

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
            "keepalive_interval": 30,
            "keepalive_count_max": 10,
        }
        if self.user:
            kwargs["username"] = self.user
        if self.key:
            kwargs["client_keys"] = [self.key]
        return kwargs

    def _domain_credentials_header(self) -> str:
        """Prepend $DomainUser/$DomainPassword variables to a single-hop script.

        For operations that write a security descriptor / ACL on an AD or
        GPO object (Set-Acl -Path AD:\\..., dsacls.exe, Set-GPPermission,
        raw DirectoryEntry.ObjectSecurity edits) — Invoke-Command's classic
        "double hop" silently no-ops: the call reports success and an
        immediate readback even shows the change, but repadmin
        /showobjmeta proves the attribute version never actually moved.
        Ordinary attribute writes (e.g. description) are NOT affected —
        this is specific to security-descriptor writes.

        The reliable pattern is a single-hop script (no computer=, so no
        nested Invoke-Command) that does its own LDAP bind with explicit
        credentials via System.DirectoryServices, e.g.:

            $de = New-Object System.DirectoryServices.DirectoryEntry(
                "LDAP://<dc>/<DN>", $DomainUser, $DomainPassword)
            $de.ObjectSecurity.AddAccessRule(...)   # or RemoveAccessRule
            $de.CommitChanges()

        This helper injects those two variables so the caller never has to
        paste the plaintext password into the script text themselves.

        Also verify with a *fresh* DirectoryEntry/repadmin re-read after
        CommitChanges — don't trust the same $de object or Get-Acl -Path
        AD:\\/Get-GPPermission through a *second* Invoke-Command hop, both
        of which have independently shown unreliable (false-negative)
        reads in this environment.
        """
        if not self.domain_user or not self.domain_password:
            raise ValueError(
                "domain.user and WINRM_MCP_PASSWORD env var are required for "
                "with_domain_credentials"
            )
        safe_user = self.domain_user.replace("'", "''")
        safe_password = self.domain_password.replace("'", "''")
        return (
            f"$DomainUser = '{safe_user}'\n"
            f"$DomainPassword = '{safe_password}'\n"
        )

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

    async def _connect(self) -> asyncssh.SSHClientConnection:
        """Connect with retry on transient (connection-establishment) failures only.

        Retrying after a script has started executing is deliberately out of
        scope here — re-running a non-idempotent remote command on a dropped
        mid-execution connection could apply it twice. Only the initial
        handshake is retried.
        """
        last_exc: Exception | None = None
        for attempt in range(self.connect_retries + 1):
            try:
                return await asyncssh.connect(**self._ssh_kwargs())
            except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < self.connect_retries:
                    await asyncio.sleep(self.connect_retry_delay)
        assert last_exc is not None
        raise last_exc

    async def run_powershell(
        self,
        script: str,
        computer: str | None = None,
        with_domain_credentials: bool = False,
    ) -> str:
        if with_domain_credentials and computer:
            raise ValueError(
                "with_domain_credentials is for single-hop scripts only — combining it "
                "with computer= would just recreate the double-hop pattern it exists to "
                "avoid. Omit computer= and do the LDAP bind (or similar) inside the "
                "script itself, targeting whichever server you need by name."
            )

        token = secrets.token_hex(8)
        remote_ps1 = f"{self.temp_dir}\\winrm_{token}.ps1"

        full_script = self._wrap_invoke_command(script, computer) if computer else script
        if with_domain_credentials:
            full_script = self._domain_credentials_header() + full_script
        if self.extra_ps_module_paths:
            prefix = "$env:PSModulePath += ';" + ";".join(self.extra_ps_module_paths) + "'\n"
            full_script = prefix + full_script

        conn = await self._connect()
        try:
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
            try:
                result = await asyncio.wait_for(
                    conn.run(
                        f'{ps_exe} -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{remote_ps1}"',
                        check=False,
                    ),
                    timeout=self.command_timeout,
                )
                output = (result.stdout or "") + (result.stderr or "")
            except asyncio.TimeoutError:
                # The remote powershell.exe may still be running detached on the
                # target after the channel wait is cancelled — we only give up
                # waiting on our end, we don't (can't reliably) kill it remotely.
                # Cleanup below still runs (best-effort) — the script itself
                # contains a plaintext domain password and shouldn't linger.
                output = (
                    f"[ERROR] Command timed out after {self.command_timeout:.0f}s "
                    f"waiting for output from {computer or self.host}. "
                    f"The remote process may still be running detached; it was "
                    f"not cancelled remotely, only the wait on our end gave up."
                )
            finally:
                # Best-effort cleanup with its own short timeout — if the
                # connection is already unresponsive after the main command
                # timed out, don't let cleanup hang the whole call too.
                try:
                    await asyncio.wait_for(
                        conn.run(f'cmd /c del /f /q "{remote_ps1}" 2>nul', check=False),
                        timeout=self.cleanup_timeout,
                    )
                except (asyncssh.Error, OSError, asyncio.TimeoutError):
                    pass
        finally:
            conn.close()
            try:
                await asyncio.wait_for(conn.wait_closed(), timeout=self.cleanup_timeout)
            except asyncio.TimeoutError:
                pass

        return _ANSI_ESCAPE.sub("", output).strip()

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
                "Returns stdout+stderr combined.\n\n"
                "WARNING — double-hop and AD/GPO security-descriptor writes: when "
                "computer= is set, the script runs via Invoke-Command -Credential, "
                "a classic PowerShell 'double hop'. Ordinary attribute writes work "
                "fine this way, but writing a security descriptor / ACL (Set-Acl "
                "-Path AD:\\..., dsacls.exe, Set-GPPermission, GPO Security "
                "Filtering, DirectoryEntry.ObjectSecurity edits) silently no-ops "
                "under double-hop — it reports success, an immediate readback even "
                "looks changed, but nothing actually commits (confirmed via "
                "repadmin /showobjmeta). For that class of operation, use "
                "with_domain_credentials=true instead (omit computer=) and do a "
                "single-hop LDAP bind with explicit credentials inside the script. "
                "Also don't trust Get-Acl -Path AD:\\ or Get-GPPermission read "
                "through a second Invoke-Command hop to verify — both have shown "
                "unreliable/false-negative reads here too; re-read with a fresh "
                "single-hop DirectoryEntry instead."
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
                            "Omit to run directly on the management server. Do not "
                            "combine with with_domain_credentials, and do not use this "
                            "for AD/GPO ACL writes — see the double-hop warning above."
                        ),
                    },
                    "with_domain_credentials": {
                        "type": "boolean",
                        "description": (
                            "Optional, default false. Prepends $DomainUser/"
                            "$DomainPassword variables to the script (from this "
                            "server's configured domain account) instead of you "
                            "having to paste the plaintext password into the script "
                            "yourself. Use for single-hop scripts (no computer=) that "
                            "need explicit AD credentials, e.g. writing a security "
                            "descriptor via System.DirectoryServices.DirectoryEntry — "
                            "see the double-hop warning above for why this is needed "
                            "instead of computer=."
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
            with_domain_credentials=arguments.get("with_domain_credentials", False),
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
