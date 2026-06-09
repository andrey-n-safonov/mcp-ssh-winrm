# mcp-ssh-winrm

MCP server for Windows administration from a Linux host.

**How it works:**

```
Claude Code (Linux) → SSH → Windows Management Server → PSRemoting (WinRM) → Target hosts
```

You write PowerShell — the server handles SSH transport, script upload, execution, and returns clean output. No shell escaping, no quoting nightmares.

## Tools

| Tool | Description |
|------|-------------|
| `run_powershell(script, computer?)` | Run PS script on the management server, or via `Invoke-Command` on a remote host |
| `check_winrm(computer)` | Test WinRM connectivity (TCP 5985) to a target computer |

## Requirements

- Linux host with SSH access to a Windows management server
- Windows management server with PowerShell and `C:\Temp\` (or configured temp dir)
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

**Test WinRM before running remote commands:**
```
check_winrm(computer="server01.domain.local")
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
