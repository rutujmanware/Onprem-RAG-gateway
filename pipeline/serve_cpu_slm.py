#!/usr/bin/env python3
"""OpenAI-compatible FastAPI server for Qwen 2.5 3B running on CPU.

Listens on port 8082 by default, providing endpoints:
- GET  /healthz
- GET  /v1/models
- POST /v1/chat/completions

Concurrency model (DEFECT-17, ~90 s stalls on the CPU tier):
  The server used to run N uvicorn worker processes, each with one model behind a lock that was held
  across streamed yields. The kernel handed connections to whichever worker accepted first, busy or
  not, so a request could queue invisibly behind one or two long answers until the gateway's 90 s
  timeout fired; abandoned streams kept their worker busy until garbage collection.

  Now ONE process holds a pool of N model slots (SLM_SLOTS, default 6; each slot = its own llama.cpp
  context with CPU_NUM_THREADS threads, weights shared through mmap):
    * a request takes any free slot (load-aware by construction);
    * if no slot frees up within SLM_QUEUE_TIMEOUT seconds it gets 503 + Retry-After immediately,
      so the gateway can overflow to the GPU tier instead of waiting;
    * generation runs in its own thread and is stopped within one token when the client disconnects;
      the slot is returned only after the model lock has been released.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Ensure project root is on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from pipeline.slm_runner import CPUSLMEngine, DEFAULT_MODEL_NAME

DEFAULT_SLOTS = 6
QUEUE_TIMEOUT_SECONDS = float(os.getenv("SLM_QUEUE_TIMEOUT", "3"))
MAX_TOKENS_CAP = 1024


def _slot_count() -> int:
    return max(1, int(os.getenv("SLM_SLOTS", os.getenv("SLM_WORKERS", str(DEFAULT_SLOTS)))))


class SlotPool:
    """Free model slots. acquire() waits at most `timeout` seconds; release() is thread-safe."""

    def __init__(self, engines: List[Any]):
        self.total = len(engines)
        self._free: asyncio.Queue = asyncio.Queue()
        for e in engines:
            self._free.put_nowait(e)
        self._loop = asyncio.get_running_loop()
        self.busy_rejections = 0
        self.cancelled = 0
        self.completed = 0

    @property
    def free(self) -> int:
        return self._free.qsize()

    async def acquire(self, timeout: float):
        """Raises the built-in TimeoutError when no slot frees up in time (callers catch that)."""
        if hasattr(asyncio, "timeout"):  # Python >= 3.11
            # asyncio.Queue.get never loses an item on cancellation, so a timeout cannot leak a slot.
            async with asyncio.timeout(timeout):
                return await self._free.get()
        # Python 3.10: no asyncio.timeout, and asyncio.wait_for there can drop a result that arrives just
        # as the timeout fires (a leaked slot). Wait on the get explicitly and keep any slot it obtained.
        task = asyncio.ensure_future(self._free.get())
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if task in done:
                return task.result()
            task.cancel()
            await asyncio.wait({task})  # never raises the task's own cancellation
            if not task.cancelled() and task.exception() is None:
                return task.result()  # it got a slot just before the cancel took effect: keep it
            raise TimeoutError()
        except asyncio.CancelledError:  # the caller itself was cancelled while waiting
            task.cancel()
            task.add_done_callback(self._give_back)
            raise

    def _give_back(self, task: "asyncio.Future") -> None:
        if not task.cancelled() and task.exception() is None:
            self._free.put_nowait(task.result())

    def release(self, engine: Any) -> None:
        try:
            self._loop.call_soon_threadsafe(self._free.put_nowait, engine)
        except RuntimeError:  # event loop already closed (shutdown)
            pass


_DONE = object()


class _Generation:
    """State shared with the generation thread. Holds no reference to the consumer-side stream, so a
    dropped (never iterated) GenerationStream is garbage-collected and its __del__ stops the thread."""

    def __init__(self, pool: SlotPool, engine: Any, loop: asyncio.AbstractEventLoop):
        self.pool, self.engine, self.loop = pool, engine, loop
        self.q: asyncio.Queue = asyncio.Queue()
        self.stop = threading.Event()
        self.tokens = 0
        self.stopped_early = False

    def emit(self, item: Any) -> None:
        try:
            self.loop.call_soon_threadsafe(self.q.put_nowait, item)
        except RuntimeError:  # event loop closed
            self.stop.set()

    def run(self, gen_kwargs: Dict[str, Any]) -> None:
        gen = None
        err: Optional[BaseException] = None
        try:
            gen = self.engine.generate_chat_stream(**gen_kwargs)
            for delta in gen:
                if self.stop.is_set():
                    break
                self.tokens += 1
                self.emit(delta)
        except BaseException as exc:  # noqa: BLE001 - reported to the consumer
            err = exc
        finally:
            if gen is not None:
                try:
                    gen.close()  # GeneratorExit inside the engine: lock released, llama stream closed
                except Exception:
                    pass
            if self.stop.is_set() and err is None:
                self.stopped_early = True
            self.pool.release(self.engine)  # only after the engine lock is free
            self.emit((_DONE, err))


class GenerationStream:
    """Runs one generation on a slot in a dedicated thread and exposes the deltas as an async iterator.

    stop() (also called on cancellation, early exit and garbage collection) makes the thread break out
    after the current token and close the model's generator, which releases the engine lock; only then
    is the slot returned to the pool.
    """

    def __init__(self, pool: SlotPool, engine: Any, gen_kwargs: Dict[str, Any]):
        self.t0 = time.time()
        self.error: Optional[BaseException] = None
        self._finished = False
        self._g = _Generation(pool, engine, asyncio.get_running_loop())
        thread = threading.Thread(target=self._g.run, args=(gen_kwargs,), daemon=True, name="slm-gen")
        try:
            thread.start()
        except Exception:
            pool.release(engine)
            raise

    @property
    def tokens(self) -> int:
        return self._g.tokens

    @property
    def stopped_early(self) -> bool:
        return self._g.stopped_early

    @stopped_early.setter
    def stopped_early(self, value: bool) -> None:
        self._g.stopped_early = value

    def stop(self) -> None:
        self._g.stop.set()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._finished:
            raise StopAsyncIteration
        try:
            item = await self._g.q.get()
        except BaseException:
            self.stop()
            raise
        if isinstance(item, tuple) and len(item) == 2 and item[0] is _DONE:
            self._finished = True
            if item[1] is not None:
                self.error = item[1]
                raise item[1]
            raise StopAsyncIteration
        return item

    def __del__(self):
        g = getattr(self, "_g", None)
        if g is not None:
            g.stop.set()


# Global pool (created at startup)
pool: Optional[SlotPool] = None
_engines: List[Any] = []


def _load_engines(n: int) -> List[Any]:
    threads = int(os.getenv("CPU_NUM_THREADS", "16"))
    engines = []
    for i in range(n):
        print(f"⚙️ [SLM] Loading slot {i + 1}/{n} ({threads} threads)")
        engines.append(CPUSLMEngine(num_threads=threads))
    return engines


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, _engines
    if not _engines:
        _engines = _load_engines(_slot_count())
    pool = SlotPool(_engines)
    print(f"✅ [SLM] {pool.total} slots ready (queue timeout {QUEUE_TIMEOUT_SECONDS}s)")
    yield


app = FastAPI(
    title="CPU Small Language Model (SLM) Server",
    description="OpenAI-compatible CPU inference server for Qwen 2.5 3B Instruct",
    version="1.1.0",
    lifespan=lifespan,
)


# DEFECT-45: like vLLM, the CPU model server requires a key, so other users of this shared host cannot use it
# directly (no grounding, no injection fence) or occupy all of its slots. /healthz stays open for monitoring.
SLM_API_KEY = os.getenv("SLM_API_KEY", "").strip()


def require_api_key(request: Request) -> None:
    if not SLM_API_KEY:
        return
    import hmac
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not (token and hmac.compare_digest(token, SLM_API_KEY)):
        raise HTTPException(status_code=401, detail="Missing or invalid API key")


class ChatMessage(BaseModel):
    role: str = Field(..., max_length=20)
    content: str = Field(..., max_length=40000)


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = DEFAULT_MODEL_NAME
    messages: List[ChatMessage] = Field(..., max_length=50)
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=0.8, gt=0.0, le=1.0)
    max_tokens: Optional[int] = Field(default=512, alias="max_tokens", ge=1, le=4096)
    stream: Optional[bool] = False


@app.get("/healthz")
def healthz():
    ready = pool is not None and pool.total > 0
    return {
        "status": "healthy" if ready else "initializing",
        "model": DEFAULT_MODEL_NAME,
        "device": "cpu",
        "threads": int(os.getenv("CPU_NUM_THREADS", "16")),
        "slots_total": pool.total if pool else 0,
        "slots_free": pool.free if pool else 0,
        "busy_rejections": pool.busy_rejections if pool else 0,
        "cancelled": pool.cancelled if pool else 0,
        "completed": pool.completed if pool else 0,
    }


@app.get("/v1/models", dependencies=[Depends(require_api_key)])
def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_MODEL_NAME,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "onprem-rag-gateway",
                "root": DEFAULT_MODEL_NAME,
                "parent": None,
                "permission": [],
            }
        ],
    }


def _chunk(cmpl_id: str, created: int, delta: Dict[str, Any], finish: Optional[str]) -> str:
    data = {
        "id": cmpl_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": DEFAULT_MODEL_NAME,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(data)}\n\n"


def _log_end(stream: GenerationStream, p: SlotPool) -> None:
    sec = max(time.time() - stream.t0, 0.001)
    if stream.stopped_early:
        p.cancelled += 1
        print(f"✂️ [SLM] Cancelled (client gone) after {stream.tokens} tokens in {sec:.2f}s | free slots {p.free}")
    else:
        p.completed += 1
        print(f"⚡ [SLM] Completed | {stream.tokens} tokens in {sec:.2f}s ({stream.tokens / sec:.2f} tok/s)")


async def _sse(stream: GenerationStream, p: SlotPool):
    cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    try:
        try:
            async for delta in stream:
                yield _chunk(cmpl_id, created, {"content": delta}, None)
        except Exception as err:
            print(f"⚠️ [SLM] Error during stream: {err}")
            # DEFECT-46: signal the failure (finish_reason "error"), never send the error text as answer content
            yield _chunk(cmpl_id, created, {}, "error")
            yield "data: [DONE]\n\n"
            return
        yield _chunk(cmpl_id, created, {}, "stop")
        yield "data: [DONE]\n\n"
    finally:
        if not stream._finished:  # client disconnected / response cancelled: stop generating now
            stream.stop()
            stream.stopped_early = True
        _log_end(stream, p)


def _busy_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": {"message": "CPU SLM busy: no free slot", "type": "server_busy"}},
        headers={"Retry-After": "1"},
    )


@app.post("/v1/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(req: ChatCompletionRequest):
    if pool is None:
        raise HTTPException(status_code=503, detail="SLM server initializing")
    raw_messages = [{"role": m.role, "content": m.content} for m in req.messages]
    if not raw_messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty.")

    do_sample = req.temperature is not None and req.temperature > 0
    gen_kwargs = dict(
        messages=raw_messages,
        max_new_tokens=min(req.max_tokens or 512, MAX_TOKENS_CAP),
        temperature=req.temperature if req.temperature is not None else 0.7,
        top_p=req.top_p if req.top_p is not None else 0.8,
        do_sample=do_sample,
    )
    snippet = raw_messages[-1]["content"][:60].replace("\n", " ")

    try:
        engine = await pool.acquire(QUEUE_TIMEOUT_SECONDS)
    except TimeoutError:
        pool.busy_rejections += 1
        print(f"🚦 [SLM] Busy: no free slot in {QUEUE_TIMEOUT_SECONDS}s -> 503 | Prompt: \"{snippet}...\"")
        return _busy_response()

    print(f"\n📥 [SLM] Request | Stream: {req.stream} | Tokens: {gen_kwargs['max_new_tokens']} | "
          f"free slots {pool.free}/{pool.total} | Prompt: \"{snippet}...\"")
    stream = GenerationStream(pool, engine, gen_kwargs)

    if req.stream:
        return StreamingResponse(_sse(stream, pool), media_type="text/event-stream")

    parts: List[str] = []
    try:
        async for delta in stream:
            parts.append(delta)
    except Exception as e:
        print(f"⚠️ [SLM] Generation failed: {e}")
        raise HTTPException(status_code=500, detail="Generation failed")
    finally:
        stream.stop()
        _log_end(stream, pool)

    text = "".join(parts).strip()
    sec = max(time.time() - stream.t0, 0.001)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": os.path.basename(str(DEFAULT_MODEL_NAME)),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": -1, "completion_tokens": stream.tokens, "total_tokens": stream.tokens},
        "system_fingerprint": f"cpu-{round(stream.tokens / sec, 2)}tok_s",
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Start CPU SLM API Server")
    parser.add_argument("--host", default=os.getenv("SLM_SERVER_HOST", "127.0.0.1"), help="Host to bind (default: 127.0.0.1 - localhost only for security)")
    parser.add_argument("--port", type=int, default=8082, help="Port to listen (default: 8082)")
    parser.add_argument("--slots", "--workers", dest="slots", type=int, default=_slot_count(),
                        help="Model slots in the single server process (--workers kept as an alias)")
    parser.add_argument("--threads", type=int, default=int(os.getenv("CPU_NUM_THREADS", "16")), help="CPU threads per slot")
    args = parser.parse_args()

    os.environ["CPU_NUM_THREADS"] = str(args.threads)
    os.environ["SLM_SLOTS"] = str(args.slots)
    print(f"🚀 Starting CPU SLM Server on http://{args.host}:{args.port} (Slots: {args.slots}, Threads/slot: {args.threads})")
    # One process on purpose: the slot pool is the scheduler (see module docstring).
    uvicorn.run(app, host=args.host, port=args.port, workers=1, reload=False, proxy_headers=False)  # DEFECT-44


if __name__ == "__main__":
    main()
