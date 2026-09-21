"""Live end-to-end smoke test against the real management server.

Needs the socks.pirelli tunnel and WINRM_MCP_PASSWORD. Run manually:
    WINRM_MCP_PASSWORD=... .venv/bin/python tests/live_smoke.py [dc-fqdn ...]
"""
import asyncio
import sys
import time

from winrm_mcp.client import Settings, WorkerPool
from winrm_mcp.server import format_results, format_tcp, load_config


async def timed(label, coro):
    t = time.monotonic()
    try:
        res = await coro
        print(f"--- {label}: {time.monotonic() - t:.2f}s")
        return res
    except Exception as exc:  # noqa: BLE001
        print(f"--- {label}: {time.monotonic() - t:.2f}s EXC {type(exc).__name__}: {exc}")
        raise


async def main(hosts):
    pool = WorkerPool(Settings.from_config(load_config()))
    try:
        r = await timed("cold local", pool.run("'hello ' + $env:COMPUTERNAME; whoami"))
        print(format_results(r)[0])
        r = await timed("warm local", pool.run("Get-Date -Format o"))
        print(format_results(r)[0])
        r = await timed("cyrillic roundtrip", pool.run("'Привет, мир: ' + 'абв'.Length; 'Ж'"))
        print(format_results(r)[0])
        r = await timed("streams+errors", pool.run(
            "Write-Host 'host-out'; Write-Warning 'warn'; Write-Error 'boom'; Get-Item C:\\nope; 'after'"))
        print(format_results(r)[0])
        r = await timed("throw", pool.run("'before'; throw 'terminating'"))
        print(format_results(r)[0])
        r = await timed("parse error", pool.run("if ("))
        print(format_results(r)[0])
        r = await timed("table", pool.run("Get-Service | Select -First 3 Name,Status | Format-Table -Auto"))
        print(format_results(r)[0])
        r = await timed("timeout 3s", pool.run("'partial'; Start-Sleep 30; 'never'", timeout=3))
        print(format_results(r))
        r = await timed("after-timeout local", pool.run("'still alive'"))
        print(format_results(r)[0])
        r = await timed("big output cap", pool.run("1..200000 | % { 'line ' + $_ }", max_output=5000))
        out = format_results(r)[0]
        print(len(out), out[-120:].replace("\n", "|"))
        r = await timed("domain creds var", pool.run("$DomainUser; ($DomainPassword.Length -gt 0)", with_domain_credentials=True))
        print(format_results(r)[0])
        rows = await timed("tcp", pool.tcp_check(hosts + ["no-such-host.rugroup.local"], timeout=4))
        print(format_tcp(rows, False))
        if hosts:
            r = await timed("remote cold", pool.run("$env:COMPUTERNAME; (Get-Date).ToString('o')", hosts[:1]))
            print(format_results(r)[0])
            r = await timed("remote warm", pool.run("$env:COMPUTERNAME", hosts[:1]))
            print(format_results(r)[0])
            r = await timed("remote fanout", pool.run("$env:COMPUTERNAME; Get-Date -f T", hosts))
            print(format_results(r)[0])
            r = await timed("remote error+connect_failed", pool.run("Get-Item C:\\nope; 'x'", hosts[:1] + ["no-such-host.rugroup.local"], ))
            print(format_results(r)[0])
            r = await timed("remote timeout", pool.run("Start-Sleep 30", hosts[:1], timeout=4))
            print(format_results(r))
            r = await timed("remote after timeout", pool.run("'ok after timeout'", hosts[:1]))
            print(format_results(r)[0])
            rows = await timed("deep check", pool.tcp_check(hosts, deep=True))
            print(format_tcp(rows, True))
    finally:
        await pool.close()


asyncio.run(main(sys.argv[1:]))
