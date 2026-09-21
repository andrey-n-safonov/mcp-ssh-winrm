# mcp-ssh-winrm

MCP server for Windows administration from a Linux host.

**How it works:**

```
Claude Code (Linux) ── one persistent SSH connection ──▶ Windows management server
                                                          └─ resident PowerShell workers (pool)
                                                               └─ cached PSRemoting sessions ──▶ target hosts
```

You write PowerShell — the server handles transport, execution, timeouts and returns clean, structured output. No shell escaping, no quoting nightmares.

Design points (each one exists because the previous connect-upload-run-delete-per-call design hurt):

- **Fast** — SSH/ProxyJump handshake and PowerShell start-up are paid once; repeat calls take ~0.2 s, calls to a host with a warm session ~0.25 s. `prewarm` even hides the first connect.
- **No secrets on disk** — scripts and the domain password are never written to the server. The one file uploaded is the worker script itself.
- **Real timeouts** — a timeout/cancel stops the script (partial output is returned); an unstoppable one restarts the worker. A kill-on-close Job Object guarantees no orphaned processes even if SSH drops.
- **Parallel** — `computers=[...]` fans out with one section of output per host; independent calls run on separate workers.
- **Recoverable** — stale SSH connections are detected by a pre-flight ping and re-established transparently; `reset_winrm` clears everything on demand.

Transport detail: Win32-OpenSSH without a pty delivers only the first chunk of a process' stdin, so stdin is used exactly once (secrets + a random token). The worker then listens on `127.0.0.1:<random port>` and the client reaches it through an SSH `direct-tcpip` channel; only a connection presenting the token is accepted, once. Requires `AllowTcpForwarding` on the management server's sshd (default).

## Tools

| Tool | Description |
|------|-------------|
| `run_powershell(script, computer?, computers?, with_domain_credentials?, timeout?, max_output?, fresh_session?)` | Run a PS script on the management server, or in parallel on one or more remote hosts via PSRemoting. Returns stdout, an `[stderr]` section, and per-host sections for multiple hosts. |
| `check_winrm(computer?, computers?, deep?)` | TCP 5985 check (fast); `deep=true` also performs a real PSRemoting handshake and reports latency. |
| `reset_winrm()` | Drop workers, cached sessions and the SSH connection; re-created on next call. |

Infrastructure failures (timeout, connect failure, SSH/worker down) come back with `isError=true`; ordinary script errors are returned as normal output with an `[stderr]` section.

## Requirements

- Linux host with SSH access to a Windows management server
- Windows management server with Windows PowerShell 5.1, `C:\Temp\` (or configured temp dir) and SSH TCP forwarding allowed
- Target computers must have WinRM enabled (port 5985) if using `computer` parameter
- Python 3.11+

## Installation

```bash
pip install mcp-ssh-winrm
```

Or from source:

```bash
git clone https://github.com/yourusername/mcp-ssh-winrm
cd mcp-ssh-winrm
pip install -e .
```

## Configuration

```bash
mkdir -p ~/.config/mcp-ssh-winrm
cp config.example.ini ~/.config/mcp-ssh-winrm/config.ini
# edit config.ini
```

The `[ssh]` section supports any host reachable from your Linux machine, including SSH aliases with `ProxyJump` defined in `~/.ssh/config`.

Set the domain account password via environment variable (never in the config file):

```bash
export WINRM_MCP_PASSWORD='your-domain-password'
```

## Claude Code integration

Add to `~/.claude.json` (or per-project `.claude/settings.json`):

```json
{
  "mcpServers": {
    "winrm": {
      "command": "mcp-ssh-winrm",
      "env": {
        "WINRM_MCP_PASSWORD": "your-domain-password"
      }
    }
  }
}
```

Or if running from source:

```json
{
  "mcpServers": {
    "winrm": {
      "command": "python",
      "args": ["-m", "winrm_mcp.server"],
      "env": {
        "WINRM_MCP_PASSWORD": "your-domain-password",
        "WINRM_MCP_CONFIG": "/path/to/config.ini"
      }
    }
  }
}
```

## Usage examples

**Check AD user on a domain controller:**
```
run_powershell(
  script="Get-ADUser -Identity jsmith -Properties PasswordLastSet, MemberOf",
  computer="dc01.domain.local"
)
```

**Run script locally on management server:**
```
run_powershell(script="Get-Service | Where-Object Status -eq 'Stopped'")
```

**Run on several hosts in parallel:**
```
run_powershell(
  script="Get-Service W32Time | Select-Object Status",
  computers=["dc01.domain.local", "dc02.domain.local"]
)
```

**Test WinRM (port + real handshake) before running remote commands:**
```
check_winrm(computers=["server01.domain.local"], deep=true)
```

## Tests

```bash
pip install -e '.[dev]' && pytest          # offline unit tests (formatting, worker protocol)
# live (needs the management server + WINRM_MCP_PASSWORD):
python tests/live_smoke.py dc01.example.local
python tests/e2e_mcp.py dc01.example.local  # through the real MCP stdio protocol
```

## ProxyJump / bastion hosts

If your management server is only reachable via a bastion/jump host, configure it in `~/.ssh/config`:

```
Host mgmt-server
    HostName 10.0.0.100
    User administrator
    ProxyJump bastion.example.com
    IdentityFile ~/.ssh/id_ed25519
```

Then set `host = mgmt-server` in `config.ini`. The MCP server reads `~/.ssh/config` automatically via asyncssh.

## License

MIT
