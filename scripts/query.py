#!/usr/bin/env python3
"""Convenient CLI client to query the On-Premise Institutional RAG Gateway.

Usage:
    python scripts/query.py "What is vermiculite and what are its uses?"
    python scripts/query.py "Compare stability of fluid flows with magnetic fields."
    python scripts/query.py  # Launches interactive mode
"""

import sys
import json
import time
import argparse
import urllib.request
import urllib.error

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8080/query"

def ask_gateway(query: str, url: str = DEFAULT_GATEWAY_URL, stream: bool = True, raw: bool = False, max_tokens: int = None):
    req_body = {"query": query, "stream": stream}
    if max_tokens:
        req_body["max_tokens"] = max_tokens
    payload = json.dumps(req_body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        },
        method="POST"
    )

    t0 = time.time()
    try:
        response = urllib.request.urlopen(req, timeout=120)
    except urllib.error.URLError as e:
        print(f"\n❌ Error connecting to Gateway at {url}: {e}")
        print("💡 Ensure the Gateway is running: `make dev` or `uvicorn gateway.main:app --port 8080`")
        return

    if not stream or raw:
        with response:
            raw_text = response.read().decode("utf-8")
        elapsed = round(time.time() - t0, 2)
        try:
            res = json.loads(raw_text)
        except json.JSONDecodeError as je:
            print(f"\n❌ JSON Decode Error ({je}): The gateway returned non-JSON data.")
            print("💡 Did you request a streaming endpoint with a non-streaming client?")
            print(f"Raw Response Preview:\n{raw_text[:400]}")
            return
        if raw:
            print(json.dumps(res, indent=2))
            return

        route = res.get("route", "unknown").upper()
        tier = res.get("target_tier", "unknown")
        model = res.get("model", "unknown")
        score = res.get("score", 0.0)
        answer = res.get("answer", "No answer returned.")
        chunks = res.get("chunks", [])
        is_cached = res.get("cached", False)
        sim = res.get("similarity")
        matched = res.get("matched_prompt")

        sim_range = ""
        if chunks:
            sim_range = f" | Similarities: {chunks[0].get('similarity', 0.0):.2f} -> {chunks[-1].get('similarity', 0.0):.2f}" if len(chunks) > 1 else f" | Sim: {chunks[0].get('similarity', 0.0):.2f}"

        print("\n" + "=" * 70)
        if is_cached:
            print(f"⚡ SEMANTIC CACHE: [HIT] (Cosine Similarity: {sim} | Matched: \"{matched}\")")
            print(f"🤖 PRODUCING MODEL: Redis Semantic Vector Cache (Zero-Compute Instant Retrieval)")
            print(f"🎯 ROUTE: [CACHED] -> Instant Sub-10ms Delivery | 0 GPU/CPU Compute")
        else:
            print(f"⚡ SEMANTIC CACHE: [MISS] (Fresh Inference Generated & Saved)")
            model_display = model if model != "unknown" else ("Qwen/Qwen2.5-7B-Instruct (GPU vLLM)" if route == "HARD" else "Qwen/Qwen2.5-3B-Instruct (CPU SLM)")
            print(f"🤖 PRODUCING MODEL: {model_display}")
            print(f"🎯 ROUTE: [{route}] -> Tier: {tier} | Complexity Score: {score}")
            if chunks and chunks[0].get("metadata_fallback_used"):
                print(f"⚠️  METADATA: [FALLBACK USED] (Filter {chunks[0].get('metadata_filter_extracted')} returned 0 hits -> Fallback to full search)")
            elif chunks and chunks[0].get("metadata_filter_applied"):
                print(f"🎯 METADATA: [FILTER APPLIED] (Constrained to {chunks[0].get('metadata_filter_extracted')})")
        print(f"⏱️  LATENCY: {elapsed}s | Retrieved Chunks: {len(chunks)}{sim_range}")
        print("=" * 70)
        print("\n📝 ANSWER:\n")
        print(answer)
        print("\n" + "-" * 70)

        if chunks:
            print(f"📚 TOP SOURCES REFERENCED ({len(chunks)} chunks):")
            for i, c in enumerate(chunks[:3], 1):
                title = c.get("title") or c.get("doc_id", "Unknown Document")
                handle = c.get("handle_id", "")
                url_ref = f"https://etd.iisc.ac.in/handle/2005/{handle}" if handle else ""
                print(f"  [{i}] {title} {url_ref}")
            print("-" * 70)
        return

    # Real-time streaming output (Like actual LLM CLI)
    print("\n" + "=" * 70)
    print("🚀 CONNECTED TO RAG GATEWAY - STREAMING RESPONSE")
    print("=" * 70)

    first_token = True
    chunks = []
    ttft = None
    stats = {}

    with response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break

            try:
                event = json.loads(data_str)
            except Exception:
                continue

            etype = event.get("type")
            if etype == "cache":
                status = event.get("status", "HIT")
                sim = event.get("similarity", 1.0)
                matched = event.get("matched_prompt", "")
                print(f"⚡ SEMANTIC CACHE: [{status}] (Cosine Similarity: {sim:.4f} | Matched: \"{matched}\")")
                print(f"🤖 PRODUCING MODEL: Redis Semantic Vector Cache (Zero-Compute Retrieval)")
            elif etype == "route":
                route = event.get("route", "").upper()
                tier = event.get("target_tier", "")
                prob = event.get("probability_hard", 0.0)
                model_name = "Qwen/Qwen2.5-7B-Instruct (GPU vLLM Tier)" if route == "HARD" else "Qwen/Qwen2.5-3B-Instruct (CPU SLM Tier)"
                if tier == "semantic_cache":
                    print(f"🎯 ROUTE: [CACHED] -> Instant Sub-10ms Delivery | 0 GPU/CPU Compute")
                else:
                    print(f"⚡ SEMANTIC CACHE: [MISS] (Fresh Inference Triggered)")
                    print(f"🤖 PRODUCING MODEL: {model_name}")
                    print(f"🎯 ROUTE: [{route}] -> Target Tier: {tier} | Hard Probability: {prob:.4f}")
            elif etype == "chunks":
                chunks = event.get("chunks", [])
                ret_s = event.get("retrieval_seconds", 0.0)
                if chunks:
                    sim_range = f" (Similarities: {chunks[0].get('similarity', 0.0):.2f} -> {chunks[-1].get('similarity', 0.0):.2f})" if len(chunks) > 1 else f" (Similarity: {chunks[0].get('similarity', 0.0):.2f})"
                    print(f"📚 RETRIEVED: {len(chunks)} chunks dynamically in {ret_s}s{sim_range}")
                    if chunks[0].get("metadata_fallback_used"):
                        print(f"⚠️  METADATA: [FALLBACK USED] (Filter {chunks[0].get('metadata_filter_extracted')} returned 0 hits -> Fallback to full search)")
                    elif chunks[0].get("metadata_filter_applied"):
                        print(f"🎯 METADATA: [FILTER APPLIED] (Constrained to {chunks[0].get('metadata_filter_extracted')})")
                else:
                    print(f"📚 RETRIEVED: 0 chunks in {ret_s}s (Below Similarity Floor - Out of Domain)")
                print("-" * 70)
                print("📝 ANSWER:\n")
            elif etype == "token":
                delta = event.get("delta", "")
                if delta:
                    if first_token:
                        ttft = round(time.time() - t0, 2)
                        first_token = False
                    sys.stdout.write(delta)
                    sys.stdout.flush()
            elif etype == "done":
                stats = event.get("stats", {})

    elapsed = round(time.time() - t0, 2)
    print("\n" + "-" * 70)
    ttft_str = f"{ttft}s" if ttft is not None else f"{elapsed}s"
    print(f"⏱️  LATENCY: {elapsed}s | TTFT: {ttft_str} | Chunks: {len(chunks)}")
    if stats.get("cached"):
        print("⚡ SPEED: Instant Cache Hit | 0 Compute Cycles")
    elif stats.get("tokens_per_second"):
        print(f"⚡ SPEED: {stats.get('tokens_per_second')} tok/s | Model: {stats.get('model', 'slm-3b-gguf')}")

    if chunks:
        print(f"📚 TOP SOURCES REFERENCED ({len(chunks)} chunks):")
        for i, c in enumerate(chunks[:3], 1):
            title = c.get("title") or c.get("doc_id", "Unknown Document")
            handle = c.get("handle_id", "")
            url_ref = f"https://etd.iisc.ac.in/handle/2005/{handle}" if handle else ""
            print(f"  [{i}] {title} {url_ref}")
        print("-" * 70)
    print("=" * 70 + "\n")

def main():
    parser = argparse.ArgumentParser(description="Query the On-Premise Institutional RAG Gateway")
    parser.add_argument("query", nargs="*", help="Question to ask the knowledge base")
    parser.add_argument("--url", default=DEFAULT_GATEWAY_URL, help=f"Gateway endpoint (default: {DEFAULT_GATEWAY_URL})")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming and wait for full response")
    parser.add_argument("--max-tokens", type=int, default=None, help="Maximum tokens to generate")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON response")
    args = parser.parse_args()

    use_stream = not args.no_stream

    if args.query:
        query_text = " ".join(args.query)
        ask_gateway(query_text, url=args.url, stream=use_stream, raw=args.raw, max_tokens=args.max_tokens)
    else:
        print("💬 On-Prem RAG Gateway Interactive Prompt (type 'exit' to quit)\n")
        while True:
            try:
                q = input("📝 Ask a question: ").strip()
                if not q or q.lower() in ("exit", "quit", "q"):
                    break
                ask_gateway(q, url=args.url, stream=use_stream, raw=args.raw, max_tokens=args.max_tokens)
            except KeyboardInterrupt:
                print("\nExiting.")
                break

if __name__ == "__main__":
    main()

