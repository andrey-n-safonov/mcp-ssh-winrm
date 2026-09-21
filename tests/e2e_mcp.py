"""End-to-end through the real MCP stdio protocol (needs tunnel + WINRM_MCP_PASSWORD).

    WINRM_MCP_CONFIG=config.ini .venv/bin/python tests/e2e_mcp.py dc01.example.local
"""
import asyncio
import os
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def call(session, name, args):
    t = time.monotonic()
    res = await session.call_tool(name, args)
    text = "\n".join(c.text for c in res.content)
    is_error = getattr(res, "isError", getattr(res, "is_error", None))  # renamed in mcp 2.x
    print(f"--- {name}({', '.join(f'{k}={str(v)[:40]!r}' for k, v in args.items())}) {time.monotonic() - t:.2f}s isError={is_error}\n{text}")
    return res


async def main(hosts):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "winrm_mcp.server"], env=dict(os.environ)
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("tools:", [t.name for t in (await s.list_tools()).tools]); await asyncio.sleep(6)  # let prewarm finish
            await call(s, "run_powershell", {"script": "'hi from ' + $env:COMPUTERNAME"})
            await call(s, "run_powershell", {"script": "$env:COMPUTERNAME", "computers": hosts})
            await call(s, "run_powershell", {"script": "Start-Sleep 20", "timeout": 2})
            await call(s, "run_powershell", {"script": "1", "computer": hosts[0], "with_domain_credentials": True})
            await call(s, "check_winrm", {"computers": hosts, "deep": True})
            await call(s, "check_winrm", {})
            await call(s, "reset_winrm", {})
            await call(s, "run_powershell", {"script": "'works after reset'"})


asyncio.run(main(sys.argv[1:]))
