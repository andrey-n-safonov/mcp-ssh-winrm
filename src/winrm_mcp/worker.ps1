# mcp-ssh-winrm resident worker (Windows PowerShell 5.1). ASCII ONLY -- PS 5.1
# reads BOM-less .ps1 files as ANSI, so non-ASCII here would be mangled.
#
# Transport: Win32-OpenSSH (no pty) reliably delivers only the FIRST chunk of
# stdin to a child process, so stdin is used exactly once, for the init line:
#   -> {"token":..,"user":..,"password":..,"module_paths":[..]}
# The worker then listens on 127.0.0.1:<random port>, announces the port on
# stdout ("@@W1 {hello,port}") and the client reaches it through an SSH
# direct-tcpip channel. The first line on the socket must be the token (only one
# connection is accepted, then the listener closes), after which requests and
# results are NDJSON lines (UTF-8, no BOM), each result prefixed "@@W1 ":
#
#   -> {"op":"run","id":N,"script":..,"computers":[..],"timeout":S,"max_output":N,
#       "domain_creds":bool,"fresh":bool}
#   -> {"op":"tcp","id":N,"computers":[..],"timeout":S,"deep":bool}
#   -> {"op":"ping","id":N} | {"op":"reset","id":N} | {"op":"cancel"} | {"op":"shutdown"}
#   <- {"id":N,"type":"ack"} then {"id":N,"type":"result", ...}
#
# Sessions to remote hosts are cached (PSSession) between calls; the credential
# lives only in this process' memory and is never written to disk. The worker
# exits when the socket closes.

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'Continue'

$script:utf8 = New-Object System.Text.UTF8Encoding($false)
$script:stdout = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), $script:utf8)
$script:stdout.AutoFlush = $true
$script:stdout.NewLine = "`n"
$script:in = $null
$script:out = $null

$script:cred = $null
$script:domUser = $null
$script:domPass = $null
$script:sessions = @{}
$script:lastUse = @{}
$script:eof = $false
$script:exitAfter = $false
$script:readTask = $null

function Send-Frame($obj) {
    $json = $obj | ConvertTo-Json -Depth 8 -Compress
    $script:out.WriteLine('@@W1 ' + $json)
}

# Line from a completed ReadLineAsync task; $null on EOF or a broken connection.
function Get-TaskLine($task) {
    try { return $task.Result } catch { return $null }
}

function Limit-Text([string]$s, [int]$max) {
    if ($null -eq $s) { return '' }
    if ($max -le 0 -or $s.Length -le $max) { return $s }
    $head = [int]($max * 0.75)
    $tail = $max - $head
    return $s.Substring(0, $head) + "`n...[truncated " + ($s.Length - $max) + " chars]...`n" + $s.Substring($s.Length - $tail)
}

# Render a mixed list of output objects like the console host would, keeping
# order; warning/verbose/information records (merged in via 3>&1 4>&1 6>&1)
# become plain text lines.
function Format-Output($items, [int]$max) {
    $sb = New-Object System.Text.StringBuilder
    $buf = New-Object System.Collections.ArrayList
    $flush = {
        if ($buf.Count -gt 0) {
            [void]$sb.Append((($buf.ToArray() | Out-String -Width 1000).TrimEnd()) + "`n")
            $buf.Clear()
        }
    }
    foreach ($o in $items) {
        if ($o -is [System.Management.Automation.WarningRecord]) {
            & $flush; [void]$sb.Append('WARNING: ' + $o.Message + "`n")
        } elseif ($o -is [System.Management.Automation.VerboseRecord]) {
            & $flush; [void]$sb.Append('VERBOSE: ' + $o.Message + "`n")
        } elseif ($o -is [System.Management.Automation.InformationRecord]) {
            & $flush; [void]$sb.Append([string]$o.MessageData + "`n")
        } else {
            [void]$buf.Add($o)
        }
    }
    & $flush
    return Limit-Text $sb.ToString().TrimEnd().Replace("`r`n", "`n") $max
}

function Format-Errors($errs, [int]$max) {
    if (-not $errs -or @($errs).Count -eq 0) { return '' }
    return Limit-Text (($errs | Out-String -Width 1000).TrimEnd().Replace("`r`n", "`n")) $max
}

# True when stdin delivered a cancel frame or hit EOF while a request runs.
function Test-Abort {
    if ($script:readTask.IsCompleted) {
        $l = Get-TaskLine $script:readTask
        if ($null -eq $l) { $script:eof = $true; return $true }
        $script:readTask = $script:in.ReadLineAsync()
        try { if ((ConvertFrom-Json $l).op -eq 'cancel') { return $true } } catch {}
        if ($l -match '"op"\s*:\s*"shutdown"') { $script:eof = $true; return $true }
    }
    return $false
}

function Get-Status($timedOut, $cancelled, $hadErrors) {
    if ($timedOut) { return 'timeout' }
    if ($cancelled) { return 'cancelled' }
    if ($hadErrors) { return 'error' }
    return 'ok'
}

# Kill every descendant of this process (a native command's own children keep
# its output pipe open, so killing only direct children is not enough).
function Stop-ChildProcesses {
    try {
        $all = @(Get-CimInstance Win32_Process)
        $todo = New-Object System.Collections.Queue
        $todo.Enqueue([int]$PID)
        $kill = @()
        while ($todo.Count -gt 0) {
            $parent = $todo.Dequeue()
            foreach ($p in $all) {
                if ([int]$p.ParentProcessId -eq $parent -and [int]$p.ProcessId -ne [int]$PID) {
                    $kill += [int]$p.ProcessId
                    $todo.Enqueue([int]$p.ProcessId)
                }
            }
        }
        foreach ($id in $kill) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    } catch {}
}

function Invoke-Local($req) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $max = [int]$req.max_output
    $rs = [runspacefactory]::CreateRunspace()
    $rs.Open()
    if ($req.domain_creds) {
        if (-not $script:domUser) { throw 'domain credentials not configured' }
        $rs.SessionStateProxy.SetVariable('DomainUser', $script:domUser)
        $rs.SessionStateProxy.SetVariable('DomainPassword', $script:domPass)
    }
    $ps = [powershell]::Create()
    $ps.Runspace = $rs
    $wrapper = 'param($__mcp_src) & ([scriptblock]::Create($__mcp_src)) 3>&1 4>&1 6>&1'
    [void]$ps.AddScript($wrapper).AddArgument([string]$req.script)
    $outCol = New-Object 'System.Management.Automation.PSDataCollection[psobject]'
    $inCol = New-Object 'System.Management.Automation.PSDataCollection[psobject]'
    $inCol.Complete()
    $ar = $ps.BeginInvoke($inCol, $outCol)
    $timedOut = $false; $cancelled = $false; $hung = $false
    while (-not $ar.IsCompleted) {
        if ($sw.Elapsed.TotalSeconds -gt [double]$req.timeout) { $timedOut = $true; break }
        if (Test-Abort) { $cancelled = $true; break }
        [void]$ar.AsyncWaitHandle.WaitOne(50)
    }
    $extraErr = @()
    if ($timedOut -or $cancelled) {
        $st = $ps.BeginStop($null, $null)
        if (-not $st.AsyncWaitHandle.WaitOne(2000)) {
            # Stop() does not kill a running native command; it just waits for it.
            Stop-ChildProcesses
            if (-not $st.AsyncWaitHandle.WaitOne(1000)) { $hung = $true }
        }
        # Enumerating a still-open PSDataCollection blocks until the pipeline ends: close first.
        try { $outCol.Complete() } catch {}
        try { $ps.Streams.Error.Complete() } catch {}
    } else {
        try { [void]$ps.EndInvoke($ar) } catch {
            # Unwrap 'Exception calling EndInvoke': the script's own terminating error is inside.
            $inner = $_.Exception.InnerException
            if ($inner -is [System.Management.Automation.IContainsErrorRecord]) { $extraErr += $inner.ErrorRecord } else { $extraErr += $_ }
        }
    }
    $errs = @($ps.Streams.Error) + $extraErr
    $stdout = Format-Output @($outCol) $max
    $stderr = Format-Errors $errs $max
    $status = Get-Status $timedOut $cancelled ($errs.Count -gt 0)
    if ($hung) {
        # The script ignored Stop (uninterruptible native call): give up on this
        # process. Kill what it spawned; the worker exits after replying.
        $script:exitAfter = $true
        $stderr += "`n[worker] script did not stop; worker will restart"
        Stop-ChildProcesses
    }
    if (-not $hung) { try { $ps.Dispose(); $rs.Dispose() } catch {} }
    return @{ computer = $null; status = $status; stdout = $stdout; stderr = $stderr;
              error_count = $errs.Count; ms = [int]$sw.ElapsedMilliseconds }
}

function Remove-CachedSession($key) {
    $s = $script:sessions[$key]
    if ($s) { try { Remove-PSSession $s -ErrorAction SilentlyContinue } catch {} }
    $script:sessions.Remove($key)
    $script:lastUse.Remove($key)
}

# Returns @{ sessions = @{key=PSSession}; errors = @{key=text} } for the hosts.
function Get-Sessions($computers, [bool]$fresh) {
    $ready = @{}; $errors = @{}; $missing = @()
    foreach ($c in $computers) {
        $k = ([string]$c).ToLower()
        if ($fresh) { Remove-CachedSession $k }
        $s = $script:sessions[$k]
        if ($s -and $s.State -eq 'Opened' -and $s.Availability -eq 'Available') {
            $idle = ((Get-Date) - $script:lastUse[$k]).TotalSeconds
            $alive = $true
            if ($idle -gt 30) {
                # Cheap liveness probe so a script never starts on a dead session.
                $j = Invoke-Command -Session $s -ScriptBlock { 1 } -AsJob
                if (Wait-Job $j -Timeout 8) { $alive = ($j.State -eq 'Completed') } else { $alive = $false }
                Remove-Job $j -Force -ErrorAction SilentlyContinue
            }
            if ($alive) { $ready[$k] = $s; continue }
            Remove-CachedSession $k
        } elseif ($s) {
            Remove-CachedSession $k
        }
        $missing += [string]$c
    }
    if ($missing.Count -gt 0) {
        if (-not $script:cred) { throw 'domain credentials not configured' }
        $opt = New-PSSessionOption -OpenTimeout 20000 -OperationTimeout 60000 -IdleTimeout 1800000 -NoMachineProfile
        $ev = $null
        $new = @(New-PSSession -ComputerName $missing -Credential $script:cred -SessionOption $opt `
                 -ErrorAction SilentlyContinue -ErrorVariable ev)
        foreach ($s in $new) {
            $k = ([string]$s.ComputerName).ToLower()
            $script:sessions[$k] = $s
            $script:lastUse[$k] = Get-Date
            $ready[$k] = $s
        }
        $unresolved = @($missing | Where-Object { -not $ready.ContainsKey($_.ToLower()) })
        foreach ($c in $unresolved) {
            $k = $c.ToLower()
            $msg = $null
            foreach ($e in @($ev)) {
                if (([string]$e.TargetObject).ToLower() -eq $k -or ([string]$e.Exception.Message).ToLower().Contains($k)) {
                    $msg = [string]$e.Exception.Message; break
                }
            }
            if (-not $msg) { $msg = ((@($ev) | ForEach-Object { [string]$_.Exception.Message }) -join '; ') }
            if (-not $msg) { $msg = 'could not open a PSRemoting session' }
            $errors[$k] = $msg
        }
    }
    return @{ sessions = $ready; errors = $errors }
}

function Invoke-Remote($req) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $max = [int]$req.max_output
    $computers = @($req.computers)
    $got = Get-Sessions $computers ([bool]$req.fresh)
    $results = @{}
    foreach ($k in $got.errors.Keys) {
        $results[$k] = @{ computer = $k; status = 'connect_failed'; stdout = ''; stderr = (Limit-Text $got.errors[$k] $max);
                          error_count = 1; ms = [int]$sw.ElapsedMilliseconds }
    }
    $sessArr = @($got.sessions.Values)
    $timedOut = $false; $cancelled = $false
    if ($sessArr.Count -gt 0) {
        $src = "& {`n" + [string]$req.script + "`n} 3>&1 4>&1 6>&1"
        $sb = [scriptblock]::Create($src)
        $job = Invoke-Command -Session $sessArr -ScriptBlock $sb -AsJob
        $terminal = 'Completed', 'Failed', 'Stopped'
        while ($true) {
            $pending = @($job.ChildJobs | Where-Object { $terminal -notcontains [string]$_.State })
            if ($pending.Count -eq 0) { break }
            if ($sw.Elapsed.TotalSeconds -gt [double]$req.timeout) { $timedOut = $true; break }
            if (Test-Abort) { $cancelled = $true; break }
            Start-Sleep -Milliseconds 50
        }
        foreach ($ch in $job.ChildJobs) {
            $k = ([string]$ch.Location).ToLower()
            $unfinished = ($terminal -notcontains [string]$ch.State)
            if ($unfinished) {
                try { Stop-Job $ch -ErrorAction SilentlyContinue } catch {}
            }
            $items = @(Receive-Job $ch 2>&1)
            $errs = @($items | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] })
            # Stopping an unfinished job makes PowerShell report this; it is our doing, not the script's.
            if ($unfinished) { $errs = @($errs | Where-Object { ($_ | Out-String) -notlike '*pipeline has been stopped*' }) }
            $outs = @($items | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
            if (-not $unfinished -and $ch.State -eq 'Failed' -and $ch.JobStateInfo.Reason -and $errs.Count -eq 0) {
                $errs = @($ch.JobStateInfo.Reason.Message)
            }
            $st = Get-Status $unfinished ($cancelled -and $unfinished) ($errs.Count -gt 0)
            $results[$k] = @{ computer = $k; status = $st; stdout = (Format-Output $outs $max);
                              stderr = (Format-Errors $errs $max); error_count = $errs.Count;
                              ms = [int]$sw.ElapsedMilliseconds }
            if ($unfinished -or $ch.State -eq 'Failed') { Remove-CachedSession $k } else { $script:lastUse[$k] = Get-Date }
        }
        Remove-Job $job -Force -ErrorAction SilentlyContinue
    }
    $list = @()
    foreach ($c in $computers) {
        $k = ([string]$c).ToLower()
        $r = $results[$k]
        if (-not $r) { $r = @{ computer = $k; status = 'error'; stdout = ''; stderr = 'no result for host'; error_count = 1; ms = 0 } }
        $r.computer = [string]$c
        $list += , $r
    }
    return $list
}

function Invoke-Tcp($req) {
    $timeoutMs = [int]([double]$req.timeout * 1000)
    $probes = @()
    foreach ($c in @($req.computers)) {
        $cl = New-Object System.Net.Sockets.TcpClient
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $probes += , @{ computer = [string]$c; client = $cl; sw = $sw; task = $cl.ConnectAsync([string]$c, 5985) }
    }
    $rows = @()
    foreach ($p in $probes) {
        $left = [Math]::Max(1, $timeoutMs - [int]$p.sw.ElapsedMilliseconds)
        $ok = $false; $err = $null
        try { $ok = $p.task.Wait($left) -and $p.client.Connected } catch { $err = $_.Exception.GetBaseException().Message }
        $ms = [int]$p.sw.ElapsedMilliseconds
        try { $p.client.Close() } catch {}
        $rows += , @{ computer = $p.computer; tcp = [bool]$ok; tcp_ms = $ms; tcp_error = $err; winrm = $null; winrm_ms = $null; winrm_error = $null }
    }
    if ($req.deep) {
        foreach ($r in $rows) {
            if (-not $r.tcp) { continue }
            $sw = [Diagnostics.Stopwatch]::StartNew()
            try {
                $got = Get-Sessions @($r.computer) $false
                if ($got.errors.Count -gt 0) {
                    $r.winrm = $false; $r.winrm_error = (@($got.errors.Values) -join '; ')
                } else {
                    $s = @($got.sessions.Values)[0]
                    $j = Invoke-Command -Session $s -ScriptBlock { $env:COMPUTERNAME } -AsJob
                    if (Wait-Job $j -Timeout 15) {
                        $r.winrm = ($j.State -eq 'Completed')
                        if (-not $r.winrm) { $r.winrm_error = [string]$j.ChildJobs[0].JobStateInfo.Reason.Message }
                    } else { $r.winrm = $false; $r.winrm_error = 'timeout' }
                    Remove-Job $j -Force -ErrorAction SilentlyContinue
                }
            } catch { $r.winrm = $false; $r.winrm_error = $_.Exception.Message }
            $r.winrm_ms = [int]$sw.ElapsedMilliseconds
        }
    }
    return $rows
}

function Reset-All {
    foreach ($k in @($script:sessions.Keys)) { Remove-CachedSession $k }
}

function Handle-Request($req) {
    switch ($req.op) {
        'ping' { Send-Frame @{ id = $req.id; type = 'result'; ok = $true; sessions = @($script:sessions.Keys) } }
        'reset' { Reset-All; Send-Frame @{ id = $req.id; type = 'result'; ok = $true } }
        'run' {
            Send-Frame @{ id = $req.id; type = 'ack' }
            if ($req.computers -and @($req.computers).Count -gt 0) {
                $res = Invoke-Remote $req
            } else {
                $res = @(Invoke-Local $req)
            }
            Send-Frame @{ id = $req.id; type = 'result'; results = @($res); exiting = [bool]$script:exitAfter }
        }
        'tcp' {
            Send-Frame @{ id = $req.id; type = 'ack' }
            $rows = Invoke-Tcp $req
            Send-Frame @{ id = $req.id; type = 'result'; rows = @($rows) }
        }
        default { Send-Frame @{ id = $req.id; type = 'result'; ok = $false; error = ('unknown op: ' + $req.op) } }
    }
}

function Wait-Task($task, [double]$seconds) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not $task.IsCompleted) {
        if ($sw.Elapsed.TotalSeconds -gt $seconds) { return $false }
        Start-Sleep -Milliseconds 20
    }
    return $true
}

# ---- kill-on-close job object ----
# Put this process in a job whose handle dies with it: whenever the worker exits
# (normally, killed, or after an unstoppable script) the OS also kills every
# process it ever spawned, even orphans whose parent already exited (e.g.
# "cmd /c ping" leaves PING.EXE parentless). Best effort.
try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class McpJob {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attrs, string name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int cls, IntPtr info, uint len);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr proc);
    [StructLayout(LayoutKind.Sequential)] struct BASIC { public long PerProc; public long PerJob; public uint LimitFlags; public UIntPtr MinWs; public UIntPtr MaxWs; public uint ActiveLimit; public UIntPtr Affinity; public uint Priority; public uint Sched; }
    [StructLayout(LayoutKind.Sequential)] struct IOC { public ulong A, B, C, D, E, F; }
    [StructLayout(LayoutKind.Sequential)] struct EXT { public BASIC Basic; public IOC Io; public UIntPtr ProcMem; public UIntPtr JobMem; public UIntPtr PeakProc; public UIntPtr PeakJob; }
    static IntPtr handle;
    public static bool Init() {
        handle = CreateJobObject(IntPtr.Zero, null);
        if (handle == IntPtr.Zero) return false;
        EXT e = new EXT();
        e.Basic.LimitFlags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        int size = Marshal.SizeOf(typeof(EXT));
        IntPtr p = Marshal.AllocHGlobal(size);
        try {
            Marshal.StructureToPtr(e, p, false);
            if (!SetInformationJobObject(handle, 9, p, (uint)size)) return false;
        } finally { Marshal.FreeHGlobal(p); }
        return AssignProcessToJobObject(handle, System.Diagnostics.Process.GetCurrentProcess().Handle);
    }
}
"@
    $script:jobOk = [McpJob]::Init()
} catch { $script:jobOk = $false }

# ---- startup: one-shot init on stdin, then switch to the loopback socket ----
$stdinReader = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $script:utf8)
$initTask = $stdinReader.ReadLineAsync()
if (-not (Wait-Task $initTask 30)) { exit 2 }
$init = (Get-TaskLine $initTask) | ConvertFrom-Json
if ($init.user -and $init.password) {
    $script:domUser = [string]$init.user
    $script:domPass = [string]$init.password
    $sec = ConvertTo-SecureString $script:domPass -AsPlainText -Force
    $script:cred = New-Object System.Management.Automation.PSCredential($script:domUser, $sec)
}
foreach ($p in @($init.module_paths)) {
    if ($p -and ($env:PSModulePath -notlike ('*' + $p + '*'))) { $env:PSModulePath += ';' + $p }
}
$token = [string]$init.token

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$script:stdout.WriteLine('@@W1 ' + (@{ type = 'hello'; proto = 2; port = $port; ps = $PSVersionTable.PSVersion.ToString() } | ConvertTo-Json -Compress))

$sw = [Diagnostics.Stopwatch]::StartNew()
while (-not $listener.Pending()) {
    if ($sw.Elapsed.TotalSeconds -gt 30) { exit 3 }
    Start-Sleep -Milliseconds 20
}
$client = $listener.AcceptTcpClient()
$listener.Stop()
$client.NoDelay = $true
$stream = $client.GetStream()
$script:in = New-Object System.IO.StreamReader($stream, $script:utf8)
$script:out = New-Object System.IO.StreamWriter($stream, $script:utf8)
$script:out.AutoFlush = $true
$script:out.NewLine = "`n"

$tokTask = $script:in.ReadLineAsync()
if (-not (Wait-Task $tokTask 10) -or (Get-TaskLine $tokTask) -ne $token) { $client.Close(); exit 4 }
$script:readTask = $script:in.ReadLineAsync()
Send-Frame @{ type = 'ready'; ps = $PSVersionTable.PSVersion.ToString(); host = $env:COMPUTERNAME;
              user = [Security.Principal.WindowsIdentity]::GetCurrent().Name; creds = [bool]$script:cred; job = [bool]$script:jobOk }

try {
    while (-not $script:eof -and -not $script:exitAfter) {
        $line = Get-TaskLine $script:readTask
        if ($null -eq $line) { break }
        $script:readTask = $script:in.ReadLineAsync()
        if ($line.Trim().Length -eq 0) { continue }
        $req = $null
        try {
            $req = $line | ConvertFrom-Json
            if ($req.op -eq 'shutdown') { break }
            if ($req.op -eq 'cancel') { continue }
            Handle-Request $req
        } catch {
            $id = $null
            if ($req) { $id = $req.id }
            Send-Frame @{ id = $id; type = 'result'; internal_error = ($_ | Out-String -Width 1000).TrimEnd() }
        }
    }
} finally {
    Reset-All
    try { $client.Close() } catch {}
}
# A runspace thread stuck in an uninterruptible call must not keep the process alive.
[Environment]::Exit(0)
