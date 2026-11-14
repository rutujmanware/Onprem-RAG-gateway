"""DEFECT-17: CPU-tier answers stalled ~90 s.

Causes fixed: per-process model lock held across streamed yields + kernel-chosen worker (busy or not)
+ unbounded invisible queueing + 90 s gateway timeout + streams never closed on client disconnect.
These tests use a fake engine (no 3B model load) and fake upstreams.
"""

import asyncio
import gc
import threading
import time

import pytest
from fastapi.testclient import TestClient

import pipeline.serve_cpu_slm as srv
import pipeline.rag_chain as rc


class FakeEngine:
    """Mimics CPUSLMEngine.generate_chat_stream: lock held across yields, one token per `delay`."""

    def __init__(self, n_tokens=1000, delay=0.01, fail_after=None):
        self._lock = threading.Lock()
        self.n_tokens, self.delay, self.fail_after = n_tokens, delay, fail_after
        self.produced = 0
        self.closed = threading.Event()

    def generate_chat_stream(self, messages, max_new_tokens=512, **_):
        with self._lock:
            try:
                for i in range(min(self.n_tokens, max_new_tokens)):
                    if self.fail_after is not None and i == self.fail_after:
                        raise RuntimeError("boom")
                    time.sleep(self.delay)
                    self.produced += 1
                    yield f"t{i} "
            finally:
                self.closed.set()


def run(coro):
    return asyncio.run(coro)


async def _wait(cond, timeout=2.0):
    t0 = time.time()
    while not cond():
        if time.time() - t0 > timeout:
            return False
        await asyncio.sleep(0.01)
    return True


KW = dict(messages=[{"role": "user", "content": "q"}], max_new_tokens=1000)


# ------------------------------------------------------------------ slot pool + generation stream
def test_pool_times_out_instead_of_queueing_forever():
    async def main():
        pool = srv.SlotPool([FakeEngine()])
        e = await pool.acquire(0.1)
        t0 = time.time()
        with pytest.raises(TimeoutError):
            await pool.acquire(0.2)
        assert time.time() - t0 < 1.0
        pool.release(e)
        await asyncio.sleep(0)
        assert pool.free == 1
    run(main())


def test_stream_completes_and_returns_slot():
    async def main():
        eng = FakeEngine(n_tokens=5, delay=0)
        pool = srv.SlotPool([eng])
        s = srv.GenerationStream(pool, await pool.acquire(1), KW)
        out = [d async for d in s]
        assert out == [f"t{i} " for i in range(5)]
        assert await _wait(lambda: pool.free == 1)
        assert not s.stopped_early and eng.closed.is_set()
    run(main())


def test_stop_releases_lock_and_slot_within_a_token():
    async def main():
        eng = FakeEngine(delay=0.02)
        pool = srv.SlotPool([eng])
        s = srv.GenerationStream(pool, await pool.acquire(1), KW)
        got = [await s.__anext__() for _ in range(3)]
        assert got[0] == "t0 "
        s.stop()
        assert await _wait(lambda: pool.free == 1, 1.0), "slot must come back promptly after stop"
        assert eng.closed.is_set() and not eng._lock.locked()
        assert eng.produced < 20, "generation must stop, not run to max tokens"
    run(main())


def test_consumer_cancellation_stops_generation():
    """Client disconnect = the response task is cancelled while awaiting the next token."""
    async def main():
        eng = FakeEngine(delay=0.05)
        pool = srv.SlotPool([eng])
        s = srv.GenerationStream(pool, await pool.acquire(1), KW)

        async def consume():
            async for _ in s:
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _wait(lambda: pool.free == 1, 1.0)
        assert eng.produced < 10
    run(main())


def test_sse_generator_closed_early_stops_generation():
    async def main():
        eng = FakeEngine(delay=0.02)
        pool = srv.SlotPool([eng])
        s = srv.GenerationStream(pool, await pool.acquire(1), KW)
        sse = srv._sse(s, pool)
        first = await sse.__anext__()
        assert '"content": "t0 "' in first
        await sse.aclose()  # what Starlette does when the client goes away
        assert await _wait(lambda: pool.free == 1, 1.0)
        assert pool.cancelled == 1 and pool.completed == 0
    run(main())


def test_never_iterated_stream_is_stopped_when_dropped():
    """Disconnect before the body starts: the stream object is dropped without being iterated."""
    async def main():
        eng = FakeEngine(delay=0.02)
        pool = srv.SlotPool([eng])
        s = srv.GenerationStream(pool, await pool.acquire(1), KW)
        del s
        gc.collect()
        assert await _wait(lambda: pool.free == 1, 1.0)
        assert eng.produced < 20
    run(main())


def test_engine_error_reaches_consumer_and_returns_slot():
    async def main():
        pool = srv.SlotPool([FakeEngine(fail_after=2, delay=0)])
        s = srv.GenerationStream(pool, await pool.acquire(1), KW)
        got = []
        with pytest.raises(RuntimeError, match="boom"):
            async for d in s:
                got.append(d)
        assert got == ["t0 ", "t1 "]
        assert await _wait(lambda: pool.free == 1)
    run(main())


def test_two_slots_generate_concurrently():
    async def main():
        engines = [FakeEngine(n_tokens=10, delay=0.05), FakeEngine(n_tokens=10, delay=0.05)]
        pool = srv.SlotPool(engines)

        async def one():
            s = srv.GenerationStream(pool, await pool.acquire(1), KW)
            return [d async for d in s]

        t0 = time.time()
        a, b = await asyncio.gather(one(), one())
        assert len(a) == len(b) == 10
        assert time.time() - t0 < 0.9, "two slots must not serialize (10 x 0.05 s each)"
    run(main())


# ------------------------------------------------------------------ HTTP endpoint
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(srv, "_engines", [FakeEngine(n_tokens=3, delay=0)])
    monkeypatch.setattr(srv, "QUEUE_TIMEOUT_SECONDS", 0.2)
    with TestClient(srv.app) as c:
        yield c


def test_http_stream_and_health(client):
    h = client.get("/healthz").json()
    assert h["status"] == "healthy" and h["slots_total"] == 1 and h["slots_free"] == 1
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "q"}], "stream": True})
    assert r.status_code == 200
    assert r.text.count('"content"') == 3 and r.text.rstrip().endswith("data: [DONE]")


def test_http_non_stream(client):
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "t0 t1 t2"
    assert body["usage"]["completion_tokens"] == 3


def test_http_busy_returns_503_fast(client):
    async def hold():
        return await srv.pool.acquire(1)
    eng = client.portal.call(hold)  # occupy the only slot
    try:
        t0 = time.time()
        r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "q"}], "stream": True})
        assert r.status_code == 503 and r.headers.get("retry-after") == "1"
        assert r.json()["error"]["type"] == "server_busy"
        assert time.time() - t0 < 2.0
        assert client.get("/healthz").json()["busy_rejections"] == 1
    finally:
        srv.pool.release(eng)


def test_http_empty_messages_rejected(client):
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 400


def test_launcher_is_single_process_with_workers_alias():
    src = open(srv.__file__).read()
    assert "workers=1" in src and '"--slots", "--workers"' in src


# ------------------------------------------------------------------ gateway client: fallback order
CHUNKS = [{"title": "T", "text": "Transformers are used in power systems.", "similarity": 0.9}]


class _Resp:
    status_code = 200


@pytest.fixture
def pipe(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    p = rc.RAGQAPipeline(router=None)
    return p


def _stream(p, **kw):
    return p.answer_query_stream("explain transformers", route_decision={"route": "easy"}, chunks=CHUNKS, **kw)


def _fake_upstreams(monkeypatch, slm, gpu):
    calls = []

    def fake(url, payload, headers=None, timeout=None):
        calls.append(url)
        return (slm if ":8082" in url else gpu)()
    monkeypatch.setattr(rc, "_stream_chat_http", fake)
    return calls


def _gen(tokens, fail_with=None, closed=None):
    def g():
        try:
            for t in tokens:
                yield t
            if fail_with:
                raise fail_with
        finally:
            if closed is not None:
                closed.append(True)
    return g


def test_slm_answers_normally(pipe, monkeypatch):
    _fake_upstreams(monkeypatch, _gen(["a", "b"]), _gen(["X"]))
    ev = list(_stream(pipe))
    done = ev[-1]
    assert done["type"] == "done" and done["full_answer"] == "ab"
    assert done["stats"]["server"] == "cpu-daemon-port-8082" and "incomplete" not in done


def test_slm_busy_overflows_to_gpu(pipe, monkeypatch):
    monkeypatch.setattr(rc.RAGQAPipeline, "_gpu_daemon_healthy", staticmethod(lambda: True))
    calls = _fake_upstreams(monkeypatch, _gen([], fail_with=rc.UpstreamBusy("busy")), _gen(["G1", "G2"]))
    done = list(_stream(pipe))[-1]
    assert done["full_answer"] == "G1G2"
    assert done["stats"]["server"] == "gpu-daemon-port-8000 (cpu-slm-overflow)"
    assert len(calls) == 2


def test_slm_failure_mid_answer_is_not_restarted(pipe, monkeypatch):
    monkeypatch.setattr(rc.RAGQAPipeline, "_gpu_daemon_healthy", staticmethod(lambda: True))
    calls = _fake_upstreams(monkeypatch, _gen(["a", "b"], fail_with=TimeoutError("read")), _gen(["X"]))
    ev = list(_stream(pipe))
    tokens = "".join(e["delta"] for e in ev if e["type"] == "token")
    assert tokens == "ab" + rc.PARTIAL_ANSWER_NOTE, "no second answer appended"
    assert ev[-1]["incomplete"] is True and len(calls) == 1


def test_slm_busy_and_gpu_down_uses_in_process(pipe, monkeypatch):
    monkeypatch.setattr(rc.RAGQAPipeline, "_gpu_daemon_healthy", staticmethod(lambda: False))
    _fake_upstreams(monkeypatch, _gen([], fail_with=rc.UpstreamBusy("busy")), _gen(["X"]))

    class Eng:
        model_name = "fake"

        def answer_rag_stream(self, **kw):
            yield from ["L1", "L2"]
    pipe._slm_engine = Eng()
    done = list(_stream(pipe))[-1]
    assert done["full_answer"] == "L1L2" and done["stats"]["server"] == "in-process"


def test_closing_the_answer_stream_closes_the_upstream(pipe, monkeypatch):
    closed = []
    _fake_upstreams(monkeypatch, _gen(["a", "b", "c"], closed=closed), _gen(["X"]))
    g = _stream(pipe)
    for e in g:
        if e["type"] == "token":
            break
    g.close()  # gateway closes it when the browser disconnects
    assert closed == [True]


def test_slm_stream_timeout_is_short_and_connect_fast():
    t = rc._slm_timeout()
    assert t.connect == 2.0 and t.read == 30.0
    assert rc._slm_timeout(streaming=False).read == 60.0


def test_stream_chat_http_maps_503_to_busy(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(503, json={"error": {"type": "server_busy"}})
    transport = httpx.MockTransport(handler)
    real_stream = httpx.stream
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: httpx.Client(transport=transport).stream(*a[:2], json=k.get("json")))
    with pytest.raises(rc.UpstreamBusy):
        list(rc._stream_chat_http("http://localhost:8082/v1/chat/completions", {"messages": []}))
    monkeypatch.setattr(httpx, "stream", real_stream)


# ------------------------------------------------------------------ gateway: always close streams
def test_gateway_closing_stream_runs_finally_on_disconnect():
    from gateway.main import closing_stream
    state = {"released": False, "produced": 0}

    def gen():
        try:
            for i in range(1000):
                state["produced"] += 1
                time.sleep(0.01)
                yield f"data: {i}\n\n"
        finally:
            state["released"] = True  # admission.release() in the real generators

    async def main():
        agen = closing_stream(gen())

        async def consume():
            async for _ in agen:
                pass
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state["released"], "finally must run immediately, not at garbage collection"
        assert state["produced"] < 100
    run(main())


def test_gateway_closing_stream_full_iteration():
    from gateway.main import closing_stream
    released = []

    def gen():
        try:
            yield "a"
            yield "b"
        finally:
            released.append(1)

    async def main():
        return [x async for x in closing_stream(gen())]
    assert run(main()) == ["a", "b"] and released == [1]


def test_gateway_wraps_sync_streams_and_skips_caching_incomplete_answers():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "gateway" / "main.py").read_text()
    assert "closing_stream(event_stream())" in src and "closing_stream(sse_chat_generator())" in src
    assert "may_store and full_text and not incomplete" in src
    assert "complete_text and collected_chunks and not incomplete" in src
