#!/usr/bin/env python3
"""
mcp-ssh-winrm — MCP server for Windows administration from Linux.

Architecture:
  Linux (Claude Code) → one persistent SSH connection → resident PowerShell
  workers on the Windows management server → PSRemoting (cached sessions) → targets

Tools:
  run_powershell(script, computer?/computers?, ...)  — run PS locally on the mgmt
                                                        server or fan out via PSRemoting
  check_winrm(computer/computers, deep?)             — TCP 5985 (+ real WSMan handshake)
  reset_winrm()                                      — drop workers/sessions/SSH, start clean
"""

import asyncio
import configparser
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from .client import HostResult, Settings, WinRMInfraError, WorkerPool

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
# Result formatting
# ---------------------------------------------------------------------------

_STATUS_NOTE = {
    "timeout": "[TIMEOUT] the script did not finish in time and was stopped; output above is partial",
    "cancelled": "[CANCELLED] the script was stopped; output above is partial",
    "connect_failed": "[CONNECT FAILED] could not open a PSRemoting session",
}


def _format_host(r: HostResult) -> str:
    parts: list[str] = []
    if r.stdout:
        parts.append(r.stdout)
    if r.stderr:
        parts.append(f"[stderr]\n{r.stderr}")
    if r.status in _STATUS_NOTE:
        parts.append(_STATUS_NOTE[r.status])
    return "\n".join(parts) if parts else "(no output)"


def format_results(results: list[HostResult]) -> tuple[str, bool]:
    """Return (text, is_infra_failure). Script-level errors are not infra failures."""
    if len(results) == 1 and results[0].computer is None:
        r = results[0]
        return _format_host(r), r.status in ("timeout", "cancelled")
    if len(results) == 1:
        r = results[0]
        return _format_host(r), r.status in ("timeout", "cancelled", "connect_failed")
    blocks = []
    for r in results:
        blocks.append(f"=== {r.computer} [{r.status}, {r.ms / 1000:.1f}s] ===\n{_format_host(r)}")
    failed = all(r.status in ("timeout", "cancelled", "connect_failed") for r in results)
    return "\n\n".join(blocks), failed


def format_tcp(rows: list[dict], deep: bool) -> str:
    lines = []
    for r in rows:
        tcp = "open" if r["tcp"] else f"CLOSED ({r.get('tcp_error') or 'no answer'})"
        line = f"{r['computer']}: 5985 {tcp} ({r['tcp_ms']} ms)"
        if deep and r["tcp"]:
            if r.get("winrm"):
                line += f"; WSMan OK ({r['winrm_ms']} ms)"
            else:
                line += f"; WSMan FAILED: {r.get('winrm_error') or 'unknown'}"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

_NAME = "mcp-ssh-winrm"
_pool: WorkerPool | None = None


def get_pool() -> WorkerPool:
    global _pool
    if _pool is None:
        _pool = WorkerPool(Settings.from_config(load_config()))
    return _pool


def _hosts_arg(arguments: dict) -> list[str]:
    hosts: list[str] = []
    one = arguments.get("computer")
    if one:
        hosts.append(one)
    hosts.extend(arguments.get("computers") or [])
    return hosts


RUN_DESCRIPTION = (
    "Run a PowerShell script on the Windows management server, or on one or "
    "many remote Windows computers via PSRemoting (WinRM). Backed by resident "
    "PowerShell workers behind a persistent SSH connection, with PSRemoting "
    "sessions kept warm between calls — repeat calls to the same host are fast. "
    "Returns stdout, then an [stderr] section if the script wrote errors. With "
    "several computers the script runs on all of them in parallel and the result "
    "is one section per host. A timeout really stops the script (and reports "
    "partial output); it is not just abandoned.\n\n"
    "Notes: each call gets a fresh scope, but PSRemoting sessions are reused, so "
    "$global:/module-import/Set-Location changes on a remote host can leak into "
    "later calls to it — pass fresh_session=true to start from a clean session. "
    "Output is capped (default 60000 chars, head+tail kept); narrow the query "
    "(Select-Object, -First) rather than raising the cap.\n\n"
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
    "single-hop DirectoryEntry instead.\n\n"
    "TIP — Windows Failover Cluster nodes: don't set computer= to the "
    "cluster's Network Name / Client Access Point (e.g. a clustered file "
    "server's CAP) and then Invoke-Command *inside* that script to reach "
    "individual nodes — that's the same double-hop, and it fails outright "
    "(Access denied / Kerberos 0x8009030e) rather than silently no-op'ing. "
    "Instead pass the node FQDNs directly (computers=[...]), single-hop, no "
    "chaining needed — confirmed faster and reliable for reading "
    "hardware/model info off individual nodes behind a Client Access "
    "Point (Pirelli FSS466-01RU/FSS463-01RU clusters, 2026-09-08)."
)


async def _list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_powershell",
            description=RUN_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "PowerShell script body to execute."},
                    "computer": {
                        "type": "string",
                        "description": (
                            "Optional. Single target hostname/FQDN. Script runs via PSRemoting "
                            "on this host. Omit (and omit computers) to run directly on the "
                            "management server. Do not combine with with_domain_credentials, "
                            "and do not use for AD/GPO ACL writes — see the double-hop warning."
                        ),
                    },
                    "computers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional. Several targets; the script runs on all of them in "
                            "parallel and results are returned per host. May be combined with computer."
                        ),
                    },
                    "with_domain_credentials": {
                        "type": "boolean",
                        "description": (
                            "Optional, default false. Makes $DomainUser/$DomainPassword available "
                            "to a single-hop script (no computer=) from this server's configured "
                            "domain account, so you never paste the plaintext password into the "
                            "script. Use for e.g. writing a security descriptor via "
                            "System.DirectoryServices.DirectoryEntry — see the double-hop warning."
                        ),
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Optional. Seconds before the script is stopped (default 300, max 3600).",
                    },
                    "max_output": {
                        "type": "integer",
                        "description": "Optional. Per-host output cap in characters (default 60000).",
                    },
                    "fresh_session": {
                        "type": "boolean",
                        "description": "Optional. Discard the cached PSRemoting session(s) and open new ones first.",
                    },
                },
                "required": ["script"],
            },
        ),
        Tool(
            name="check_winrm",
            description=(
                "Test WinRM connectivity to one or many Windows computers. Always checks "
                "TCP 5985 (fast). With deep=true also performs a real WSMan/PSRemoting "
                "handshake with the domain credentials and reports latency — this catches "
                "'port open but auth/Kerberos broken', which a plain port test cannot."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "computer": {"type": "string", "description": "Hostname or IP of the target."},
                    "computers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Several targets, checked in parallel.",
                    },
                    "deep": {"type": "boolean", "description": "Also do a real PSRemoting handshake (default false)."},
                },
            },
        ),
        Tool(
            name="reset_winrm",
            description=(
                "Recovery: drop all resident workers, cached PSRemoting sessions and the SSH "
                "connection to the management server. Everything is re-created on the next "
                "call. Use if calls behave strangely (stale session, wedged worker) instead "
                "of restarting the MCP server."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


def _text(text: str, is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=is_error)


async def _call_tool(name: str, arguments: dict) -> CallToolResult:
    pool = get_pool()
    try:
        if name == "run_powershell":
            results = await pool.run(
                arguments["script"],
                _hosts_arg(arguments),
                with_domain_credentials=arguments.get("with_domain_credentials", False),
                timeout=arguments.get("timeout"),
                max_output=arguments.get("max_output"),
                fresh_session=arguments.get("fresh_session", False),
            )
            text, failed = format_results(results)
            return _text(text, failed)

        if name == "check_winrm":
            hosts = _hosts_arg(arguments)
            if not hosts:
                return _text("computer or computers is required", True)
            deep = bool(arguments.get("deep", False))
            rows = await pool.tcp_check(hosts, deep=deep)
            return _text(format_tcp(rows, deep))

        if name == "reset_winrm":
            return _text(await pool.reset())
    except WinRMInfraError as exc:
        return _text(f"[WINRM MCP ERROR] {exc}", True)
    except ValueError as exc:
        return _text(f"[WINRM MCP ERROR] {exc}", True)

    return _text(f"Unknown tool: {name}", True)


def _build_server() -> Server:
    """Register handlers for both mcp 1.x (decorators) and 2.x (constructor callbacks)."""
    if hasattr(Server, "list_tools"):  # mcp 1.x
        srv = Server(_NAME)
        srv.list_tools()(_list_tools)
        srv.call_tool()(_call_tool)
        return srv

    from mcp.types import ListToolsResult  # mcp 2.x

    async def on_list_tools(ctx, params):
        return ListToolsResult(tools=await _list_tools())

    async def on_call_tool(ctx, params):
        return await _call_tool(params.name, params.arguments or {})

    return Server(_NAME, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


server = _build_server()


def main() -> None:
    asyncio.run(_run())


async def _run() -> None:
    warm = None
    try:
        if get_pool().s.prewarm:
            warm = asyncio.create_task(get_pool().prewarm())
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())
    finally:
        if warm is not None:
            warm.cancel()
        if _pool is not None:
            await _pool.close()


if __name__ == "__main__":
    main()
