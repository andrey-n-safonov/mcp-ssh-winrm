"""Offline unit tests: result formatting and the worker frame protocol (fake streams)."""
import asyncio
import json

import pytest

from winrm_mcp.client import FRAME_PREFIX, HostResult, Worker, WinRMInfraError, _dedupe
from winrm_mcp.server import format_results, format_tcp

# --- fakes -----------------------------------------------------------------

class FakeReader:
    def __init__(self):
        self.q: asyncio.Queue[str] = asyncio.Queue()

    async def read(self, n):
        return await self.q.get()

    def feed(self, text):
        self.q.put_nowait(text)

    def eof(self):
        self.q.put_nowait("")


class FakeWriter:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    def write(self, data):
        for line in data.splitlines():
            self.sent.append(json.loads(line))

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self):
        self.stdout, self.stderr = FakeReader(), FakeReader()
        self._closed = asyncio.Event()

    async def wait_closed(self):
        await self._closed.wait()

    def close(self):
        self._closed.set()


def frame(obj) -> str:
    return FRAME_PREFIX + json.dumps(obj) + "\n"


async def make_worker():
    proc, reader, writer = FakeProc(), FakeReader(), FakeWriter()
    w = Worker(proc, reader, writer, 1)
    reader.feed(frame({"type": "ready", "ps": "5.1"}))
    await w.wait_ready(2)
    return w, reader, writer


# --- formatting --------------------------------------------------------------

def test_dedupe_case_insensitive_and_order():
    assert _dedupe(["A.x", "a.X", " b ", "", "A.x"]) == ["A.x", "b"]


def test_format_single_local_ok_and_stderr():
    ok = HostResult(None, "ok", stdout="hi")
    assert format_results([ok]) == ("hi", False)
    err = HostResult(None, "error", stdout="a", stderr="boom", error_count=1)
    assert format_results([err]) == ("a\n[stderr]\nboom", False)  # script error != infra failure


def test_format_timeout_is_failure_and_empty_output_marker():
    text, failed = format_results([HostResult(None, "timeout", stdout="partial")])
    assert failed and text.startswith("partial\n[TIMEOUT]")
    assert format_results([HostResult(None, "ok")]) == ("(no output)", False)


def test_format_multi_host_sections():
    rs = [HostResult("a", "ok", stdout="1", ms=1200), HostResult("b", "connect_failed", stderr="dns")]
    text, failed = format_results(rs)
    assert "=== a [ok, 1.2s] ===\n1" in text and "=== b [connect_failed" in text
    assert not failed  # one host worked
    assert format_results([HostResult("a", "connect_failed"), HostResult("b", "timeout")])[1]


def test_format_tcp_rows():
    rows = [
        {"computer": "a", "tcp": True, "tcp_ms": 5, "winrm": True, "winrm_ms": 90},
        {"computer": "b", "tcp": False, "tcp_ms": 4000, "tcp_error": "No such host"},
        {"computer": "c", "tcp": True, "tcp_ms": 6, "winrm": False, "winrm_error": "Kerberos"},
    ]
    out = format_tcp(rows, True).splitlines()
    assert "WSMan OK (90 ms)" in out[0]
    assert "CLOSED (No such host)" in out[1]
    assert "WSMan FAILED: Kerberos" in out[2]


# --- worker protocol -----------------------------------------------------------

async def test_request_roundtrip_and_stray_lines_ignored():
    w, reader, writer = await make_worker()

    async def server():
        while not writer.sent:
            await asyncio.sleep(0.01)
        fid = writer.sent[0]["id"]
        reader.feed("PS host noise that is not a frame\n")
        reader.feed(frame({"id": fid, "type": "ack"}))
        reader.feed(frame({"id": fid, "type": "result", "results": [{"status": "ok"}]}))

    t = asyncio.create_task(server())
    res = await w.request({"op": "run", "script": "1"}, deadline=2)
    await t
    assert res["results"][0]["status"] == "ok"
    assert writer.sent[0]["op"] == "run"


async def test_frame_split_across_chunks_and_large():
    w, reader, writer = await make_worker()
    big = "x" * 500_000
    line = frame({"id": 1, "type": "result", "results": [{"stdout": big}]})

    async def server():
        while not writer.sent:
            await asyncio.sleep(0.01)
        for i in range(0, len(line), 65536):  # arbitrary chunking, mid-frame
            reader.feed(line[i:i + 65536])

    t = asyncio.create_task(server())
    res = await w.request({"op": "run"}, deadline=5)
    await t
    assert len(res["results"][0]["stdout"]) == 500_000


async def test_deadline_kills_worker():
    w, reader, writer = await make_worker()
    with pytest.raises(WinRMInfraError, match="did not answer"):
        await w.request({"op": "run"}, deadline=0.2)
    assert w.dead and writer.closed


async def test_connection_loss_fails_pending_request():
    w, reader, writer = await make_worker()
    t = asyncio.create_task(w.request({"op": "run"}, deadline=5))
    await asyncio.sleep(0.05)
    reader.eof()
    with pytest.raises(WinRMInfraError, match="closed"):
        await t
    assert w.dead


async def test_cancel_sends_cancel_frame_then_collects_result():
    w, reader, writer = await make_worker()
    t = asyncio.create_task(w.request({"op": "run"}, deadline=30))
    await asyncio.sleep(0.05)
    t.cancel()

    async def server():
        while not any(m.get("op") == "cancel" for m in writer.sent):
            await asyncio.sleep(0.01)
        reader.feed(frame({"id": writer.sent[0]["id"], "type": "result", "results": []}))

    s = asyncio.create_task(server())
    with pytest.raises(asyncio.CancelledError):
        await t
    await s
    assert not w.dead  # worker stopped the script itself and stays usable


async def test_cancel_without_answer_kills_worker():
    w, reader, writer = await make_worker()
    t = asyncio.create_task(w.request({"op": "run"}, deadline=30))
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert w.dead


async def _answer_first_request(writer, reader, **fields):
    while not writer.sent:
        await asyncio.sleep(0.01)
    reader.feed(frame({"id": writer.sent[0]["id"], "type": "result", **fields}))


async def test_exiting_flag_retires_worker():
    w, reader, writer = await make_worker()
    t = asyncio.create_task(_answer_first_request(writer, reader, results=[], exiting=True))
    res = await w.request({"op": "run"}, deadline=2)
    await t
    assert res["results"] == [] and w.dead  # result delivered, worker not reused


async def test_internal_error_raises_infra_error():
    w, reader, writer = await make_worker()
    t = asyncio.create_task(_answer_first_request(writer, reader, internal_error="boom"))
    with pytest.raises(WinRMInfraError, match="boom"):
        await w.request({"op": "run"}, deadline=2)
    await t
