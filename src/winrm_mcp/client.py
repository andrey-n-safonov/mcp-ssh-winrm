"""SSH transport + pool of resident PowerShell workers.

One persistent SSH connection to the management server carries a small pool of
long-lived ``powershell.exe`` workers (see ``worker.ps1``) that speak NDJSON over
NDJSON over a loopback socket. Compared to the old connect-upload-run-delete cycle per call this:

* pays SSH/ProxyJump handshake and PowerShell start-up once, not per call;
* keeps PSRemoting sessions to target hosts warm between calls;
* never writes a script (or the domain password) to disk on the server;
* can really stop a script on timeout/cancel, not just stop waiting for it;
* returns structured per-host results and can fan out to many hosts in parallel.
"""

from __future__ import annotations

import asyncio
import configparser
import hashlib
import itertools
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import asyncssh

FRAME_PREFIX = "@@W1 "
PS_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
WORKER_SRC = Path(__file__).with_name("worker.ps1")


class WinRMInfraError(Exception):
    """Transport-level failure (SSH down, worker died/hung) — not a script error."""


@dataclass
class HostResult:
    computer: str | None
    status: str  # ok | error | timeout | cancelled | connect_failed
    stdout: str = ""
    stderr: str = ""
    error_count: int = 0
    ms: int = 0


@dataclass
class Settings:
    host: str
    port: int = 22
    user: str | None = None
    key: str | None = None
    temp_dir: str = r"C:\Temp\mcp-ssh-winrm"
    module_paths: list[str] = field(default_factory=list)
    command_timeout: float = 300.0
    cleanup_timeout: float = 15.0
    connect_retries: int = 2
    connect_retry_delay: float = 3.0
    connect_timeout: float = 20.0
    max_workers: int = 3
    max_output_chars: int = 60000
    ping_after: float = 20.0
    prewarm: bool = True
    domain_user: str = ""
    domain_password: str = ""

    @classmethod
    def from_config(cls, cfg: configparser.ConfigParser) -> "Settings":
        ssh = cfg["ssh"]
        domain = cfg["domain"] if cfg.has_section("domain") else {}
        key = ssh.get("key", fallback="")
        extra = ssh.get("extra_ps_module_paths", "")
        return cls(
            host=ssh["host"],
            port=ssh.getint("port", fallback=22),
            user=ssh.get("user", fallback="") or None,
            key=os.path.expanduser(key) if key else None,
            temp_dir=ssh.get("temp_dir", fallback=cls.temp_dir),
            module_paths=[p.strip() for p in extra.split(";") if p.strip()],
            command_timeout=ssh.getfloat("command_timeout", fallback=300.0),
            cleanup_timeout=ssh.getfloat("cleanup_timeout", fallback=15.0),
            connect_retries=ssh.getint("connect_retries", fallback=2),
            connect_retry_delay=ssh.getfloat("connect_retry_delay", fallback=3.0),
            connect_timeout=ssh.getfloat("connect_timeout", fallback=20.0),
            max_workers=ssh.getint("max_workers", fallback=3),
            max_output_chars=ssh.getint("max_output_chars", fallback=60000),
            ping_after=ssh.getfloat("ping_after", fallback=20.0),
            prewarm=ssh.getboolean("prewarm", fallback=True),
            domain_user=domain.get("user", ""),
            domain_password=os.environ.get(domain.get("password_env", "WINRM_MCP_PASSWORD"), ""),
        )


class Worker:
    """One resident powershell.exe worker on the management server."""

    def __init__(
        self,
        process: asyncssh.SSHClientProcess,
        reader: asyncssh.SSHReader,
        writer: asyncssh.SSHWriter,
        idx: int,
    ):
        self.idx = idx
        self.process = process  # only kept to own the powershell.exe lifetime
        self.reader = reader  # NDJSON frames arrive here (direct-tcpip to the worker)
        self.writer = writer
        self.busy = False
        self.dead = False
        self.death_reason = ""
        self.hosts: set[str] = set()  # computers this worker likely holds sessions for
        self.last_used = time.monotonic()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._acks: dict[int, asyncio.Event] = {}
        self._hello = asyncio.Event()
        self.info: dict = {}
        self._stderr_tail = ""
        self._tasks = [
            asyncio.create_task(self._read_frames()),
            asyncio.create_task(self._drain(process.stdout)),
            asyncio.create_task(self._drain(process.stderr)),
            asyncio.create_task(self._watch_process()),
        ]

    # -- reading ----------------------------------------------------------

    async def _read_frames(self) -> None:
        buf = ""
        try:
            while True:
                chunk = await self.reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._dispatch(line.rstrip("\r"))
        except (asyncssh.Error, OSError):
            pass
        finally:
            self._mark_dead("worker connection closed")

    async def _drain(self, stream) -> None:
        """Keep the process' stdout/stderr flowing; keep a tail for diagnostics."""
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                self._stderr_tail = (self._stderr_tail + chunk)[-2000:]
        except (asyncssh.Error, OSError):
            pass

    async def _watch_process(self) -> None:
        try:
            await self.process.wait_closed()
        except Exception:
            pass
        self._mark_dead("worker process exited")

    def _dispatch(self, line: str) -> None:
        if not line.startswith(FRAME_PREFIX):
            return  # stray host output — never trust it as a frame
        try:
            frame = json.loads(line[len(FRAME_PREFIX):])
        except json.JSONDecodeError:
            return
        if frame.get("type") == "ready":
            self.info = frame
            self._hello.set()
            return
        fid = frame.get("id")
        if frame.get("type") == "ack":
            ev = self._acks.get(fid)
            if ev:
                ev.set()
            return
        fut = self._pending.pop(fid, None)
        if fut and not fut.done():
            fut.set_result(frame)

    def _mark_dead(self, reason: str) -> None:
        if self.dead:
            return
        self.dead = True
        self.death_reason = reason
        tail = self._stderr_tail.strip()
        exc = WinRMInfraError(f"{reason}" + (f" (worker stderr: {tail[-500:]})" if tail else ""))
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # -- requests ---------------------------------------------------------

    async def wait_ready(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._hello.wait(), timeout)
        except asyncio.TimeoutError:
            self.kill("worker did not start")
            raise WinRMInfraError(
                "worker did not start within %.0fs%s"
                % (timeout, f" (stderr: {self._stderr_tail.strip()[-500:]})" if self._stderr_tail.strip() else "")
            ) from None
        if self.dead:
            raise WinRMInfraError(self.death_reason)

    def _send(self, payload: dict) -> None:
        if self.dead:
            raise WinRMInfraError(f"worker is dead: {self.death_reason}")
        self.writer.write(json.dumps(payload, ensure_ascii=False) + "\n")

    async def request(self, payload: dict, *, deadline: float) -> dict:
        """Send a request and wait up to ``deadline`` seconds for its result frame.

        On deadline the worker is killed (it is presumed hung); on caller
        cancellation a cancel frame is sent first so the script is stopped
        cleanly, and the worker is only killed if it does not answer.
        """
        fid = next(self._ids)
        payload = {**payload, "id": fid}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[fid] = fut
        self._acks[fid] = asyncio.Event()
        try:
            try:
                self._send(payload)
                await self.writer.drain()
            except (asyncssh.Error, OSError) as exc:
                self._mark_dead(f"write to worker failed: {exc}")
                raise WinRMInfraError(self.death_reason) from exc
            try:
                frame = await asyncio.wait_for(asyncio.shield(fut), deadline)
            except asyncio.TimeoutError:
                self.kill("worker unresponsive")
                raise WinRMInfraError(
                    f"worker did not answer within {deadline:.0f}s and was killed; "
                    "the script may still have been running on the server"
                ) from None
            except asyncio.CancelledError:
                await self._cancel_running(fut)
                raise
        finally:
            self._pending.pop(fid, None)
            self._acks.pop(fid, None)
            if fut.done() and not fut.cancelled():
                fut.exception()  # mark retrieved: no 'never retrieved' log noise
            self.last_used = time.monotonic()
        if frame.get("exiting"):
            self._mark_dead("worker is restarting after an unstoppable script")
        if frame.get("internal_error"):
            raise WinRMInfraError(f"worker internal error: {frame['internal_error']}")
        return frame

    async def _cancel_running(self, fut: asyncio.Future) -> None:
        try:
            self._send({"op": "cancel"})
            await self.writer.drain()
            await asyncio.wait_for(asyncio.shield(fut), 8)
        except Exception:
            self.kill("cancel was not acknowledged")

    # -- lifecycle --------------------------------------------------------

    def kill(self, reason: str) -> None:
        self._mark_dead(reason)
        try:
            self.writer.close()  # worker exits when its socket closes
        except Exception:
            pass
        try:
            self.process.close()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()

    async def close(self, timeout: float) -> None:
        if not self.dead:
            try:
                self._send({"op": "shutdown"})
                await asyncio.wait_for(self.process.wait_closed(), timeout)
            except Exception:
                pass
        self.kill("closed")


class WorkerPool:
    def __init__(self, settings: Settings):
        self.s = settings
        self._conn: asyncssh.SSHClientConnection | None = None
        self._conn_lock = asyncio.Lock()
        self._remote_worker_path: str | None = None
        self._workers: list[Worker] = []
        self._cond = asyncio.Condition()
        self._spawning = 0
        self._idx = itertools.count(1)

    # -- SSH connection ---------------------------------------------------

    def _ssh_kwargs(self) -> dict:
        kw: dict = {
            "host": self.s.host,
            "port": self.s.port,
            "known_hosts": None,
            "config": [os.path.expanduser("~/.ssh/config")],
            "keepalive_interval": 15,
            "keepalive_count_max": 3,
            "connect_timeout": self.s.connect_timeout,
            "login_timeout": 30,
        }
        if self.s.user:
            kw["username"] = self.s.user
        if self.s.key:
            kw["client_keys"] = [self.s.key]
        return kw

    async def _ensure_conn(self) -> asyncssh.SSHClientConnection:
        async with self._conn_lock:
            if self._conn is not None and not self._conn.is_closed():
                return self._conn
            self._conn = None
            self._remote_worker_path = None
            last: Exception | None = None
            for attempt in range(self.s.connect_retries + 1):
                try:
                    conn = await asyncssh.connect(**self._ssh_kwargs())
                    break
                except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
                    last = exc
                    if attempt < self.s.connect_retries:
                        await asyncio.sleep(self.s.connect_retry_delay)
            else:
                raise WinRMInfraError(
                    f"SSH connect to {self.s.host} failed after {self.s.connect_retries + 1} attempts: "
                    f"{last!r} (is the socks.pirelli tunnel up?)"
                )
            self._conn = conn
            await self._upload_worker(conn)
            return conn

    async def _upload_worker(self, conn: asyncssh.SSHClientConnection) -> None:
        src = WORKER_SRC.read_bytes()
        digest = hashlib.sha256(src).hexdigest()[:12]
        win_path = f"{self.s.temp_dir}\\worker_{digest}.ps1"
        await conn.run(f'cmd /c if not exist "{self.s.temp_dir}" mkdir "{self.s.temp_dir}"', check=False)
        drive, rest = win_path[0], win_path[2:].replace("\\", "/")
        async with conn.start_sftp_client() as sftp:
            # Windows OpenSSH SFTP expects /C:/path/... — the file holds no secrets.
            async with sftp.open(f"/{drive}:{rest}", "wb") as f:
                await f.write(src)
        self._remote_worker_path = win_path

    async def _conn_alive(self) -> bool:
        conn = self._conn
        if conn is None or conn.is_closed():
            return False
        try:
            await asyncio.wait_for(conn.run("cmd /c rem", check=False), 6)
            return True
        except (asyncssh.Error, OSError, asyncio.TimeoutError):
            return False

    def _drop_conn(self) -> None:
        conn, self._conn = self._conn, None
        self._remote_worker_path = None
        for w in self._workers:
            w.kill("ssh connection dropped")
        self._workers.clear()
        if conn is not None:
            conn.close()

    # -- workers ----------------------------------------------------------

    async def _spawn(self) -> Worker:
        conn = await self._ensure_conn()
        cmd = (
            f'{PS_EXE} -NoProfile -NonInteractive -ExecutionPolicy Bypass '
            f'-File "{self._remote_worker_path}"'
        )
        token = secrets.token_hex(32)
        proc = None
        try:
            proc = await conn.create_process(cmd, encoding="utf-8", errors="replace")
            # Win32-OpenSSH (no pty) delivers only the first chunk of stdin, so the
            # secrets go in one single write; everything else uses the socket.
            proc.stdin.write(json.dumps({
                "token": token,
                "user": self.s.domain_user,
                "password": self.s.domain_password,
                "module_paths": self.s.module_paths,
            }) + "\n")
            await proc.stdin.drain()
            port = await self._read_port(proc, 30)
            reader, writer = await conn.open_connection(
                "127.0.0.1", port, encoding="utf-8", errors="replace"
            )
            writer.write(token + "\n")
            await writer.drain()
        except (asyncssh.Error, OSError, asyncio.TimeoutError, WinRMInfraError) as exc:
            if proc is not None:
                proc.close()
            if isinstance(exc, WinRMInfraError):
                raise
            self._drop_conn()
            raise WinRMInfraError(f"could not start worker: {exc!r}") from exc
        worker = Worker(proc, reader, writer, next(self._idx))
        await worker.wait_ready(30)
        return worker

    @staticmethod
    async def _read_port(proc, timeout: float) -> int:
        """Read the worker's hello line (carries its loopback port) from stdout."""
        buf = ""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise WinRMInfraError("worker did not announce its port in time")
            chunk = await asyncio.wait_for(proc.stdout.read(4096), left)
            if not chunk:
                err = (await asyncio.wait_for(proc.stderr.read(2000), 2)) if proc.stderr else ""
                raise WinRMInfraError(f"worker exited during start-up: {err.strip()[-500:]}")
            buf += chunk
            for line in buf.split("\n"):
                if line.startswith(FRAME_PREFIX):
                    try:
                        frame = json.loads(line[len(FRAME_PREFIX):])
                    except json.JSONDecodeError:
                        continue
                    if frame.get("type") == "hello":
                        return int(frame["port"])

    @asynccontextmanager
    async def acquire(self, computers: list[str]):
        """Reserve a healthy idle worker, preferring one with warm sessions."""
        want = {c.lower() for c in computers}
        worker: Worker | None = None
        for _attempt in range(self.s.connect_retries + 1):
            worker = await self._reserve(want)
            # A stale TCP connection looks alive until we try to use it. Probe
            # before sending the real request, so a failure here is provably
            # "nothing ran yet" and safe to retry transparently.
            if time.monotonic() - worker.last_used > self.s.ping_after:
                try:
                    await worker.request({"op": "ping"}, deadline=6)
                except WinRMInfraError:
                    await self._release(worker, discard=True)
                    if not await self._conn_alive():
                        self._drop_conn()  # stale connection: rebuild the whole transport
                    worker = None
                    continue
            break
        if worker is None:
            raise WinRMInfraError("could not obtain a healthy worker (connection unstable)")
        try:
            yield worker
        finally:
            worker.hosts |= want
            await self._release(worker, discard=worker.dead)

    async def _reserve(self, want: set[str]) -> Worker:
        async with self._cond:
            while True:
                self._workers = [w for w in self._workers if not w.dead]
                idle = [w for w in self._workers if not w.busy]
                if idle:
                    warm = [w for w in idle if want and want <= w.hosts]
                    pick = (warm or idle)[0]
                    pick.busy = True
                    return pick
                if len(self._workers) + self._spawning < self.s.max_workers:
                    self._spawning += 1
                    break
                await self._cond.wait()
        try:
            worker = await self._spawn()
        except BaseException:
            async with self._cond:
                self._spawning -= 1
                self._cond.notify()
            raise
        async with self._cond:
            self._spawning -= 1
            worker.busy = True
            self._workers.append(worker)
        return worker

    async def _release(self, worker: Worker, discard: bool) -> None:
        async with self._cond:
            worker.busy = False
            if discard:
                worker.kill("discarded")
                if worker in self._workers:
                    self._workers.remove(worker)
            self._cond.notify_all()

    async def close(self) -> None:
        for w in list(self._workers):
            await w.close(self.s.cleanup_timeout)
        self._workers.clear()
        if self._conn is not None:
            self._conn.close()
            try:
                await asyncio.wait_for(self._conn.wait_closed(), self.s.cleanup_timeout)
            except Exception:
                pass
            self._conn = None

    # -- public operations ------------------------------------------------

    async def run(
        self,
        script: str,
        computers: list[str] | None = None,
        *,
        with_domain_credentials: bool = False,
        timeout: float | None = None,
        max_output: int | None = None,
        fresh_session: bool = False,
    ) -> list[HostResult]:
        computers = _dedupe(computers or [])
        if with_domain_credentials and computers:
            raise ValueError(
                "with_domain_credentials is for single-hop scripts only — combining it "
                "with computer= would just recreate the double-hop pattern it exists to "
                "avoid. Omit computer= and do the LDAP bind (or similar) inside the "
                "script itself, targeting whichever server you need by name."
            )
        if (computers or with_domain_credentials) and not (self.s.domain_user and self.s.domain_password):
            raise ValueError(
                "domain.user and the WINRM_MCP_PASSWORD env var are required for "
                "PSRemoting / with_domain_credentials"
            )
        timeout = min(timeout or self.s.command_timeout, 3600.0)
        req = {
            "op": "run",
            "script": script,
            "computers": computers,
            "timeout": timeout,
            "max_output": max_output or self.s.max_output_chars,
            "domain_creds": with_domain_credentials,
            "fresh": fresh_session,
        }
        async with self.acquire(computers) as w:
            frame = await w.request(req, deadline=timeout + 30)
        return [HostResult(**{k: r.get(k) for k in HostResult.__dataclass_fields__ if k in r}) for r in frame["results"]]

    async def tcp_check(self, computers: list[str], deep: bool = False, timeout: float = 5.0) -> list[dict]:
        computers = _dedupe(computers)
        req = {"op": "tcp", "computers": computers, "timeout": timeout, "deep": deep}
        async with self.acquire(computers if deep else []) as w:
            frame = await w.request(req, deadline=timeout + (60 if deep else 15))
        return frame["rows"]

    async def prewarm(self) -> None:
        """Open the SSH connection and one worker in the background so the first
        real call does not pay for them. Failures are deliberately ignored — the
        real call will retry and report properly."""
        try:
            async with self.acquire([]):
                pass
        except Exception:  # noqa: BLE001
            pass

    async def reset(self) -> str:
        n = len(self._workers)
        self._drop_conn()
        return f"dropped {n} worker(s) and the SSH connection; they will be re-created on the next call"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for c in items:
        c = c.strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out
