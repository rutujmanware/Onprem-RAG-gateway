import asyncio
import json
import logging
import os
import sys
import time
import uuid
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

logger = logging.getLogger("gateway.main")

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
    if not xff and client_host in ("127.0.0.1", "::1", "localhost", "testclient"):
        return True

    # If forwarded, check if the forwarded client IP is loopback
    if xff:
        client_ip = xff.split(",")[0].strip()
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            return True

    # Allow authenticated web session
    if "session" in request.scope:
        user = request.session.get("user")
        if user and user.get("exp", 0) > time.time():
            return True

    # Allow admin API key
    if ADMIN_KEY:
        auth_hdr = request.headers.get("X-Admin-Key") or request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if auth_hdr == ADMIN_KEY:
            return True

    raise HTTPException(status_code=404, detail="Not Found")

from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "e7b4c91a3f8d2e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a"),
    https_only=os.getenv("COOKIE_SECURE", "0") == "1",
    same_site="lax",
    max_age=int(os.getenv("SESSION_TTL", str(8 * 3600))),
    session_cookie="askiisc_session",
)

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False

class DirectQueryRequest(BaseModel):
    query: str
    top_k: Optional[int] = None
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = 2048
    stream: Optional[bool] = False
    bypass_cache: Optional[bool] = False

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
        "vector_dim": cache.vector_dim,
        "cached_queries_count": count,
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

    query = req.messages[-1].content.strip()
    t0 = time.time()

    # Step 0: Semantic Vector Cache Lookup
    try:
        hit = await cache.alookup(query)
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
    decision = dispatcher.decide(score)

    if req.stream:
        rag = RAGQAPipeline()
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())

        def sse_chat_generator():
            full_ans = []
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
                elif event.get("type") == "done":
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

            # Save generated completion to cache
            complete_text = "".join(full_ans)
            if complete_text:
                try:
                    cache.store(query, {"answer": complete_text})
                except Exception:
                    pass
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            sse_chat_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

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
                await cache.astore(query, {"answer": ans_text})
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
            await cache.astore(query, {"answer": ans_text})
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
            await cache.astore(query, {"answer": ans_text})
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
    await cache.astore(query, {"answer": ans_text, "chunks": rag_res.get("chunks", [])})
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
    query = req.query.strip()
    t0 = time.time()

    # Identify caller for token-bucket and queue accounting
    client_id = request.client.host if request.client else "anonymous"
    if "session" in request.scope:
        u = request.session.get("user")
        if u and isinstance(u, dict) and u.get("email"):
            client_id = u["email"]

    # Step 0: Check Semantic Vector Cache in Redis (Fast path < 5ms bypasses admission queue)
    hit = None
    if not req.bypass_cache:
        try:
            hit = await cache.alookup(query)
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

        if req.stream:
            def cached_stream():
                # Yield cache hit notification event
                yield f"data: {json.dumps({'type': 'cache', 'status': 'HIT', 'similarity': sim, 'matched_prompt': matched_prompt})}\n\n"
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
                    yield f"data: {json.dumps(event)}\n\n"

                full_text = "".join(collected_answer)
                if full_text:
                    try:
                        cache_payload = {
                            "query": query,
                            "answer": full_text,
                            "route": route_info.get("route", "easy"),
                            "target_tier": route_info.get("target_tier", "cpu_slm"),
                            "chunks": collected_chunks,
                        }
                        cache.store(query, cache_payload)
                    except Exception:
                        pass
                yield "data: [DONE]\n\n"
            finally:
                admission.release()

        return StreamingResponse(
            event_stream(),
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

        decision = dispatcher.decide(score)
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

        if decision.target_tier == ModelTier.GPU_LLM:
            gpu_max_tokens = req.max_tokens if (req.max_tokens is not None and req.max_tokens > 0) else 2048

            if not chunks:
                param_messages = [
                    {
                        "role": "system",
                        "content": (
                            "You are an advanced academic AI research assistant. "
                            "No specific institutional repository documents directly cover this topic. "
                            "Please provide a thorough, accurate, and deeply reasoned explanation of the user's question "
                            "using standard academic and technical literature."
                        ),
                    },
                    {"role": "user", "content": query},
                ]
                vllm_up = await vllm.check_health()
                if vllm_up:
                    try:
                        resp = await vllm.generate_chat(param_messages, max_tokens=gpu_max_tokens, temperature=req.temperature or 0.2)
                        answer_text = "No specific institutional repository theses were found on this topic. Based on general academic knowledge:\n\n" + resp["choices"][0]["message"]["content"]
                        res = {
                            "query": query,
                            "answer": answer_text,
                            "route": "hard",
                            "target_tier": "gpu_vllm",
                            "model": resp.get("model", "Qwen/Qwen2.5-7B-Instruct"),
                            "score": round(score, 4),
                            "chunks": [],
                            "cached": False,
                            "stats": {
                                "retrieval_seconds": pre_duration,
                                "routing_time_seconds": round(time.time() - t0, 4),
                            },
                        }
                        if not req.bypass_cache:
                            await cache.astore(query, res)
                        return res
                    except Exception:
                        pass
                return {
                    "query": query,
                    "answer": "No relevant documents were found in the knowledge base matching your query.",
                    "route": "hard",
                    "target_tier": "gpu_vllm",
                    "model": "retriever-filter",
                    "score": round(score, 4),
                    "chunks": [],
                    "cached": False,
                    "stats": {
                        "retrieval_seconds": pre_duration,
                        "routing_time_seconds": round(time.time() - t0, 4),
                    },
                }

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
                    res = {
                        "query": query,
                        "answer": answer_text,
                        "route": "hard",
                        "target_tier": "gpu_vllm",
                        "model": resp.get("model", "Qwen/Qwen2.5-7B-Instruct"),
                        "score": round(score, 4),
                        "chunks": sanitize_chunks(chunks),
                        "cached": False,
                        "stats": {
                            "retrieval_seconds": pre_duration,
                            "routing_time_seconds": round(time.time() - t0, 4),
                        },
                    }
                    if not req.bypass_cache:
                        await cache.astore(query, res)
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
                res["chunks"] = sanitize_chunks(res["chunks"])
            res["cached"] = False
            res["routing_time_seconds"] = round(time.time() - t0, 4)
            if not req.bypass_cache:
                await cache.astore(query, res)
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
            res["chunks"] = sanitize_chunks(res["chunks"])
        res["route"] = "easy"
        res["target_tier"] = "cpu_slm"
        res["score"] = round(score, 4)
        res["cached"] = False
        res["routing_time_seconds"] = round(time.time() - t0, 4)
        if not req.bypass_cache:
            await cache.astore(query, res)
        return res
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
        message: str
        history: Optional[List[Dict[str, Any]]] = []
        thesis_id: Optional[str] = None
        force: Optional[str] = None

    def get_current_web_user(request: Request):
        u = request.session.get("user")
        return u if u and u.get("exp", 0) > time.time() else None

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def serve_index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/me", include_in_schema=False)
    async def me(request: Request):
        u = get_current_web_user(request)
        return {
            "tier": "internal" if u else "public",
            "name": u.get("name") if u else None,
            "email": u.get("email") if u else None,
            "login_required": True,
            "auth_mode": "otp",
            "backend": "gateway",
        }

    @app.post("/auth/otp/request", include_in_schema=False)
    @app.post("/auth/otp/resend", include_in_schema=False)
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

    @app.post("/auth/otp/verify", include_in_schema=False)
    async def otp_verify(body: OtpVerify, request: Request):
        try:
            email = auth.normalize_email(body.email)
        except auth.AuthError:
            return JSONResponse({"error": "Invalid or expired code."}, status_code=401)
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

    @app.post("/auth/logout", include_in_schema=False)
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/theses", include_in_schema=False)
    async def theses():
        return []

    @app.post("/chat", include_in_schema=False)
    async def chat_frontend(req: ChatRequest, request: Request):
        u = get_current_web_user(request)
        if not u:
            return JSONResponse(
                {"error": "Sign in with your IISc email to use AskIISc."},
                status_code=401,
            )
        return StreamingResponse(
            stream_via_gateway(req.message, "internal"),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
