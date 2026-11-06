"""Backend adapter: forward the question to the team gateway (POST /query with stream=true) and translate what comes back into this app's
SSE events (meta, sources, token, done, error). The gateway does the routing (two-tower MLP), the Redis retrieval and the CPU-SLM / 7B generation.
Field names are read tolerantly because they were taken from the team's design document, not from a live capture; set GATEWAY_DEBUG=1 to log raw events."""
import json, logging, time
import httpx
from . import config as C

log = logging.getLogger("askiisc.gateway")

def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"

def tier_of(route) -> str | None:
    s = str(route or "").lower()
    if not s: return None
    if "cache" in s: return "cache"                         # answered from the semantic cache, no model ran
    return "llm" if any(k in s for k in ("hard", "gpu", "llm", "7b")) else "slm"

def _meta(c: dict) -> dict:
    m = c.get("metadata")
    if isinstance(m, str):
        try: m = json.loads(m)
        except ValueError: m = {}
    return m if isinstance(m, dict) else {}

def cite(i: int, c: dict) -> dict:
    m = _meta(c)
    try: dist = float(c.get("score", 0))
    except (TypeError, ValueError): dist = 0.0
    return {"n": i, "thesis_id": str(c.get("doc_id") or c.get("handle_id") or ""), "title": str(m.get("title") or c.get("title") or ""),
            "chapter": "", "version": "", "distance": dist,
            "text": ""}

def token_text(d: dict) -> str:
    for k in ("token", "delta", "content", "text"):
        v = d.get(k)
        if isinstance(v, dict): v = v.get("content")
        if isinstance(v, str) and v: return v
    ch = d.get("choices")                                   # OpenAI-style chunk, in case the gateway forwards those
    if isinstance(ch, list) and ch:
        v = (ch[0].get("delta") or {}).get("content")
        if isinstance(v, str): return v
    return ""

async def stream_via_gateway(query: str, tier: str):
    """Async generator of SSE strings for the browser."""
    t0 = time.perf_counter(); st = {"route": None, "meta": False, "tokens": 0, "raw": 0}

    def meta_ev(d):
        st["meta"] = True
        s = d.get("probability_hard", d.get("score"))
        return sse("meta", {"tier": tier, "route": st["route"], "s": s if isinstance(s, (int, float)) else None, "alpha": None,
                            "reason": "gateway", "citations": []})
    def done_ev():
        return sse("done", {"model": st["route"], "timings": {"embed_ms": 0, "retrieve_ms": 0, "total_ms": round((time.perf_counter() - t0) * 1e3)},
                            "conf": None, "escalated": False})
    def handle(d: dict):
        """Translate one gateway event into zero or more browser events."""
        out, t = [], d.get("type")
        if C.GATEWAY_DEBUG and st["raw"] < 8:
            st["raw"] += 1; log.warning("gateway raw event %d: %s", st["raw"], json.dumps(d)[:300])
        if t == "route" or (t is None and ("route" in d or "target_tier" in d) and "answer" not in d):
            st["route"] = tier_of(d.get("target_tier") or d.get("route")) or st["route"]; out.append(meta_ev(d))
        elif t == "chunks" or (t is None and "chunks" in d and "answer" not in d):
            cs = [cite(i, c) for i, c in enumerate(d.get("chunks") or [], 1) if isinstance(c, dict)]
            if cs: out.append(sse("sources", {"citations": cs}))
        elif t == "token":
            txt = token_text(d)
            if txt: st["tokens"] += 1; out.append(sse("token", {"t": txt}))
        elif t == "cache":
            if str(d.get("status", "")).upper() == "HIT": st["route"] = "cache"      # a HIT is followed by a route event whose tier is semantic_cache
        elif t == "error":
            out.append(sse("error", {"msg": str(d.get("message") or d.get("error") or "The answering service reported an error.")[:200]}))
        elif t == "done":
            if d.get("cached") is True: st["route"] = "cache"
            else: st["route"] = st["route"] or tier_of(d.get("target_tier") or d.get("route"))
        elif t is None:
            txt = token_text(d)
            if txt: st["tokens"] += 1; out.append(sse("token", {"t": txt}))      # bare OpenAI-style chunk
        return out

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(C.GATEWAY_TIMEOUT, connect=5)) as cl:
            async with cl.stream("POST", f"{C.GATEWAY_URL}/query", json={"query": query, "stream": True, "max_tokens": C.MAX_TOKENS}) as r:
                if r.status_code != 200:
                    log.warning("gateway HTTP %s: %r", r.status_code, (await r.aread())[:200])
                    yield sse("error", {"msg": f"The answering service returned an error (HTTP {r.status_code})."}); return
                if "event-stream" not in r.headers.get("content-type", ""):     # gateway answered with one JSON document instead of a stream
                    d = json.loads(await r.aread())
                    if C.GATEWAY_DEBUG: log.warning("gateway raw JSON: %s", json.dumps(d)[:300])
                    st["route"] = tier_of(d.get("target_tier") or d.get("route"))
                    yield meta_ev(d)
                    cs = [cite(i, c) for i, c in enumerate(d.get("chunks") or [], 1) if isinstance(c, dict)]
                    if cs: yield sse("sources", {"citations": cs})
                    if d.get("answer"): st["tokens"] += 1; yield sse("token", {"t": str(d["answer"])})
                else:
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"): continue
                        data = line[5:].strip()
                        if data == "[DONE]": break
                        try: d = json.loads(data)
                        except ValueError: continue
                        if isinstance(d, dict):
                            for ev in handle(d): yield ev
                if not st["tokens"]:
                    yield sse("error", {"msg": "The answering service returned no answer."}); return
                yield done_ev()
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        log.warning("gateway unreachable/bad response: %r", e)
        yield sse("error", {"msg": f"The answering service is not reachable right now ({type(e).__name__})."})
