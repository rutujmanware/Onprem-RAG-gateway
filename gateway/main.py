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
from gateway.clients.vllm_client import VLLMClient
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
    slm: CPUSLMClient = Depends(get_slm_client),
    vllm: VLLMClient = Depends(get_vllm_client),
):
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty")

    query = req.messages[-1].content
    t0 = time.time()

    # Step 1: Neural Complexity Routing (MLP Router)
    score = await scorer.score(query)
    decision = dispatcher.decide(score)

    # Step 2: Route Enforcement for Hard Queries -> GPU LLM Tier (Qwen 2.5 7B)
    if decision.target_tier == ModelTier.GPU_LLM:
        vllm_up = await vllm.check_health()
        if vllm_up:
            try:
                resp = await vllm.generate_chat(
                    [{"role": m.role, "content": m.content} for m in req.messages],
                    max_tokens=req.max_tokens or 512,
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
                return resp
            except Exception:
                pass

        # In-process GPU LLM fallback
        try:
            from pipeline.gpu_llm_runner import GPULLMEngine
            engine = GPULLMEngine.get_instance()
            gpu_res = engine.generate_chat(
                [{"role": m.role, "content": m.content} for m in req.messages],
                max_new_tokens=req.max_tokens or 512,
                temperature=req.temperature or 0.2,
            )
            return {
                "id": f"chatcmpl-hard-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "model": "Qwen/Qwen2.5-7B-Instruct",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": gpu_res["text"]},
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
    # First attempt: Remote CPU SLM daemon on port 8082
    slm_up = await slm.check_health()
    if slm_up:
        try:
            resp = await slm.generate_chat(
                [{"role": m.role, "content": m.content} for m in req.messages],
                max_tokens=req.max_tokens or 160,
                temperature=req.temperature or 0.2,
            )
            resp["router_meta"] = {
                "score": round(score, 4),
                "tier": decision.target_tier.value,
                "route": "easy",
                "reason": decision.reason,
                "routing_time_seconds": round(time.time() - t0, 4),
            }
            return resp
        except Exception:
            pass

    # Fallback attempt: In-process RAG QA Pipeline
    rag = RAGQAPipeline()
    rag_res = rag.answer_query(
        query,
        max_new_tokens=req.max_tokens or 160,
        temperature=req.temperature or 0.2,
    )
    return {
        "id": f"chatcmpl-slm-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "model": "qwen2.5-3b-instruct-cpu",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": rag_res.get("answer", ""),
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


@app.post("/query")
async def query_endpoint(
    req: DirectQueryRequest,
    scorer: ComplexityScorer = Depends(get_complexity_scorer),
    dispatcher: RequestDispatcher = Depends(get_dispatcher),
    vllm: VLLMClient = Depends(get_vllm_client),
):
    query = req.query
    t0 = time.time()
    score = await scorer.score(query)
    decision = dispatcher.decide(score)

    if decision.target_tier == ModelTier.GPU_LLM:
        # Hard query: retrieve chunks (top_k=5 for deep synthesis) and synthesize via GPU LLM
        from pipeline.retriever import RedisChunkRetriever
        from pipeline.gpu_llm_runner import GPULLMEngine

        retriever = RedisChunkRetriever()
        t0_ret = time.time()
        try:
            chunks = retriever.search(query, top_k=req.top_k or 5)
        except Exception:
            chunks = []
        ret_time = round(time.time() - t0_ret, 3)

        vllm_up = await vllm.check_health()
        if vllm_up:
            try:
                messages = GPULLMEngine.build_rag_messages(query, chunks)
                resp = await vllm.generate_chat(
                    messages,
                    max_tokens=req.max_tokens or 512,
                    temperature=req.temperature or 0.2,
                )
                answer_text = resp["choices"][0]["message"]["content"]
                return {
                    "query": query,
                    "answer": answer_text,
                    "route": "hard",
                    "target_tier": "gpu_vllm",
                    "model": resp.get("model", "Qwen/Qwen2.5-7B-Instruct"),
                    "score": round(score, 4),
                    "chunks": chunks,
                    "stats": {
                        "retrieval_seconds": ret_time,
                        "routing_time_seconds": round(time.time() - t0, 4),
                    },
                }
            except Exception:
                pass

        # In-process GPU LLM fallback
        rag = RAGQAPipeline(top_k=req.top_k or 5)
        res = rag.answer_query(
            query,
            top_k=req.top_k or 5,
            max_new_tokens=req.max_tokens or 512,
            temperature=req.temperature or 0.2,
        )
        res["routing_time_seconds"] = round(time.time() - t0, 4)
        return res

    # Route: Easy -> CPU SLM
    rag = RAGQAPipeline(top_k=req.top_k)
    res = rag.answer_query(
        query,
        top_k=req.top_k,
        max_new_tokens=req.max_tokens or 160,
        temperature=req.temperature or 0.2,
    )
    res["route"] = "easy"
    res["target_tier"] = "cpu_slm"
    res["score"] = round(score, 4)
    res["routing_time_seconds"] = round(time.time() - t0, 4)
    return res
