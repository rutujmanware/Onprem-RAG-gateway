#!/usr/bin/env python3
"""OpenAI-compatible FastAPI server for Qwen 2.5 7B running on GPU (NVIDIA A100).

Listens on port 8000 by default, providing endpoints:
- GET  /healthz
- GET  /v1/models
- POST /v1/chat/completions
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
import json
from pydantic import BaseModel, Field
import uvicorn

from pipeline.gpu_llm_runner import GPULLMEngine, DEFAULT_GPU_MODEL, DEFAULT_GPU_DEVICE

MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"

app = FastAPI(
    title="GPU Large Language Model (LLM) Server",
    description="OpenAI-compatible GPU inference server for Qwen 2.5 7B Instruct on NVIDIA A100",
    version="1.0.0",
)

# Global engine handle (lazy or startup initialized)
engine: Optional[GPULLMEngine] = None


@app.on_event("startup")
def startup_event():
    global engine
    device = os.getenv("GPU_DEVICE", DEFAULT_GPU_DEVICE)
    engine = GPULLMEngine.get_instance(device=device)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = MODEL_NAME
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.2
    top_p: Optional[float] = 0.8
    max_tokens: Optional[int] = Field(default=512, alias="max_tokens")
    stream: Optional[bool] = False


@app.get("/healthz")
def healthz():
    global engine
    is_ready = engine is not None and engine.model is not None
    vram_alloc = 0.0
    vram_total = 0.0
    if torch.cuda.is_available():
        vram_alloc = round(torch.cuda.memory_allocated() / (1024 ** 2), 2)
        vram_total = round(torch.cuda.get_device_properties(0).total_memory / (1024 ** 2), 2)

    return {
        "status": "healthy" if is_ready else "initializing",
        "model": MODEL_NAME,
        "device": str(engine.device) if engine else os.getenv("GPU_DEVICE", DEFAULT_GPU_DEVICE),
        "vram_allocated_mb": vram_alloc,
        "vram_total_mb": vram_total,
    }


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "onprem-rag-gateway",
                "root": MODEL_NAME,
                "parent": None,
                "permission": [],
            }
        ],
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    global engine
    if engine is None:
        device = os.getenv("GPU_DEVICE", DEFAULT_GPU_DEVICE)
        engine = GPULLMEngine.get_instance(device=device)

    raw_messages = [{"role": m.role, "content": m.content} for m in req.messages]
    if not raw_messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty.")

    do_sample = (req.temperature is not None and req.temperature > 0)
    temp = req.temperature if req.temperature is not None else 0.2
    top_p = req.top_p if req.top_p is not None else 0.8
    max_tokens = req.max_tokens or 512

    try:
        if req.stream:
            def event_generator():
                cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                created_ts = int(time.time())
                for chunk_text in engine.generate_chat_stream(
                    messages=raw_messages,
                    max_new_tokens=max_tokens,
                    temperature=temp,
                    top_p=top_p,
                    do_sample=do_sample,
                ):
                    data = {
                        "id": cmpl_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": MODEL_NAME,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": chunk_text},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(data)}\n\n"

                done_data = {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": MODEL_NAME,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(done_data)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(event_generator(), media_type="text/event-stream")

        result = engine.generate_chat(
            messages=raw_messages,
            max_new_tokens=max_tokens,
            temperature=temp,
            top_p=top_p,
            do_sample=do_sample,
        )

        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        return {
            "id": cmpl_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": result.get("model", MODEL_NAME),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": result["text"],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": -1,
                "completion_tokens": result["num_tokens"],
                "total_tokens": result["num_tokens"],
            },
            "system_fingerprint": f"gpu-{result.get('device', 'cuda')}-{result.get('tokens_per_second', 0)}tok_s",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"GPU generation failed: {str(e)}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Start GPU LLM API Server (Qwen 2.5 7B)")
    parser.add_argument("--host", default=os.getenv("GPU_SERVER_HOST", "127.0.0.1"), help="Host to bind (default: 127.0.0.1 - localhost only for security)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen (default: 8000)")
    parser.add_argument("--device", default=DEFAULT_GPU_DEVICE, help="GPU device (default: cuda:0)")
    args = parser.parse_args()

    os.environ["GPU_DEVICE"] = str(args.device)
    print(f"🚀 Starting GPU LLM Server on http://{args.host}:{args.port} (Device: {args.device})")
    uvicorn.run("pipeline.serve_gpu_llm:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()

