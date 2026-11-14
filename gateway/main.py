import asyncio
import json
import logging
import os
import sys
import time
import uuid
import secrets
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

# Ensure project root is in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, "smtp.env"), override=False)
    load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)
except ImportError:
    pass

from gateway.auth.guard import UserIdentity, require_iisc_user
from gateway.cache.redis_semantic_cache import RedisSemanticCache
from gateway.queue.admission import AdmissionController
from gateway.dependencies import (
    container, get_semantic_cache, get_complexity_scorer,
    get_dispatcher, get_admission_controller, get_slm_client, get_vllm_client
)
from gateway.router.complexity_scorer import ComplexityScorer
from gateway.router.dispatcher import ModelTier, RequestDispatcher
from gateway.clients.cpu_slm_client import CPUSLMClient
from gateway.clients.vllm_client import VLLMClient
from pipeline.rag_chain import RAGQAPipeline
from pipeline.retriever import sanitize_chunks
from pipeline.sanitizer import sanitize_and_fence_prompt, PromptInjectionAlert, is_no_relevant_docs_answer

logger = logging.getLogger("gateway.main")


_STREAM_END = object()


async def closing_stream(gen):
    """Serve a sync generator from a worker thread and ALWAYS close it, client disconnect included.

    Starlette iterates sync generators in a thread but never closes them when the browser goes away,
    so their finally blocks (admission.release(), closing the upstream model stream) only ran at
    garbage collection, leaving model slots busy (DEFECT-17). Closing here runs them immediately.
    """
    import threading
    import anyio
    lock = threading.Lock()  # close() must wait for a next() still running in its thread

    def step():
        with lock:
            return next(gen, _STREAM_END)

    def close():
        with lock:
            gen.close()

    try:
        while True:
            item = await anyio.to_thread.run_sync(step)
            if item is _STREAM_END:
                return
            yield item
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(close)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize container (creates Redis semantic cache index, etc.)
    try:
        await container.initialize()
        # Sanitize Redis cache on startup: purge any uncacheable entries (insufficient data, refusals)
        cache = container.semantic_cache
        purged = await cache.apurge_uncacheable_entries()
        if purged > 0:
            logger.info(f"Startup sanitized Redis cache: purged {purged} uncacheable entries.")
    except Exception as e:
        logger.warning(f"Container initialization warning: {e}")
    # Load router, embedder, retriever and spell dictionaries now, not on a user's first question
    # (the spell dictionaries alone take ~2.5 s per worker process).
    try:
        from pipeline.rag_chain import warm_up
        t_warm = time.time()
        await run_in_threadpool(warm_up)
        logger.info(f"Retrieval warm-up done in {time.time() - t_warm:.2f}s")
    except Exception as e:
        logger.warning(f"Retrieval warm-up skipped: {e}")
    yield
    try:
        await container.shutdown()
    except Exception:
        pass

ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "0").lower() in ("1", "true", "yes")

app = FastAPI(
    title="AskIISc Campus Intelligence Platform",
    description="AskIISc On-Premise Institutional RAG Gateway",
    version="2.0.0",
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
    lifespan=lifespan
)

ADMIN_KEY = os.getenv("ADMIN_API_KEY", "")

def require_internal_or_auth(request: Request):
    """
    Ensure internal/admin endpoints (/query, /cache/*) cannot be accessed
    by arbitrary external users. Returns 404 to hide the endpoint's existence.
    Allowed callers:
    1. Direct loopback / localhost (127.0.0.1, ::1, testclient)
    2. Valid authenticated web session (IISc user logged into AskIISc)
    3. Valid X-Admin-Key / Bearer header matching ADMIN_API_KEY (if configured)
    """
    client_host = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")

    # Direct loopback without external forwarder
    # Security note (VULN-01 fix): Never trust client-supplied X-Forwarded-For headers
    # to authenticate loopback access (CWE-290). Only direct local socket connections
    # without forwarding headers are permitted unauthenticated loopback access.
    if not xff and client_host in ("127.0.0.1", "::1", "localhost", "testclient"):
        return True

    # Allow authenticated web session
    if "session" in request.scope:
        user = request.session.get("user")
        if user and user.get("exp", 0) > time.time():
            return True

    # Allow admin API key (VULN-08: constant-time comparison prevents timing side-channels)
    if ADMIN_KEY:
        auth_hdr = request.headers.get("X-Admin-Key") or request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if auth_hdr and secrets.compare_digest(auth_hdr, ADMIN_KEY):
            return True

    raise HTTPException(status_code=404, detail="Not Found")

# Session Security (VULN-02 Remediation):
# Enforce high-entropy cryptographic session secret. Never allow known weak or fallback keys.
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
KNOWN_INSECURE_SECRETS = {
    "e7b4c91a3f8d2e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a",
    "CHANGE_ME",
    "secret",
    "test-secret",
    "supersecret",
}

if not SESSION_SECRET or SESSION_SECRET in KNOWN_INSECURE_SECRETS or len(SESSION_SECRET) < 32:
    if os.getenv("ENVIRONMENT", "").lower() in ("production", "prod"):
        raise RuntimeError(
            "FATAL: Insecure or missing SESSION_SECRET in production. "
            "Generate a secure 256-bit key using: python3 -c 'import secrets; print(secrets.token_hex(32))'"
        )
    logger.warning(
        "SESSION_SECRET is unset or uses an insecure default. Generating an ephemeral high-entropy random key."
    )
    SESSION_SECRET = secrets.token_hex(32)

from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=os.getenv("COOKIE_SECURE", "0") == "1",
    same_site="lax",
    max_age=int(os.getenv("SESSION_TTL", str(8 * 3600))),
    session_cookie="askiisc_session",
)

class ChatMessage(BaseModel):
    role: str = Field(..., max_length=50)
    content: str = Field(..., min_length=1, max_length=4000)

class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., min_length=1, max_length=50)
    temperature: Optional[float] = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=8192)
    stream: Optional[bool] = False

class DirectQueryRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=1500)
    top_k: Optional[int] = Field(default=None, ge=1, le=50)
    temperature: Optional[float] = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=2048, ge=1, le=8192)
    stream: Optional[bool] = False
    bypass_cache: Optional[bool] = False
    # Single-use token issued only with a cached answer; requests a fresh answer for that same question.
    regenerate_token: Optional[str] = Field(default=None, max_length=128)

@app.get("/healthz", include_in_schema=False)
@app.get("/health", include_in_schema=False)
async def healthz():
    return {"status": "ok"}

@app.get("/cache/stats", dependencies=[Depends(require_internal_or_auth)], include_in_schema=False)
async def cache_stats(cache: RedisSemanticCache = Depends(get_semantic_cache)):
    count = await cache.acount()
    return {
        "status": "active",
        "cache_type": "RedisSemanticCache",
        "index_name": cache.index_name,
        "key_prefix": cache.key_prefix,
        "similarity_threshold": cache.similarity_threshold,
        "regenerations_logged": cache.regen_event_count(),
        "vector_dim": cache.vector_dim,
        "cached_queries_count": count,
    }

class CachePurgeRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=1500)
    threshold: Optional[float] = Field(default=0.85, ge=0.5, le=1.0)

@app.post("/cache/purge", dependencies=[Depends(require_internal_or_auth)], include_in_schema=False)
async def cache_purge_query(
    req: CachePurgeRequest,
    cache: RedisSemanticCache = Depends(get_semantic_cache),
):
    purged = await cache.apurge(req.query, threshold=req.threshold or 0.85)
    return {
        "status": "ok",
        "purged_count": purged,
        "query": req.query,
        "threshold": req.threshold or 0.85,
    }

@app.post("/cache/clear", dependencies=[Depends(require_internal_or_auth)], include_in_schema=False)
async def cache_clear(cache: RedisSemanticCache = Depends(get_semantic_cache)):
    success = await cache.aclear()
    return {"status": "cleared" if success else "failed"}

@app.post("/cache/purge-invalid", dependencies=[Depends(require_internal_or_auth)], include_in_schema=False)
async def cache_purge_invalid(cache: RedisSemanticCache = Depends(get_semantic_cache)):
    purged = await cache.apurge_uncacheable_entries()
    return {"status": "ok", "purged_count": purged}

@app.get("/queue/stats", dependencies=[Depends(require_internal_or_auth)], include_in_schema=False)
async def queue_stats(admission: AdmissionController = Depends(get_admission_controller)):
    return admission.get_stats()

@app.post("/v1/chat/completions", dependencies=[Depends(require_internal_or_auth)], include_in_schema=False)
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    user: UserIdentity = Depends(require_iisc_user),
    cache: RedisSemanticCache = Depends(get_semantic_cache),
    scorer: ComplexityScorer = Depends(get_complexity_scorer),
    dispatcher: RequestDispatcher = Depends(get_dispatcher),
    slm: CPUSLMClient = Depends(get_slm_client),
    vllm: VLLMClient = Depends(get_vllm_client),
    admission: AdmissionController = Depends(get_admission_controller),
):
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty")

    raw_query = req.messages[-1].content.strip()
    if not raw_query or len(raw_query) < 2:
        raise HTTPException(status_code=400, detail="Query message must contain at least 2 non-whitespace characters")
    # VULN-04: Validate prompt against adversarial injection & jailbreak patterns
    try:
        sanitize_and_fence_prompt(raw_query)
    except PromptInjectionAlert as e:
        raise HTTPException(status_code=400, detail=str(e))

    from pipeline.text_processing.typo_normalizer import TypoNormalizer
    query = TypoNormalizer.get_instance().normalize(raw_query)

    t0 = time.time()

    # Identify caller for token-bucket and queue accounting
    client_host = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    client_id = client_host or "anonymous"

    internal_user = request.headers.get("x-internal-user", "").strip()
    if internal_user and client_host in ("127.0.0.1", "::1", "localhost", "testclient") and not xff:
        client_id = internal_user
    elif "session" in request.scope:
        u = request.session.get("user")
        if u and isinstance(u, dict) and u.get("email"):
            client_id = u["email"]
    elif user and getattr(user, "email", None):
        client_id = user.email

    # Step 0: Semantic Vector Cache Lookup
    try:
        hit = await cache.alookup(query, owner=client_id)
        if hit is None and query != raw_query:
            hit = await cache.alookup(raw_query, owner=client_id)
    except Exception:
        hit = None

    if hit is not None:
        cached_resp = hit.get("response", {})
        if isinstance(cached_resp, str):
            try:
                cached_resp = json.loads(cached_resp)
            except Exception:
                cached_resp = {"answer": cached_resp}
        ans = cached_resp.get("answer", "")
        sim = hit.get("similarity", 1.0)
        cmpl_id = f"chatcmpl-cached-{uuid.uuid4().hex[:8]}"

        if req.stream:
            def cached_chat_stream():
                words = ans.split(" ")
                for i, w in enumerate(words):
                    delta = w if i == len(words) - 1 else w + " "
                    chunk = {
                        "id": cmpl_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "redis-semantic-cache",
                        "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                final_chunk = {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "redis-semantic-cache",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                cached_chat_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        return {
            "id": cmpl_id,
            "object": "chat.completion",
            "model": "redis-semantic-cache",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": ans},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": len(ans.split()),
                "total_tokens": len(ans.split()),
            },
            "router_meta": {
                "score": 0.0,
                "tier": "semantic_cache",
                "route": "cached",
                "cached": True,
                "similarity": sim,
                "matched_prompt": hit.get("matched_prompt", query),
                "routing_time_seconds": round(time.time() - t0, 4),
            },
            "user": user.email,
        }

    # Step 1: Neural Complexity Routing (MLP Router)
    score = await scorer.score(query)
    decision = dispatcher.decide(score, query=query)

    if req.stream:
        await admission.acquire(client_id)
        rag = RAGQAPipeline()
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())

        def sse_chat_generator():
            try:
                full_ans = []
                collected_chunks = []
                incomplete = False
                for event in rag.answer_query_stream(
                    query,
                    max_new_tokens=req.max_tokens,
                    temperature=req.temperature or 0.2,
                ):
                    if event.get("type") == "token":
                        delta_text = event.get("delta", "")
                        full_ans.append(delta_text)
                        chunk = {
                            "id": cmpl_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": "qwen2.5-3b-instruct-cpu" if decision.target_tier != ModelTier.GPU_LLM else "qwen2.5-7b-instruct-gpu",
                            "choices": [{
                                "index": 0,
                                "delta": {"content": delta_text},
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                    elif event.get("type") == "chunks":
                        collected_chunks = event.get("chunks", [])
                    elif event.get("type") == "done":
                        incomplete = bool(event.get("incomplete"))
                        final_chunk = {
                            "id": cmpl_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": "qwen2.5-3b-instruct-cpu" if decision.target_tier != ModelTier.GPU_LLM else "qwen2.5-7b-instruct-gpu",
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }],
                        }
                        yield f"data: {json.dumps(final_chunk)}\n\n"

                # Save generated completion to cache only when document chunks were retrieved
                complete_text = "".join(full_ans)
                if complete_text and collected_chunks and not incomplete:
                    try:
                        cache.store(query, {"answer": complete_text, "chunks": collected_chunks}, owner=client_id)
                        if query != raw_query:
                            cache.store(raw_query, {"answer": complete_text, "chunks": collected_chunks}, owner=client_id)
                    except Exception:
                        pass
                yield "data: [DONE]\n\n"
            finally:
                admission.release()

        return StreamingResponse(
            closing_stream(sse_chat_generator()),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Cold non-streaming generation: guarded by admission slot
    async with admission.slot(client_id):
        # Step 2: Route Enforcement for Hard Queries -> GPU LLM Tier (Qwen 2.5 7B)
        if decision.target_tier == ModelTier.GPU_LLM:
            gpu_max_tokens = req.max_tokens if (req.max_tokens is not None and req.max_tokens > 0) else 2048
            vllm_up = await vllm.check_health()
            if vllm_up:
                try:
                    resp = await vllm.generate_chat(
                        [{"role": m.role, "content": m.content} for m in req.messages],
                        max_tokens=gpu_max_tokens,
                        temperature=req.temperature or 0.2,
                    )
                    resp["router_meta"] = {
                        "score": round(score, 4),
                        "tier": decision.target_tier.value,
                        "route": "hard",
                        "reason": decision.reason,
                        "routing_time_seconds": round(time.time() - t0, 4),
                    }
                    resp["user"] = user.email
                    ans_text = resp["choices"][0]["message"]["content"]
                    await cache.astore(query, {"answer": ans_text}, owner=client_id)
                    return resp
                except Exception:
                    pass

            # In-process GPU LLM fallback
            try:
                from pipeline.gpu_llm_runner import GPULLMEngine
                engine = GPULLMEngine.get_instance()
                gpu_res = engine.generate_chat(
                    [{"role": m.role, "content": m.content} for m in req.messages],
                    max_new_tokens=gpu_max_tokens,
                    temperature=req.temperature or 0.2,
                )
                ans_text = gpu_res["text"]
                await cache.astore(query, {"answer": ans_text}, owner=client_id)
                if query != raw_query:
                    await cache.astore(raw_query, {"answer": ans_text}, owner=client_id)
                return {
                    "id": f"chatcmpl-hard-{uuid.uuid4().hex[:8]}",
                    "object": "chat.completion",
                    "model": "Qwen/Qwen2.5-7B-Instruct",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": ans_text},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": -1,
                        "completion_tokens": gpu_res["num_tokens"],
                        "total_tokens": gpu_res["num_tokens"],
                    },
                    "router_meta": {
                        "score": round(score, 4),
                        "tier": decision.target_tier.value,
                        "route": "hard",
                        "reason": decision.reason,
                        "routing_time_seconds": round(time.time() - t0, 4),
                    },
                    "user": user.email,
                }
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"GPU LLM generation failed: {str(e)}")

        # Step 3: Easy Queries -> CPU SLM Tier (Qwen 2.5 3B)
        slm_max_tokens = req.max_tokens if (req.max_tokens is not None and req.max_tokens > 0) else 160
        slm_up = await slm.check_health()
        if slm_up:
            try:
                resp = await slm.generate_chat(
                    [{"role": m.role, "content": m.content} for m in req.messages],
                    max_tokens=slm_max_tokens,
                    temperature=req.temperature or 0.2,
                )
                resp["router_meta"] = {
                    "score": round(score, 4),
                    "tier": decision.target_tier.value,
                    "route": "easy",
                    "reason": decision.reason,
                    "routing_time_seconds": round(time.time() - t0, 4),
                }
                ans_text = resp["choices"][0]["message"]["content"]
                await cache.astore(query, {"answer": ans_text}, owner=client_id)
                if query != raw_query:
                    await cache.astore(raw_query, {"answer": ans_text}, owner=client_id)
                return resp
            except Exception:
                pass

        # Fallback attempt: In-process RAG QA Pipeline
        rag = RAGQAPipeline()
        rag_res = await run_in_threadpool(
            rag.answer_query,
            query,
            max_new_tokens=slm_max_tokens,
            temperature=req.temperature or 0.2,
        )
        ans_text = rag_res.get("answer", "")
        retrieved_chunks = rag_res.get("chunks", [])
        if retrieved_chunks:
            await cache.astore(query, {"answer": ans_text, "chunks": retrieved_chunks}, owner=client_id)
        return {
            "id": f"chatcmpl-slm-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "model": "qwen2.5-3b-instruct-cpu",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": ans_text,
                },
                "finish_reason": "stop",
            }],
            "router_meta": {
                "score": round(score, 4),
                "tier": decision.target_tier.value,
                "route": "easy",
                "reason": decision.reason,
                "chunks_retrieved": len(rag_res.get("chunks", [])),
                "routing_time_seconds": round(time.time() - t0, 4),
            },
            "user": user.email,
        }


@app.post("/query", dependencies=[Depends(require_internal_or_auth)], include_in_schema=False)
async def query_endpoint(
    req: DirectQueryRequest,
    request: Request,
    cache: RedisSemanticCache = Depends(get_semantic_cache),
    scorer: ComplexityScorer = Depends(get_complexity_scorer),
    dispatcher: RequestDispatcher = Depends(get_dispatcher),
    vllm: VLLMClient = Depends(get_vllm_client),
    admission: AdmissionController = Depends(get_admission_controller),
):
    raw_query = req.query.strip()
    if len(raw_query) < 2:
        raise HTTPException(status_code=400, detail="Query must contain at least 2 non-whitespace characters")
    # VULN-04: Validate prompt against adversarial injection & jailbreak patterns
    try:
        sanitize_and_fence_prompt(raw_query)
    except PromptInjectionAlert as e:
        raise HTTPException(status_code=400, detail=str(e))

    from pipeline.text_processing.typo_normalizer import TypoNormalizer
    query = TypoNormalizer.get_instance().normalize(raw_query)

    t0 = time.time()

    # Identify caller for token-bucket and queue accounting
    client_host = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    client_id = client_host or "anonymous"

    # VULN-03: If forwarded by internal web chat proxy over direct loopback, attribute to the user
    internal_user = request.headers.get("x-internal-user", "").strip()
    if internal_user and client_host in ("127.0.0.1", "::1", "localhost", "testclient") and not xff:
        client_id = internal_user
    elif "session" in request.scope:
        u = request.session.get("user")
        if u and isinstance(u, dict) and u.get("email"):
            client_id = u["email"]

    # "Regenerate": only valid with a token that was issued together with a cached answer to this user
    # for this exact question. Capped per user; regenerating a SHARED answer only creates a private
    # override, and the shared entry is evicted once enough distinct users regenerate it.
    regen = None
    if req.regenerate_token:
        if not await cache.aregen_quota_ok(client_id):
            raise HTTPException(status_code=429, detail="Regenerate limit reached. Please try again later.")
        regen = await cache.aconsume_regen_token(req.regenerate_token, client_id, raw_query)
        if regen is None:
            raise HTTPException(status_code=403, detail="Regenerate is only available for an answer that was served from the cache.")
        evicted = await cache.arecord_shared_regen(regen["matched_prompt"], client_id) if regen["kind"] == "exact" else False
        await cache.alog_regen_event(regen, client_id, evicted)
    regen_kind = regen["kind"] if regen else None
    use_cache = (not req.bypass_cache) and regen is None
    may_store = (not req.bypass_cache) or regen is not None

    # Step 0: Check Semantic Vector Cache in Redis (Fast path < 5ms bypasses admission queue)
    hit = None
    if use_cache:
        try:
            hit = await cache.alookup(query, owner=client_id)
            if hit is None and query != raw_query:
                hit = await cache.alookup(raw_query, owner=client_id)
        except Exception:
            hit = None

    if hit is not None:
        cached_resp = hit.get("response", {})
        if isinstance(cached_resp, str):
            try:
                cached_resp = json.loads(cached_resp)
            except Exception:
                cached_resp = {"answer": cached_resp}

        sim = hit.get("similarity", 1.0)
        matched_prompt = hit.get("matched_prompt", query)
        cache_kind = "override" if hit.get("override") else ("exact" if hit.get("fast_exact") else "semantic")
        regen_token = await cache.aissue_regen_token(client_id, raw_query, cache_kind, matched_prompt, sim)

        if req.stream:
            def cached_stream():
                # Yield cache hit notification event
                yield f"data: {json.dumps({'type': 'cache', 'status': 'HIT', 'similarity': sim, 'matched_prompt': matched_prompt, 'badge': 'Cached answer', 'cache_kind': cache_kind, 'regenerate_token': regen_token, 'cached_at': cached_resp.get('cached_at', time.time())})}\n\n"
                # Yield route event
                yield f"data: {json.dumps({'type': 'route', 'route': cached_resp.get('route', 'cached'), 'target_tier': 'semantic_cache', 'probability_hard': 0.0})}\n\n"
                # Yield chunks event
                chunks = sanitize_chunks(cached_resp.get("chunks", []))
                yield f"data: {json.dumps({'type': 'chunks', 'chunks': chunks, 'retrieval_seconds': 0.001})}\n\n"
                # Stream cached answer tokens
                ans = cached_resp.get("answer", "")
                words = ans.split(" ")
                for i, w in enumerate(words):
                    delta = w if i == len(words) - 1 else w + " "
                    yield f"data: {json.dumps({'type': 'token', 'delta': delta})}\n\n"
                # Yield completion stats event
                yield f"data: {json.dumps({'type': 'done', 'cached': True, 'similarity': sim, 'matched_prompt': matched_prompt, 'stats': {'tokens_per_second': 9999.0, 'total_seconds': round(time.time() - t0, 4), 'cached': True}})}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                cached_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # Non-streaming cache hit
        if "chunks" in cached_resp:
            cached_resp["chunks"] = sanitize_chunks(cached_resp["chunks"])
        cached_resp["cached"] = True
        cached_resp["cache_hit"] = True
        cached_resp["badge"] = "Cached answer"
        cached_resp["cache_kind"] = cache_kind
        cached_resp["regenerate_token"] = regen_token
        cached_resp["query"] = raw_query
        if query != raw_query:
            cached_resp["normalized_query"] = query
        cached_resp["provenance"] = {
            "status": "cached",
            "cached_at": cached_resp.get("cached_at", time.time()),
            "similarity": sim,
            "model_version": cached_resp.get("model_version", "institution-rag-v1"),
        }
        cached_resp["similarity"] = sim
        cached_resp["matched_prompt"] = matched_prompt
        cached_resp["routing_time_seconds"] = round(time.time() - t0, 4)
        return cached_resp

    # Cache Miss: Cold inference required. Guarded by Admission Queue Manager.
    if req.stream:
        await admission.acquire(client_id)
        rag = RAGQAPipeline(top_k=req.top_k)

        def event_stream():
            try:
                collected_answer = []
                collected_chunks = []
                route_info = {}
                incomplete = False
                for event in rag.answer_query_stream(
                    query,
                    top_k=req.top_k,
                    max_new_tokens=req.max_tokens,
                    temperature=req.temperature or 0.2,
                ):
                    etype = event.get("type")
                    if etype == "token":
                        collected_answer.append(event.get("delta", ""))
                    elif etype == "chunks":
                        event["chunks"] = sanitize_chunks(event.get("chunks", []))
                        collected_chunks = event["chunks"]
                    elif etype == "route":
                        route_info = event
                    elif etype == "done":
                        incomplete = bool(event.get("incomplete"))
                    yield f"data: {json.dumps(event)}\n\n"

                full_text = "".join(collected_answer)
                if is_no_relevant_docs_answer(full_text):
                    collected_chunks = []
                    yield f"data: {json.dumps({'type': 'chunks', 'chunks': []})}\n\n"

                # Only cache responses when document chunks were successfully retrieved and not a refusal
                if may_store and full_text and not incomplete and collected_chunks and not is_no_relevant_docs_answer(full_text):
                    try:
                        cache_payload = {
                            "query": raw_query,
                            "answer": full_text,
                            "route": route_info.get("route", "easy"),
                            "target_tier": route_info.get("target_tier", "cpu_slm"),
                            "chunks": collected_chunks,
                        }
                        if query != raw_query:
                            cache_payload["normalized_query"] = query
                        cache.store(query, cache_payload, owner=client_id, regen_kind=regen_kind)
                        if query != raw_query:
                            cache.store(raw_query, cache_payload, owner=client_id, regen_kind=regen_kind)
                    except Exception:
                        pass
                yield "data: [DONE]\n\n"
            finally:
                admission.release()

        return StreamingResponse(
            closing_stream(event_stream()),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming cold inference: wrap inside admission slot
    async with admission.slot(client_id):
        from pipeline.retriever import RedisChunkRetriever
        from pipeline.gpu_llm_runner import GPULLMEngine

        retriever = RedisChunkRetriever()
        t0_pre = time.time()

        # Step 1: Concurrent Fan-Out (Parallel Scoring + Async Retrieval)
        score_task = asyncio.create_task(scorer.score(query))
        retrieval_task = asyncio.create_task(retriever.asearch(query, top_k=req.top_k))

        score, candidate_chunks = await asyncio.gather(score_task, retrieval_task)
        pre_duration = round(time.time() - t0_pre, 3)

        decision = dispatcher.decide(score, query=query)
        is_hard = (decision.target_tier == ModelTier.GPU_LLM)

        # Step 2: Dynamic In-Memory Tier Document Slicing
        is_catalog = bool(candidate_chunks and candidate_chunks[0].get("doc_id") == "catalog_summary")
        if is_catalog:
            chunks = candidate_chunks
        elif req.top_k is not None:
            chunks = candidate_chunks[:req.top_k]
        elif is_hard:
            chunks = candidate_chunks[:8]  # Up to 8 docs for GPU LLM 7B
        else:
            chunks = candidate_chunks[:3]  # Up to 3 docs for CPU SLM 3B

        if not chunks:
            refusal_text = "No specific institutional repository theses were found on this topic."
            res = {
                "query": raw_query,
                "answer": refusal_text,
                "route": "hard" if is_hard else "easy",
                "target_tier": "gpu_vllm" if is_hard else "cpu_slm",
                "model": "retriever-filter",
                "score": round(score, 4),
                "chunks": [],
                "cached": False,
                "stats": {
                    "retrieval_seconds": pre_duration,
                    "routing_time_seconds": round(time.time() - t0, 4),
                },
            }
            if query != raw_query:
                res["normalized_query"] = query
            return res

        if decision.target_tier == ModelTier.GPU_LLM:
            gpu_max_tokens = req.max_tokens if (req.max_tokens is not None and req.max_tokens > 0) else 2048

            vllm_up = await vllm.check_health()
            if vllm_up:
                try:
                    messages = GPULLMEngine.build_rag_messages(query, chunks)
                    resp = await vllm.generate_chat(
                        messages,
                        max_tokens=gpu_max_tokens,
                        temperature=req.temperature or 0.2,
                    )
                    answer_text = resp["choices"][0]["message"]["content"]
                    served_chunks = [] if is_no_relevant_docs_answer(answer_text) else sanitize_chunks(chunks)
                    res = {
                        "query": raw_query,
                        "answer": answer_text,
                        "route": "hard",
                        "target_tier": "gpu_vllm",
                        "model": resp.get("model", "Qwen/Qwen2.5-7B-Instruct"),
                        "score": round(score, 4),
                        "chunks": served_chunks,
                        "cached": False,
                        "stats": {
                            "retrieval_seconds": pre_duration,
                            "routing_time_seconds": round(time.time() - t0, 4),
                        },
                    }
                    if query != raw_query:
                        res["normalized_query"] = query
                    if may_store and served_chunks and not is_no_relevant_docs_answer(answer_text):
                        await cache.astore(query, res, owner=client_id, regen_kind=regen_kind)
                        if query != raw_query:
                            await cache.astore(raw_query, res, owner=client_id, regen_kind=regen_kind)
                    return res
                except Exception:
                    pass

            rag = RAGQAPipeline(top_k=req.top_k)
            res = rag.answer_query(
                query,
                top_k=req.top_k,
                max_new_tokens=gpu_max_tokens,
                temperature=req.temperature or 0.2,
                route_decision={"route": "hard", "target_tier": "gpu_vllm", "probability_hard": score},
                chunks=chunks,
            )
            if "chunks" in res:
                if is_no_relevant_docs_answer(res.get("answer", "")):
                    res["chunks"] = []
                else:
                    res["chunks"] = sanitize_chunks(res["chunks"])
            res["query"] = raw_query
            if query != raw_query:
                res["normalized_query"] = query
            res["cached"] = False
            res["routing_time_seconds"] = round(time.time() - t0, 4)
            if may_store and res.get("chunks") and not is_no_relevant_docs_answer(res.get("answer", "")):
                await cache.astore(query, res, owner=client_id, regen_kind=regen_kind)
                if query != raw_query:
                    await cache.astore(raw_query, res, owner=client_id, regen_kind=regen_kind)
            return res

        # Route: Easy -> CPU SLM
        slm_max_tokens = req.max_tokens if (req.max_tokens is not None and req.max_tokens > 0) else 1024
        rag = RAGQAPipeline(top_k=req.top_k)
        res = await run_in_threadpool(
            rag.answer_query,
            query,
            top_k=req.top_k,
            max_new_tokens=slm_max_tokens,
            temperature=req.temperature or 0.2,
            route_decision={"route": "easy", "target_tier": "cpu_slm", "probability_hard": score},
            chunks=chunks,
        )
        if "chunks" in res:
            if is_no_relevant_docs_answer(res.get("answer", "")):
                res["chunks"] = []
            else:
                res["chunks"] = sanitize_chunks(res["chunks"])
        res["query"] = raw_query
        if query != raw_query:
            res["normalized_query"] = query
        res["route"] = "easy"
        res["target_tier"] = "cpu_slm"
        res["score"] = round(score, 4)
        res["cached"] = False
        res["routing_time_seconds"] = round(time.time() - t0, 4)
        if may_store and res.get("chunks") and not is_no_relevant_docs_answer(res.get("answer", "")):
            await cache.astore(query, res, owner=client_id, regen_kind=regen_kind)
            if query != raw_query:
                await cache.astore(raw_query, res, owner=client_id, regen_kind=regen_kind)
        return res


# ---------------------------------------------------------------------------
# Frontend Web Chat UI (Mounted from askiisc_chat with Session Auth)
# ---------------------------------------------------------------------------
STATIC_DIR = Path(__file__).resolve().parent.parent / "askiisc_chat" / "static"
if STATIC_DIR.exists():
    import sys
    from fastapi import Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    chat_dir = str(Path(__file__).resolve().parent.parent / "askiisc_chat")
    if chat_dir not in sys.path:
        sys.path.insert(0, chat_dir)
    from askiisc import auth, config as C
    from askiisc.gateway_backend import stream_via_gateway

    class OtpReq(BaseModel):
        email: str = Field(max_length=300)

    class OtpVerify(BaseModel):
        email: str = Field(max_length=300)
        code: str = Field(max_length=16)

    class ChatRequest(BaseModel):
        message: str = Field(..., min_length=1, max_length=1500)
        history: Optional[List[Dict[str, Any]]] = Field(default=[], max_length=50)
        thesis_id: Optional[str] = Field(default=None, max_length=100)
        force: Optional[str] = Field(default=None, max_length=20)
        regenerate_token: Optional[str] = Field(default=None, max_length=128)

    CSRF_COOKIE_NAME = "csrftoken"
    CSRF_HEADER_NAME = "x-csrf-token"

    def generate_csrf_token() -> str:
        return secrets.token_urlsafe(32)

    def set_csrf_cookie(response: Any, token: str, request: Optional[Request] = None):
        is_secure = (os.getenv("COOKIE_SECURE", "0") == "1") or (
            request is not None and (
                request.url.scheme == "https" or
                request.headers.get("x-forwarded-proto") == "https"
            )
        )
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=token,
            max_age=int(os.getenv("SESSION_TTL", str(8 * 3600))),
            httponly=False,
            samesite="lax",
            secure=is_secure,
            path="/",
        )

    def verify_csrf_token(request: Request):
        """Verify CSRF token for state-changing browser requests (Double Submit Cookie)."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return

        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
        csrf_header = request.headers.get(CSRF_HEADER_NAME)

        # If a valid session user is logged in or a csrf cookie was issued, require matching header
        u = get_current_web_user(request)
        if u or csrf_cookie:
            if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
                logger.warning("CSRF check failed: cookie=%s, header=%s", bool(csrf_cookie), bool(csrf_header))
                raise HTTPException(
                    status_code=403,
                    detail="CSRF token missing or invalid. Please refresh the page.",
                )

    def get_current_web_user(request: Request):
        u = request.session.get("user")
        return u if u and u.get("exp", 0) > time.time() else None

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def serve_index(request: Request):
        response = FileResponse(STATIC_DIR / "index.html")
        csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or generate_csrf_token()
        set_csrf_cookie(response, csrf_token, request)
        return response

    @app.get("/me", include_in_schema=False)
    async def me(request: Request):
        u = get_current_web_user(request)
        csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or generate_csrf_token()
        response = JSONResponse({
            "tier": "internal" if u else "public",
            "name": u.get("name") if u else None,
            "email": u.get("email") if u else None,
            "login_required": True,
            "auth_mode": "otp",
            "backend": "gateway",
            "debug_enabled": os.getenv("DEBUG_UI", "0") == "1",
            "csrf_token": csrf_token,
        })
        set_csrf_cookie(response, csrf_token, request)
        return response

    @app.post("/auth/otp/request", dependencies=[Depends(verify_csrf_token)], include_in_schema=False)
    @app.post("/auth/otp/resend", dependencies=[Depends(verify_csrf_token)], include_in_schema=False)
    async def otp_request(body: OtpReq, request: Request):
        try:
            email = auth.normalize_email(body.email)
        except auth.AuthError:
            return JSONResponse({"error": "Enter your IISc email address (@iisc.ac.in)."}, status_code=400)
        try:
            client_ip = auth.client_ip(request) or "127.0.0.1"
            code = auth.otp_store.issue(email, client_ip)
        except auth.RateLimited as e:
            resend_in = getattr(e, "resend_in", int(C.OTP_RESEND_SECS))
            return JSONResponse({"error": str(e), "resend_in": resend_in}, status_code=429)

        try:
            import asyncio
            await asyncio.to_thread(auth.send_mail, email, "Your AskIISc sign-in code", auth.otp_mail_body(code))
        except auth.MailError as e:
            auth.otp_store.revoke(email)
            logger.error("otp mail failed: %s", e)
            return JSONResponse({"error": f"Could not send email: {e}"}, status_code=503)

        is_resend = request.url.path.endswith("/resend")
        code_desc = "A new 6-digit verification code" if is_resend else "A 6-digit verification code"
        return {
            "ok": True,
            "message": f"{code_desc} has been sent to your email address (valid for {int(C.OTP_TTL)} seconds).",
            "resend_in": int(C.OTP_RESEND_SECS),
        }

    @app.post("/auth/otp/verify", dependencies=[Depends(verify_csrf_token)], include_in_schema=False)
    async def otp_verify(body: OtpVerify, request: Request):
        try:
            email = auth.normalize_email(body.email)
        except auth.AuthError:
            return JSONResponse({"error": "Invalid or expired code."}, status_code=401)
        client_ip = auth.client_ip(request) or "127.0.0.1"
        try:
            auth.otp_store.check_verify_rate_limit(client_ip, email)
        except auth.RateLimited as e:
            return JSONResponse({"error": str(e)}, status_code=429)

        if not auth.otp_store.verify(email, body.code.strip()):
            return JSONResponse({"error": "Invalid or expired code."}, status_code=401)
        request.session.clear()
        import hashlib
        request.session["user"] = {
            "sub": hashlib.sha256(email.encode()).hexdigest(),
            "name": email,
            "email": email,
            "exp": time.time() + C.SESSION_TTL,
        }
        return {"ok": True}

    @app.post("/auth/logout", dependencies=[Depends(verify_csrf_token)], include_in_schema=False)
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/theses", include_in_schema=False)
    async def theses():
        return []

    @app.post("/chat", dependencies=[Depends(verify_csrf_token)], include_in_schema=False)
    async def chat_frontend(req: ChatRequest, request: Request):
        u = get_current_web_user(request)
        if not u:
            return JSONResponse(
                {"error": "Sign in with your IISc email to use AskIISc."},
                status_code=401,
            )
        # VULN-04: Validate prompt against adversarial injection & jailbreak patterns
        try:
            sanitize_and_fence_prompt(req.message)
        except PromptInjectionAlert:
            return JSONResponse(
                {"error": "Adversarial prompt injection detected. Your query contains prohibited system override patterns."},
                status_code=400,
            )
        user_email = u.get("email", "")
        return StreamingResponse(
            stream_via_gateway(req.message, "internal", user_email=user_email, regenerate_token=req.regenerate_token),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
