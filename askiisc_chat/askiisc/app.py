import json, time, os, logging, asyncio, hashlib, secrets
from contextlib import asynccontextmanager
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import config as C
from .embedder import embed_query
from .retrieval import Retriever, NpzRetriever
from .router import Router
from .prompt import build_messages
from . import auth
from . import gateway_backend
from .llm_clients import stream_chat, mean_prob, ModelError

log = logging.getLogger("askiisc")
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")

class ChatReq(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[dict] = []
    thesis_id: str | None = None
    force: str | None = None          # "slm" | "llm" | None (auto)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "retriever") and C.BACKEND != "gateway":
        app.state.retriever = (NpzRetriever.load(C.CHUNKS_FILE) if C.RETRIEVER_BACKEND == "npz"
                               else Retriever(aioredis.from_url(C.REDIS_URL)))
    if not hasattr(app.state, "router") and C.BACKEND != "gateway":
        app.state.router = Router()
    yield

app = FastAPI(title="AskIISc chat", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.add_middleware(SessionMiddleware, secret_key=C.SESSION_SECRET or secrets.token_urlsafe(32),  # random per-start if unset: sessions die on restart
                   https_only=C.COOKIE_SECURE, same_site="lax", max_age=C.SESSION_TTL, session_cookie="askiisc_session")

@app.middleware("http")
async def network_gate(request: Request, call_next):
    """Outermost check: not on an allowed IISc range -> nothing is served, not even the UI."""
    if not auth.ip_allowed(auth.client_ip(request)):
        return JSONResponse({"error": "Access is restricted to the IISc network."}, status_code=403)
    return await call_next(request)

def current_user(request: Request):
    u = request.session.get("user")
    return u if u and u.get("exp", 0) > time.time() else None

class OtpReq(BaseModel):
    email: str = Field(max_length=300)
class OtpVerify(BaseModel):
    email: str = Field(max_length=300)
    code: str = Field(max_length=16)

@app.post("/auth/otp/request")
@app.post("/auth/otp/resend")
async def otp_request(body: OtpReq, request: Request):
    if C.AUTH_MODE != "otp": return JSONResponse({"error": "not enabled"}, status_code=404)
    try: email = auth.normalize_email(body.email)
    except auth.AuthError:     # nothing is sent to addresses outside the allowed domain(s)
        return JSONResponse({"error": "Enter your IISc email address."}, status_code=400)
    try: code = auth.otp_store.issue(email, auth.client_ip(request) or "?")
    except auth.RateLimited as e:
        return JSONResponse({"error": str(e), "resend_in": getattr(e, "resend_in", int(C.OTP_RESEND_SECS))}, status_code=429)
    try: await asyncio.to_thread(auth.send_mail, email, "Your AskIISc sign-in code", auth.otp_mail_body(code))
    except auth.MailError as e:
        auth.otp_store.revoke(email); log.error("otp mail failed: %s", e)
        return JSONResponse({"error": f"Could not send email: {e}"}, status_code=503)
    is_resend = request.url.path.endswith("/resend")
    prefix = "A new 6-digit verification code" if is_resend else "A 6-digit verification code"
    return {"ok": True, "message": f"{prefix} has been sent to your email address.", "resend_in": int(C.OTP_RESEND_SECS)}

@app.post("/auth/otp/verify")
async def otp_verify(body: OtpVerify, request: Request):
    if C.AUTH_MODE != "otp": return JSONResponse({"error": "not enabled"}, status_code=404)
    try: email = auth.normalize_email(body.email)
    except auth.AuthError: return JSONResponse({"error": "Invalid or expired code."}, status_code=401)
    if not auth.otp_store.verify(email, body.code.strip()):
        return JSONResponse({"error": "Invalid or expired code."}, status_code=401)   # same message for wrong/expired/locked
    request.session.clear()
    request.session["user"] = {"sub": hashlib.sha256(email.encode()).hexdigest(), "name": email, "email": email,
                               "exp": time.time() + C.SESSION_TTL}
    return {"ok": True}

@app.get("/auth/login")
async def login(request: Request):
    if C.AUTH_MODE != "entra": return JSONResponse({"error": "not enabled"}, status_code=404)
    st = auth.new_login_state(); request.session["oidc"] = st
    return RedirectResponse(auth.authorize_url(st))

@app.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if C.AUTH_MODE != "entra": return JSONResponse({"error": "not enabled"}, status_code=404)
    st = request.session.pop("oidc", None)
    if error or not st or not code or not secrets.compare_digest(state, st["state"]):
        return JSONResponse({"error": "Sign-in failed."}, status_code=400)
    try:
        user = await asyncio.to_thread(auth.validate_id_token, await auth.exchange_code(code, st["verifier"]), st["nonce"])
    except auth.AuthError as e:
        log.warning("sign-in rejected: %s", e)
        return JSONResponse({"error": "Sign-in rejected: this account is not eligible for restricted access."}, status_code=403)
    request.session.clear()                       # fresh session on privilege change (anti-fixation)
    request.session["user"] = {**user, "exp": time.time() + C.SESSION_TTL}
    return RedirectResponse("/")

@app.post("/auth/logout")
async def logout(request: Request):
    request.session.clear(); return {"ok": True}

@app.get("/me")
async def me(request: Request):
    u = current_user(request)
    return {"tier": "internal" if u else "public", "name": u and u["name"], "email": u and u["email"],
            "login_required": C.REQUIRE_LOGIN, "auth_mode": C.AUTH_MODE, "backend": C.BACKEND}

def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"

def trace(rec: dict):
    try:
        with open(C.TRACE_FILE, "a") as f: f.write(json.dumps(rec) + "\n")
    except OSError: pass

async def run_model(which, messages, out: dict):
    """Yields ('token', text) and finally stores text/conf in `out`."""
    parts, lps = [], []
    async for tok, lp in stream_chat(which, messages):
        parts.append(tok)
        if lp is not None: lps.append(lp)
        yield tok
    out["text"], out["conf"] = "".join(parts), mean_prob(lps)

@app.post("/chat")
async def chat(req: ChatReq, request: Request):
    user = current_user(request); tier = "internal" if user else ("public" if C.ACCESS_TIERS else "open")
    if C.REQUIRE_LOGIN and not user:
        return JSONResponse({"error": "Sign in with your IISc email to use AskIISc."}, status_code=401)
    if C.BACKEND == "gateway":
        return StreamingResponse(gateway_backend.stream_via_gateway(req.message, tier), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    async def gen():
        t0 = time.perf_counter(); tr = {"ts": time.time(), "tier": tier, "user": user and hashlib.sha256(user["sub"].encode()).hexdigest()[:10], "q_len": len(req.message), "thesis": req.thesis_id}
        try:
            qvec = await embed_query(req.message)
            t1 = time.perf_counter()
            rt, probe = app.state.retriever, []
            if tier == "public":    # public users: ONLY public chunks reach the prompt; probe restricted just to explain a refusal
                res = await asyncio.gather(rt.search(qvec, req.thesis_id, access="public"),
                                           rt.search(qvec, req.thesis_id, k=1, access="restricted"), return_exceptions=True)
                chunks = res[0] if not isinstance(res[0], Exception) else None
                probe = res[1] if not isinstance(res[1], Exception) else []
            else:
                try: chunks = await rt.search(qvec, req.thesis_id)
                except Exception as e: chunks = e
            if isinstance(chunks, Exception) or chunks is None:     # Redis down / index missing: answer is ungrounded -> say so
                log.warning("retrieval failed"); chunks = []
                yield sse("warning", {"msg": "Retrieval failed; answering without sources."})
            t2 = time.perf_counter()
            if probe and probe[0].distance < C.SENSITIVE_DIST and (not chunks or probe[0].distance < chunks[0].distance):
                yield sse("refused", {"msg": C.REFUSAL_MSG, "login": "/auth/login"})
                yield sse("done", {"model": None, "refused": True, "timings": {"embed_ms": round((t1 - t0) * 1e3), "retrieve_ms": round((t2 - t1) * 1e3), "total_ms": round((t2 - t0) * 1e3)}})
                trace(tr | {"refused": True, "s": None}); return
            r = app.state.router.route(req.message, qvec, req.force)
            first = r["route"]
            def used(which): return chunks[:C.SLM_TOP_K] if which == "slm" else chunks   # CPU SLM reads fewer chunks
            def cites(which): return [{"n": i, **c.cite()} for i, c in enumerate(used(which), 1)]
            yield sse("meta", {"tier": tier, "route": first, "s": round(r["s"], 3), "alpha": app.state.router.alpha,
                               "reason": r["reason"], "citations": cites(first)})
            order = [first] + (["llm" if first == "slm" else "slm"] if req.force not in ("slm", "llm") else [])
            final = None
            for which in order:
                if which != first: yield sse("sources", {"citations": cites(which)})   # different model -> different context
                msgs = build_messages(req.message, used(which), req.history, req.thesis_id)
                out, tstart = {}, time.perf_counter()
                try:
                    ttft = None
                    async for tok in run_model(which, msgs, out):
                        if ttft is None: ttft = time.perf_counter() - tstart
                        yield sse("token", {"t": tok})
                except ModelError as e:
                    log.warning("%s failed: %s", which, e)
                    if which != order[-1]:
                        yield sse("fallback", {"from": which, "to": order[-1], "why": str(e)[:160]})
                        continue
                    yield sse("error", {"msg": f"No model reachable: {e}"}); return
                conf = out.get("conf")
                # Cascade: SLM low confidence -> discard and rerun on the 7B
                if (C.CASCADE and which == "slm" and req.force != "slm" and conf is not None
                        and conf < C.CASCADE_MIN_CONF and len(order) > 1):
                    yield sse("escalate", {"conf": round(conf, 3), "threshold": C.CASCADE_MIN_CONF})
                    final = {"model": which, "escalated": True}; continue
                final = {"model": which, "conf": conf, "ttft": ttft, "escalated": bool(final)}
                break
            t3 = time.perf_counter()
            tm = {"embed_ms": (t1 - t0) * 1e3, "retrieve_ms": (t2 - t1) * 1e3, "total_ms": (t3 - t0) * 1e3}
            yield sse("done", {"model": final and final["model"], "timings": {k: round(v) for k, v in tm.items()},
                               "conf": final and final.get("conf") and round(final["conf"], 3),
                               "escalated": bool(final and final.get("escalated"))})
            trace(tr | {"route0": first, "s": r["s"], "final": final, "chunks": [c.id for c in chunks], **tm})
        except Exception as e:
            log.exception("chat failed"); yield sse("error", {"msg": f"{type(e).__name__}: {e}"})
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.get("/theses")
async def theses(request: Request):
    u = current_user(request)
    if C.REQUIRE_LOGIN and not u: return JSONResponse({"error": "sign in"}, status_code=401)
    if C.BACKEND == "gateway": return []          # thesis filter is not offered by the gateway
    return await app.state.retriever.theses(None if (u or not C.ACCESS_TIERS) else "public")

@app.get("/health")
async def health():
    import httpx
    if C.BACKEND == "gateway":
        try:
            async with httpx.AsyncClient(timeout=3) as cl: r = await cl.get(f"{C.GATEWAY_URL}/healthz")
            return {"backend": "gateway", "gateway": r.status_code == 200, "gateway_health": r.json() if r.status_code == 200 else None}
        except Exception as e: return {"backend": "gateway", "gateway": False, "error": type(e).__name__}
    async def probe(url):
        try:
            async with httpx.AsyncClient(timeout=2) as cl: return (await cl.get(f"{url}/models")).status_code < 500
        except Exception: return False
    try: rd = await app.state.retriever.ping()
    except Exception: rd = False
    return {"redis": rd, "slm": await probe(C.SLM_URL), "llm": await probe(C.LLM_URL),
            "router_model_loaded": app.state.router.loaded, "alpha": app.state.router.alpha}

@app.get("/")
async def index(): return FileResponse(os.path.join(STATIC, "index.html"))

if __name__ == "__main__":
    import uvicorn; uvicorn.run(app, host=C.BIND_HOST, port=C.BIND_PORT)
