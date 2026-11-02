import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

# Ensure project root is in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from gateway.auth.guard import UserIdentity, require_iisc_user
from gateway.dependencies import (
    container, get_l0_cache, get_l1_cache, get_complexity_scorer,
    get_dispatcher, get_admission_controller, get_slm_client, get_vllm_client
)
from gateway.router.complexity_scorer import ComplexityScorer
from gateway.router.dispatcher import ModelTier, RequestDispatcher
from gateway.clients.cpu_slm_client import CPUSLMClient
from pipeline.rag_chain import RAGQAPipeline

app = FastAPI(
    title="On-Premise Institutional RAG Gateway",
    description="Intelligent Hybrid Routing Gateway between CPU SLM (Qwen 2.5 3B) and GPU LLM (7B/70B)",
    version="2.0.0"
)

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = 160

class DirectQueryRequest(BaseModel):
    query: str
    top_k: Optional[int] = 3
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = 160

@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "service": "On-Premise RAG Gateway",
        "router": "Two-Tower Hybrid MLP (Active)"
    }

@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    user: UserIdentity = Depends(require_iisc_user),
    scorer: ComplexityScorer = Depends(get_complexity_scorer),
    dispatcher: RequestDispatcher = Depends(get_dispatcher),
    slm: CPUSLMClient = Depends(get_slm_client)
):
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty")

    query = req.messages[-1].content
    t0 = time.time()

    # Step 1: Neural Complexity Routing (MLP Router)
    score = await scorer.score(query)
    decision = dispatcher.decide(score)

    # Step 2: Route Enforcement
    if decision.target_tier == ModelTier.GPU_LLM:
        content = (
            f"The 7B model is not yet ready. Your query has been classified as 'hard' "
            f"(Complexity Score: {score:.4f}) and assigned to the GPU LLM tier."
        )
        return {
            "id": f"chatcmpl-hard-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "model": "gpu-vllm-7b",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop"
            }],
            "router_meta": {
                "score": round(score, 4),
                "tier": decision.target_tier.value,
                "route": "hard",
                "reason": decision.reason,
                "routing_time_seconds": round(time.time() - t0, 4)
            },
            "user": user.email
        }

    # Step 3: Easy Queries -> CPU SLM Tier
    # First attempt: Remote CPU SLM daemon on port 8082
    slm_up = await slm.check_health()
    if slm_up:
        try:
            resp = await slm.generate_chat(
                [{"role": m.role, "content": m.content} for m in req.messages],
                max_tokens=req.max_tokens or 160,
                temperature=req.temperature or 0.2
            )
            resp["router_meta"] = {
                "score": round(score, 4),
                "tier": decision.target_tier.value,
                "route": "easy",
                "reason": decision.reason,
                "routing_time_seconds": round(time.time() - t0, 4)
            }
            return resp
        except Exception:
            pass

    # Fallback attempt: In-process RAG QA Pipeline
    rag = RAGQAPipeline()
    rag_res = rag.answer_query(
        query,
        max_new_tokens=req.max_tokens or 160,
        temperature=req.temperature or 0.2
    )
    return {
        "id": f"chatcmpl-slm-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "model": "qwen2.5-3b-instruct-cpu",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": rag_res.get("answer", "")
            },
            "finish_reason": "stop"
        }],
        "router_meta": {
            "score": round(score, 4),
            "tier": decision.target_tier.value,
            "route": "easy",
            "reason": decision.reason,
            "chunks_retrieved": len(rag_res.get("chunks", [])),
            "routing_time_seconds": round(time.time() - t0, 4)
        },
        "user": user.email
    }

@app.post("/query")
async def query_endpoint(
    req: DirectQueryRequest,
    scorer: ComplexityScorer = Depends(get_complexity_scorer),
    dispatcher: RequestDispatcher = Depends(get_dispatcher)
):
    query = req.query
    t0 = time.time()
    score = await scorer.score(query)
    decision = dispatcher.decide(score)

    if decision.target_tier == ModelTier.GPU_LLM:
        return {
            "query": query,
            "answer": f"The 7B model is not yet ready. Your query has been classified as 'hard' (Complexity Score: {score:.4f}) requiring the GPU LLM tier.",
            "route": "hard",
            "target_tier": "gpu_vllm",
            "score": round(score, 4),
            "status": "deferred_7b_not_ready",
            "routing_time_seconds": round(time.time() - t0, 4)
        }

    # Route: Easy -> CPU SLM
    rag = RAGQAPipeline(top_k=req.top_k)
    res = rag.answer_query(
        query,
        top_k=req.top_k,
        max_new_tokens=req.max_tokens,
        temperature=req.temperature
    )
    res["route"] = "easy"
    res["target_tier"] = "cpu_slm"
    res["score"] = round(score, 4)
    res["routing_time_seconds"] = round(time.time() - t0, 4)
    return res
