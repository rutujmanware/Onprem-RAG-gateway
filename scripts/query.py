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

def ask_gateway(query: str, url: str = DEFAULT_GATEWAY_URL, raw: bool = False):
    payload = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            res = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"\n❌ Error connecting to Gateway at {url}: {e}")
        print("💡 Ensure the Gateway is running: `make dev` or `uvicorn gateway.main:app --port 8080`")
        return

    elapsed = round(time.time() - t0, 2)

    if raw:
        print(json.dumps(res, indent=2))
        return

    route = res.get("route", "unknown").upper()
    tier = res.get("target_tier", "unknown")
    model = res.get("model", "unknown")
    score = res.get("score", 0.0)
    answer = res.get("answer", "No answer returned.")
    chunks = res.get("chunks", [])

    print("\n" + "=" * 70)
    print(f"🎯 ROUTE: [{route}] -> Tier: {tier} | Model: {model} | Complexity Score: {score}")
    print(f"⏱️  LATENCY: {elapsed}s | Retrieved Chunks: {len(chunks)}")
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

def main():
    parser = argparse.ArgumentParser(description="Query the On-Premise Institutional RAG Gateway")
    parser.add_argument("query", nargs="*", help="Question to ask the knowledge base")
    parser.add_argument("--url", default=DEFAULT_GATEWAY_URL, help=f"Gateway endpoint (default: {DEFAULT_GATEWAY_URL})")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON response")
    args = parser.parse_args()

    if args.query:
        query_text = " ".join(args.query)
        ask_gateway(query_text, url=args.url, raw=args.raw)
    else:
        print("💬 On-Prem RAG Gateway Interactive Prompt (type 'exit' to quit)\n")
        while True:
            try:
                q = input("📝 Ask a question: ").strip()
                if not q or q.lower() in ("exit", "quit", "q"):
                    break
                ask_gateway(q, url=args.url, raw=args.raw)
            except KeyboardInterrupt:
                print("\nExiting.")
                break

if __name__ == "__main__":
    main()

