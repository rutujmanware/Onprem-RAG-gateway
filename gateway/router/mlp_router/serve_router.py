#!/usr/bin/env python3
"""Standalone Microservice for Two-Tower Neural Complexity Router.

Listens on port 8090 by default, providing low-latency routing endpoints:
- GET  /healthz
- POST /score
- POST /route
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

from gateway.router.mlp_router.inference import QueryRouter

DEFAULT_PORT = int(os.getenv("ROUTER_PORT", "8090"))
from contextlib import asynccontextmanager

DEFAULT_HOST = os.getenv("ROUTER_HOST", "127.0.0.1")
DEFAULT_THRESHOLD = float(os.getenv("ROUTING_ALPHA", "0.65"))
DEFAULT_DEVICE = os.getenv("ROUTER_DEVICE", "cpu")

router_instance: Optional[QueryRouter] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global router_instance
    print(f"🚀 Initializing QueryRouter on device: {DEFAULT_DEVICE} (Threshold: {DEFAULT_THRESHOLD})...")
    router_instance = QueryRouter(threshold=DEFAULT_THRESHOLD, device=DEFAULT_DEVICE)
    print("✅ QueryRouter microservice ready!")
    yield


app = FastAPI(
    title="Neural Complexity Router Microservice",
    description="Standalone two-tower neural routing microservice for query complexity classification",
    version="1.0.0",
    lifespan=lifespan,
)


class RouteRequest(BaseModel):
    query: str = Field(..., description="User query text to classify")
    threshold: Optional[float] = Field(default=None, description="Optional override threshold (default: 0.65)")


class RouteBatchRequest(BaseModel):
    queries: List[str] = Field(..., description="List of user query texts")
    threshold: Optional[float] = Field(default=None, description="Optional override threshold")


@app.get("/healthz")
def healthz():
    global router_instance
    return {
        "status": "healthy" if router_instance is not None else "initializing",
        "service": "mlp-router-microservice",
        "device": DEFAULT_DEVICE,
        "default_threshold": DEFAULT_THRESHOLD,
    }


@app.post("/score")
def score_query(req: RouteRequest):
    global router_instance
    if router_instance is None:
        raise HTTPException(status_code=503, detail="Router not initialized")
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    t0 = time.time()
    res = router_instance.predict(req.query)
    latency_ms = (time.time() - t0) * 1000

    return {
        "query": req.query,
        "score": res["probability_hard"],
        "latency_ms": round(latency_ms, 2),
    }


@app.post("/route")
def route_query(req: RouteRequest):
    global router_instance
    if router_instance is None:
        raise HTTPException(status_code=503, detail="Router not initialized")
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    threshold = req.threshold if req.threshold is not None else DEFAULT_THRESHOLD
    t0 = time.time()
    res = router_instance.predict(req.query)
    score = res["probability_hard"]
    is_hard = score >= threshold

    latency_ms = (time.time() - t0) * 1000

    return {
        "query": req.query,
        "route": "hard" if is_hard else "easy",
        "target_tier": "gpu_vllm" if is_hard else "cpu_slm",
        "score": score,
        "threshold": threshold,
        "latency_ms": round(latency_ms, 2),
    }


@app.post("/route/batch")
def route_batch(req: RouteBatchRequest):
    global router_instance
    if router_instance is None:
        raise HTTPException(status_code=503, detail="Router not initialized")

    threshold = req.threshold if req.threshold is not None else DEFAULT_THRESHOLD
    t0 = time.time()
    results = []
    for q in req.queries:
        if not q.strip():
            continue
        res = router_instance.predict(q)
        score = res["probability_hard"]
        is_hard = score >= threshold
        results.append({
            "query": q,
            "route": "hard" if is_hard else "easy",
            "target_tier": "gpu_vllm" if is_hard else "cpu_slm",
            "score": score,
        })
    total_ms = (time.time() - t0) * 1000

    return {
        "batch_size": len(results),
        "results": results,
        "total_latency_ms": round(total_ms, 2),
        "avg_latency_ms": round(total_ms / max(len(results), 1), 2),
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Start Standalone MLP Router Microservice")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host to bind (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to listen (default: {DEFAULT_PORT})")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help=f"Device to run embedder and MLP (default: {DEFAULT_DEVICE})")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help=f"Classification threshold (default: {DEFAULT_THRESHOLD})")
    args = parser.parse_args()

    os.environ["ROUTER_DEVICE"] = str(args.device)
    os.environ["ROUTING_ALPHA"] = str(args.threshold)

    print(f"🚀 Starting MLP Router Microservice on http://{args.host}:{args.port} (Device: {args.device})")
    uvicorn.run("gateway.router.mlp_router.serve_router:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()

