import json, math
import httpx
from . import config as C

class ModelError(Exception): pass

ENDPOINTS = {
    "slm": lambda: (C.SLM_URL, C.SLM_MODEL, C.SLM_KEY),
    "llm": lambda: (C.LLM_URL, C.LLM_MODEL, C.LLM_KEY),
}

async def stream_chat(which: str, messages: list[dict]):
    """Async generator of (token_text, logprob_or_None). Raises ModelError on any failure."""
    url, model, key = ENDPOINTS[which]()
    body = {"model": model, "messages": messages, "stream": True,
            "max_tokens": C.MAX_TOKENS, "temperature": C.TEMPERATURE}
    if which == "slm":
        body["logprobs"] = True   # used for the cascade confidence check
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(C.SLM_TIMEOUT if which == "slm" else C.REQUEST_TIMEOUT, connect=5)) as cl:
            async with cl.stream("POST", f"{url}/chat/completions", json=body,
                                 headers={"Authorization": f"Bearer {key}"}) as r:
                if r.status_code != 200:
                    raise ModelError(f"{which} HTTP {r.status_code}: {(await r.aread())[:200]!r}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"): continue
                    data = line[5:].strip()
                    if data == "[DONE]": return
                    ch = json.loads(data)["choices"][0]
                    tok = (ch.get("delta") or {}).get("content")
                    if not tok: continue
                    lp = None
                    lpc = (ch.get("logprobs") or {}).get("content")
                    if lpc: lp = lpc[0].get("logprob")
                    yield tok, lp
    except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
        raise ModelError(f"{which} unreachable/bad response: {e!r}") from e

def mean_prob(logprobs: list[float]) -> float | None:
    return sum(math.exp(l) for l in logprobs) / len(logprobs) if logprobs else None
