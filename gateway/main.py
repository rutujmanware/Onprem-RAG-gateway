import asyncio
import json
import logging
import os
import sys
import time
import uuid
import secrets
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
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

from gateway.auth.internal import consteq, verify_internal_user
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
from pipeline.rag_chain import GPU_FALLBACK_REASON, RAGQAPipeline, _gpu_inprocess_fallback_enabled
from pipeline.retriever import (RETRIEVAL_RETRY_AFTER_SECONDS, RETRIEVAL_UNAVAILABLE_MESSAGE, RetrievalUnavailable,
                                is_retrieval_outage, sanitize_chunks)
from pipeline.metadata_queries import METADATA_QUERY_MESSAGE, is_metadata_query
from pipeline.rag_chain import metadata_query_events, metadata_query_result
from pipeline.sanitizer import (sanitize_and_fence_prompt, classifier_fence, PromptInjectionAlert,
                                is_no_relevant_docs_answer)

logger = logging.getLogger("gateway.main")


_STREAM_END = object()


class ReleasingStreamingResponse(StreamingResponse):
    """StreamingResponse that always runs `on_close` when the response ends, also when the client disconnects
    before streaming starts (the generator's own finally then never runs). DEFECT-34: the admission slot taken
    before the response is returned is released on every path; ticket release is idempotent."""

    def __init__(self, *args, on_close=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self._on_close is not None:
                self._on_close()


async def closing_stream(gen):
    """Serve a sync generator from a worker thread and ALWAYS close it, client disconnect included.

    Starlette iterates sync generators in a thread but never closes them when the browser goes away,
    so their finally blocks (admission.release(), closing the upstream model stream) only ran at
    garbage collection, leaving model slots busy (DEFECT-17). Closing here runs them immediately.
    """
    import threading
    import anyio
    lock = threading.Lock()  # close() must wait for a next() still running in its thread

    def step():
        with lock:
            return next(gen, _STREAM_END)

    def close():
        with lock:
            gen.close()

    try:
        while True:
            item = await anyio.to_thread.run_sync(step)
            if item is _STREAM_END:
                return
            yield item
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(close)

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
    # Load router, embedder, retriever and spell dictionaries now, not on a user's first question
    # (the spell dictionaries alone take ~2.5 s per worker process).
    try:
        from pipeline.rag_chain import warm_up
        t_warm = time.time()
        await run_in_threadpool(warm_up)
        logger.info(f"Retrieval warm-up done in {time.time() - t_warm:.2f}s")
    except Exception as e:
        logger.warning(f"Retrieval warm-up skipped: {e}")
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
    if _is_loopback(request) or _has_live_session(request) or _has_admin_key(request):
        return True
    raise HTTPException(status_code=404, detail="Not Found")


def require_admin_key(request: Request):
    """Cache/queue administration (/cache/*, /queue/stats): ADMIN_API_KEY only (X-Admin-Key or Bearer).

    DEFECT-24: a login session is never enough (these routes clear or purge the cache every user shares).
    DEFECT-29: neither is a request from the server itself; this is a shared host, so "loopback" means any
    local user. Only key holders administer; with ADMIN_API_KEY unset nobody can. Returns 404 to hide the routes.
    """
    if _has_admin_key(request):
        return True
    raise HTTPException(status_code=404, detail="Not Found")


def _temperature(t: Optional[float]) -> float:
    """0 is a valid temperature (greedy); only a missing value means the default (DEFECT-47)."""
    return 0.2 if t is None else float(t)


def _caller_id(request: Request) -> str:
    """Who is asking, for rate limiting at arrival (same identity the handlers use later)."""
    internal = _internal_user(request)
    if internal:
        return internal
    if "session" in request.scope:
        u = request.session.get("user")
        if u and isinstance(u, dict) and u.get("email"):
            return u["email"]
    return (request.client.host if request.client else "") or "anonymous"


def _strike_identity(request: Request) -> Optional[str]:
    """DEFECT-59: who a prompt-injection refusal counts against (repeat offenders are blocked for a while).
    Identified callers only: the signed-in session, the chat bridge's signed user, or a network address. Unsigned
    local callers (scripts and test suites on this server, which send attack sets on purpose; inside the perimeter,
    PRD L23) are not counted, so they can never block "127.0.0.1" for everyone on the host."""
    internal = _internal_user(request)
    if internal:
        return internal
    if "session" in request.scope:
        u = request.session.get("user")
        if u and isinstance(u, dict) and u.get("email"):
            return u["email"]
    if _is_loopback(request):
        return None
    return (request.client.host if request.client else "") or None


async def _check_offender(admission: AdmissionController, request: Request) -> None:
    who = _strike_identity(request)
    if who:
        await admission.check_injection_block(who)


async def _injection_refused(admission: AdmissionController, request: Request,
                             e: PromptInjectionAlert) -> HTTPException:
    """The 400 for a refused question; an injection (not a language) refusal is also a strike (DEFECT-59)."""
    from pipeline.language_gate import LanguageNotSupported
    who = _strike_identity(request)
    if who and not isinstance(e, LanguageNotSupported):
        await admission.record_injection_refusal(who)
    # e is always a PromptInjectionAlert (a fixed message), never an internal error
    return HTTPException(status_code=400, detail=str(e))


async def _cache_lookup(cache, query: str, raw_query: str, owner: str):
    """Cache lookup for the normalised question, then the question as typed (DEFECT-40).

    DEFECT-62: when the typo normaliser changed the question, a hit found for the corrected text is also checked
    against the question as typed with the intent guard (`check_entity_conflict`), exact hits included: an exact hit
    returns at once and never ran the guard, so a rewritten term (ferrielectric -> ferroelectric) was served the other
    term's cached answer at similarity 1.0. Typos pass the guard (a misspelling is not a real word), so they still hit.
    """
    hit = await cache.alookup(query, owner=owner)
    if hit is not None and query != raw_query:
        conflict, reason = RedisSemanticCache.check_entity_conflict(raw_query, hit.get("matched_prompt") or query)
        if conflict:
            logger.info("Cache hit for the corrected question refused (%s): %r", reason, raw_query)
            hit = None
    if hit is None and query != raw_query:
        hit = await cache.alookup(raw_query, owner=owner)
    return hit


def _fence_messages(messages) -> None:
    """Patterns on every message, English-only on user messages. The classifier runs later, on a cache miss
    (`_classify_messages`, DEFECT-51)."""
    for m in messages:
        sanitize_and_fence_prompt(m.content or "", use_classifier=False,
                                  user_message=(m.role or "user").lower() == "user")


def _classify_messages(messages) -> None:
    for m in messages:
        if (m.role or "user").lower() == "user":
            classifier_fence(m.content or "")


async def _classifier_gate(fn, arg, admission: AdmissionController, request: Request) -> None:
    """Embedding-classifier half of the injection fence (DEFECT-51): run on every cache miss, before retrieval,
    generation or any cache write. A cache hit calls no model and serves an answer stored for a question that
    passed the full fence (the same normalised text, or the user's own earlier question), so hits skip it."""
    try:
        await run_in_threadpool(fn, arg)
    except PromptInjectionAlert as e:
        raise await _injection_refused(admission, request, e)


def _internal_user(request: Request) -> Optional[str]:
    """The user named by the web chat bridge (`X-Internal-User`), only from loopback and only with a valid
    `X-Internal-Auth` signature (DEFECT-30: on this shared host any local user could otherwise claim any
    identity). A header that is present but not validly signed is refused with 403."""
    user = request.headers.get("x-internal-user", "").strip()
    if not user or not _is_loopback(request):
        return None
    if not verify_internal_user(user, request.headers.get("x-internal-auth", "").strip()):
        raise HTTPException(status_code=403, detail="Invalid internal user identity.")
    return user


def _is_loopback(request: Request) -> bool:
    # Security note (VULN-01 fix): Never trust client-supplied X-Forwarded-For headers
    # to authenticate loopback access (CWE-290). Only direct local socket connections
    # without forwarding headers are permitted unauthenticated loopback access.
    client_host = request.client.host if request.client else ""
    return not request.headers.get("x-forwarded-for") and client_host in ("127.0.0.1", "::1", "localhost", "testclient")


def _has_live_session(request: Request) -> bool:
    if "session" not in request.scope:
        return False
    user = request.session.get("user")
    return bool(user and isinstance(user, dict) and user.get("exp", 0) > time.time())


def _has_admin_key(request: Request) -> bool:
    # VULN-08: constant-time comparison prevents timing side-channels
    if not ADMIN_KEY:
        return False
    auth_hdr = request.headers.get("X-Admin-Key") or request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    return bool(auth_hdr) and consteq(auth_hdr, ADMIN_KEY)   # DEFECT-54: never raises on non-ASCII

# Session Security (VULN-02 Remediation):
# Enforce high-entropy cryptographic session secret. Never allow known weak or fallback keys.
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
KNOWN_INSECURE_SECRETS = {
    "e7b4c91a3f8d2e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a",
    "CHANGE_ME",
    "secret",
    "test-secret",
    "supersecret",
}

if not SESSION_SECRET or SESSION_SECRET in KNOWN_INSECURE_SECRETS or len(SESSION_SECRET) < 32:
    if os.getenv("ENVIRONMENT", "").lower() in ("production", "prod"):
        raise RuntimeError(
            "FATAL: Insecure or missing SESSION_SECRET in production. "
            "Generate a secure 256-bit key using: python3 -c 'import secrets; print(secrets.token_hex(32))'"
        )
    # DEFECT-48: one generated secret shared by all workers of this checkout (owner-only file), and exported, so
    # sessions work across workers and the signed internal identity (DEFECT-30) keeps working in development.
    _dev_secret_file = os.path.join(BASE_DIR, ".dev_session_secret")
    try:
        fd = os.open(_dev_secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    with open(_dev_secret_file) as fh:
        SESSION_SECRET = fh.read().strip()
    os.environ["SESSION_SECRET"] = SESSION_SECRET
    logger.warning("SESSION_SECRET is unset or weak: using the generated development secret in %s. "
                   "Set SESSION_SECRET in .env for production.", _dev_secret_file)

MAX_REQUEST_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(256 * 1024)))


class BodySizeLimit:
    """DEFECT-48: reject request bodies over MAX_REQUEST_BODY_BYTES (HTTP 413) before they are read into memory;
    checks Content-Length and counts streamed/chunked bodies. Largest legitimate body: 50 chat messages of
    4,000 characters (~200 KB)."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = MAX_REQUEST_BODY_BYTES
        for k, v in scope.get("headers") or []:
            if k == b"content-length":
                try:
                    if int(v) > limit:
                        return await _too_large(send)
                except ValueError:
                    return await _too_large(send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send):
    body = b'{"detail":"Request body too large."}'
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


ALLOWED_CIDRS_GATE = [c.strip() for c in os.getenv("GATEWAY_ALLOWED_CIDRS", "").split(",") if c.strip()]


@app.middleware("http")
async def network_gate(request: Request, call_next):
    """DEFECT-48: optional network gate (set GATEWAY_ALLOWED_CIDRS, e.g. the IISc ranges). Off when unset, so an
    unconfigured deployment is not locked out. Loopback is always allowed (the web chat bridge)."""
    if ALLOWED_CIDRS_GATE:
        import ipaddress
        host = request.client.host if request.client else ""
        try:
            ip = ipaddress.ip_address(host)
            ok = ip.is_loopback or any(ip in ipaddress.ip_network(c, strict=False) for c in ALLOWED_CIDRS_GATE)
        except ValueError:
            ok = host in ("testclient", "localhost")
        if not ok:
            return JSONResponse({"detail": "Access is restricted to the IISc network."}, status_code=403)
    return await call_next(request)


class HideGuardedRoutes:
    """DEFECT-54: a caller that a route's access guard would reject gets exactly the response of an unknown path.

    Hiding used to be patched per response type (405 wrong method, 422 bad body), and other responses still
    revealed the routes to any campus host: /query/ redirected (307) to /query, a body that is not UTF-8 got
    "400 error parsing the body", and a non-ASCII admin key crashed the key comparison (500). Now the guard runs
    before routing, body parsing and the route itself; a rejected request is routed to a path that does not
    exist, so every method, body, header and trailing-slash variant answers the same 404 as a missing page.
    Runs inside SessionMiddleware (the guards accept a live session) and is registered before it for that reason.
    """

    _HIDDEN_PATH = "/.not-found"

    def __init__(self, app_):
        self.app = app_
        self._guards = None

    def _guard_for(self, path: str):
        if self._guards is None:   # built on first use: routes are declared further down this module
            self._guards = {}
            for route in app.router.routes:
                for dep in getattr(route, "dependencies", None) or ():
                    if dep.dependency in _ROUTE_GUARDS:
                        self._guards[route.path] = dep.dependency
        return self._guards.get(path.rstrip("/") or "/")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            guard = self._guard_for(scope.get("path", ""))
            if guard is not None:
                try:
                    guard(Request(scope))
                    allowed = True
                except Exception:   # HTTPException 404 from the guard, or anything unexpected: hidden
                    allowed = False
                if not allowed:
                    scope = {**scope, "path": self._HIDDEN_PATH, "raw_path": self._HIDDEN_PATH.encode()}
        await self.app(scope, receive, send)


app.add_middleware(HideGuardedRoutes)

from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=os.getenv("COOKIE_SECURE", "0") == "1",
    same_site="lax",
    max_age=int(os.getenv("SESSION_TTL", str(8 * 3600))),
    session_cookie="askiisc_session",
)
app.add_middleware(BodySizeLimit)


# DEFECT-55: browser hardening for every response (page, API, errors, hidden routes alike).
# script-src 'self': the page's logic lives in /static/app.js, so a script injected into an answer cannot run.
# img-src 'self' data:: an answer cannot make the reader's browser load an outside image (which would carry the
# question or answer to another server without a click). style-src keeps 'unsafe-inline' for the page's <style>
# block and KaTeX's math layout. frame-ancestors / X-Frame-Options: no other site can frame the sign-in page.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
]


class SecurityHeaders:
    """Adds SECURITY_HEADERS to every HTTP response. Responses outside /static/ (the page, /me with the user's
    email and CSRF token, answers, errors) are never stored by browsers or proxies (Cache-Control: no-store);
    /static/app.js is revalidated on every load so a deploy takes effect at once; vendored libraries keep the
    default caching."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        cache = None if path.startswith("/static/vendor/") else (
            b"no-cache" if path.startswith("/static/") else b"no-store")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                headers = [h for h in message.get("headers", []) if not (cache and h[0].lower() == b"cache-control")]
                headers += [h for h in SECURITY_HEADERS if h[0] not in names]
                if cache:
                    headers.append((b"cache-control", cache))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)   # outermost: also covers 413s from BodySizeLimit

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]   # DEFECT-41: no other roles
    content: str = Field(..., min_length=1, max_length=4000)

class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., min_length=1, max_length=50)
    temperature: Optional[float] = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=8192)
    stream: Optional[bool] = False

class DirectQueryRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=1500)
    top_k: Optional[int] = Field(default=None, ge=1, le=50)
    temperature: Optional[float] = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=2048, ge=1, le=8192)
    stream: Optional[bool] = False
    bypass_cache: Optional[bool] = False
    # Single-use token issued only with a cached answer; requests a fresh answer for that same question.
    regenerate_token: Optional[str] = Field(default=None, max_length=128)


_ROUTE_GUARDS = (require_internal_or_auth, require_admin_key)


def _route_guard(request: Request, any_method: bool = False):
    for route in app.router.routes:
        if getattr(route, "path", None) == request.url.path and (
                any_method or request.method in (getattr(route, "methods", None) or ())):
            for dep in getattr(route, "dependencies", None) or ():
                if dep.dependency in _ROUTE_GUARDS:
                    return dep.dependency
    return None


@app.exception_handler(StarletteHTTPException)
async def hide_guarded_routes_on_wrong_method(request: Request, exc: StarletteHTTPException):
    """A wrong HTTP method on a hidden route (e.g. POST /cache/stats) answered 405, revealing the route.
    A caller its guard would reject now gets the same 404 as for any unknown path."""
    if exc.status_code == 405:
        guard = _route_guard(request, any_method=True)
        if guard is not None:
            try:
                guard(request)
            except HTTPException as e:
                return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def hide_guarded_routes_on_bad_body(request: Request, exc: RequestValidationError):
    """FastAPI parses and validates the body before running route dependencies, so a malformed body
    used to get 422 from a hidden route before its access guard ran, revealing that the route exists.
    An unauthorised caller now gets the same 404 as for a well-formed request."""
    guard = _route_guard(request)
    if guard is not None:
        try:
            guard(request)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await request_validation_exception_handler(request, exc)


def _metadata_sse():
    for e in metadata_query_events():
        yield f"data: {json.dumps(e)}\n\n"
    yield "data: [DONE]\n\n"


CACHE_MAX_TEMPERATURE = 0.3
CACHE_MIN_MAX_TOKENS = 512


def standard_generation(temperature: Optional[float], max_tokens: Optional[int], top_k: Optional[int] = None) -> bool:
    """DEFECT-39: only answers generated with the standard settings may be cached. Shared exact entries are
    write-once, so whoever asks first sets the answer everyone gets; with this rule a caller cannot make a
    high-temperature or deliberately truncated answer the cached one. Such requests are answered, not stored.
    DEFECT-56: the same for a caller-chosen number of source documents (`top_k`, /query only): `top_k: 1` made a
    one-document answer the shared entry for 4 hours. The tier's own default context size is the standard."""
    t = 0.2 if temperature is None else temperature
    return (t <= CACHE_MAX_TEMPERATURE and (max_tokens is None or max_tokens >= CACHE_MIN_MAX_TOKENS)
            and top_k is None)


def _retrieval_unavailable_response() -> JSONResponse:
    return JSONResponse({"detail": RETRIEVAL_UNAVAILABLE_MESSAGE}, status_code=503,
                        headers={"Retry-After": str(RETRIEVAL_RETRY_AFTER_SECONDS)})


@app.exception_handler(RetrievalUnavailable)
async def retrieval_unavailable_handler(request: Request, exc: RetrievalUnavailable):
    """DEFECT-28: Redis down / loading or the thesis index missing -> HTTP 503 (details in the log)."""
    return _retrieval_unavailable_response()


try:
    import redis.exceptions as _redis_exc

    @app.exception_handler(_redis_exc.ConnectionError)
    @app.exception_handler(_redis_exc.TimeoutError)
    async def redis_unavailable_handler(request: Request, exc: Exception):
        """Safety net for any other Redis call on a request path (includes BusyLoadingError)."""
        logger.warning("Redis unavailable on %s: %r", request.url.path, exc, exc_info=exc)
        return _retrieval_unavailable_response()
except ImportError:  # pragma: no cover
    pass


@app.get("/healthz", include_in_schema=False)
@app.get("/health", include_in_schema=False)
async def healthz():
    """Liveness: the gateway process is up. Readiness (can it answer?) is /ready."""
    return {"status": "ok"}


async def _readiness_checks(cache, slm, vllm) -> Dict[str, bool]:
    async def redis_index() -> bool:
        try:
            names = await asyncio.wait_for(cache.async_client.execute_command("FT._LIST"), timeout=2.0)
            names = {n.decode() if isinstance(n, bytes) else str(n) for n in names}
            return os.getenv("REDIS_INDEX_NAME", "rag_documents_idx") in names
        except Exception:
            return False

    async def up(client) -> bool:
        try:
            return bool(await asyncio.wait_for(client.check_health(), timeout=4.0))
        except Exception:
            return False

    def embedder_ok() -> bool:
        try:
            from pipeline.embeddings.embedder import LocalEmbedder
            e = LocalEmbedder()
            return e._model is not None or getattr(e, "_st_model", None) is not None
        except Exception:
            return False

    r, c, g, e = await asyncio.gather(redis_index(), up(slm), up(vllm), run_in_threadpool(embedder_ok))
    return {"redis_thesis_index": r, "embedding_model": e, "cpu_model": c, "gpu_model": g}


@app.get("/ready", include_in_schema=False)
async def ready(request: Request, cache: RedisSemanticCache = Depends(get_semantic_cache),
                slm: CPUSLMClient = Depends(get_slm_client), vllm: VLLMClient = Depends(get_vllm_client)):
    """Readiness (DEFECT-42): 200 only if Redis with the thesis index is reachable and at least one model tier
    answers health checks (the CPU tier alone suffices: hard questions fall back to it, FR-2); else 503.
    Which dependency failed is shown only to the server itself or an admin-key holder."""
    checks = await _readiness_checks(cache, slm, vllm)
    ok = checks["redis_thesis_index"] and checks["embedding_model"] and (checks["cpu_model"] or checks["gpu_model"])
    body: Dict[str, Any] = {"status": "ready" if ok else "not_ready"}
    if _is_loopback(request) or _has_admin_key(request):
        body["checks"] = checks
    return JSONResponse(body, status_code=200 if ok else 503, headers={"Cache-Control": "no-store"})

@app.get("/cache/stats", dependencies=[Depends(require_admin_key)], include_in_schema=False)
async def cache_stats(cache: RedisSemanticCache = Depends(get_semantic_cache)):
    count = await cache.acount()
    return {
        "status": "active",
        "cache_type": "RedisSemanticCache",
        "index_name": cache.index_name,
        "key_prefix": cache.key_prefix,
        "similarity_threshold": cache.similarity_threshold,
        "regenerations_logged": cache.regen_event_count(),
        "vector_dim": cache.vector_dim,
        "cached_queries_count": count,
    }

class CachePurgeRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=1500)
    threshold: Optional[float] = Field(default=0.85, ge=0.5, le=1.0)

@app.post("/cache/purge", dependencies=[Depends(require_admin_key)], include_in_schema=False)
async def cache_purge_query(
    req: CachePurgeRequest,
    cache: RedisSemanticCache = Depends(get_semantic_cache),
):
    purged = await cache.apurge(req.query, threshold=req.threshold or 0.85)
    return {
        "status": "ok",
        "purged_count": purged,
        "query": req.query,
        "threshold": req.threshold or 0.85,
    }

@app.post("/cache/clear", dependencies=[Depends(require_admin_key)], include_in_schema=False)
async def cache_clear(cache: RedisSemanticCache = Depends(get_semantic_cache)):
    success = await cache.aclear()
    return {"status": "cleared" if success else "failed"}

@app.post("/cache/purge-invalid", dependencies=[Depends(require_admin_key)], include_in_schema=False)
async def cache_purge_invalid(cache: RedisSemanticCache = Depends(get_semantic_cache)):
    purged = await cache.apurge_uncacheable_entries()
    return {"status": "ok", "purged_count": purged}

@app.get("/queue/stats", dependencies=[Depends(require_admin_key)], include_in_schema=False)
async def queue_stats(admission: AdmissionController = Depends(get_admission_controller)):
    return admission.get_stats()

@app.post("/v1/chat/completions", dependencies=[Depends(require_internal_or_auth)], include_in_schema=False)
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    cache: RedisSemanticCache = Depends(get_semantic_cache),
    scorer: ComplexityScorer = Depends(get_complexity_scorer),
    dispatcher: RequestDispatcher = Depends(get_dispatcher),
    slm: CPUSLMClient = Depends(get_slm_client),
    vllm: VLLMClient = Depends(get_vllm_client),
    admission: AdmissionController = Depends(get_admission_controller),
):
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty")

    if req.messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="The last message must be the user's question.")
    raw_query = req.messages[-1].content.strip()
    if not raw_query or len(raw_query) < 2:
        raise HTTPException(status_code=400, detail="Query message must contain at least 2 non-whitespace characters")
    # VULN-04 / DEFECT-31: validate EVERY message (any role) against the injection patterns, not only the
    # last one: the non-streaming branches send the whole message list to the model.
    await admission.charge(_caller_id(request))   # DEFECT-47: rate limit before any work, cache hits included
    await _check_offender(admission, request)     # DEFECT-59: blocked after repeated injection attempts
    try:
        await run_in_threadpool(_fence_messages, req.messages)   # classifier is CPU work (DEFECT-47)
    except PromptInjectionAlert as e:
        raise await _injection_refused(admission, request, e)

    # DEFECT-33: counting/listing theses by metadata is not supported; say so (never cached, no model call)
    if is_metadata_query(raw_query):
        cid = f"chatcmpl-meta-{uuid.uuid4().hex[:8]}"
        meta = {"tier": "none", "route": "unsupported", "metadata_query": True}
        if req.stream:
            def _meta_chat_sse():
                yield "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "model": "none",
                                             "choices": [{"index": 0, "delta": {"content": METADATA_QUERY_MESSAGE},
                                                          "finish_reason": "stop"}], "router_meta": meta}) + "\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(_meta_chat_sse(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return {"id": cid, "object": "chat.completion", "model": "none",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": METADATA_QUERY_MESSAGE},
                             "finish_reason": "stop"}], "router_meta": meta}

    from pipeline.text_processing.typo_normalizer import TypoNormalizer
    query = await run_in_threadpool(TypoNormalizer.get_instance().normalize, raw_query)

    t0 = time.time()

    # Identify caller for token-bucket and queue accounting
    client_host = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    client_id = client_host or "anonymous"

    internal_user = _internal_user(request)  # DEFECT-30: signed by the web chat bridge, else 403
    if internal_user:
        client_id = internal_user
    elif "session" in request.scope:
        u = request.session.get("user")
        if u and isinstance(u, dict) and u.get("email"):
            client_id = u["email"]

    # Step 0: Semantic Vector Cache Lookup
    try:
        hit = await _cache_lookup(cache, query, raw_query, client_id)   # DEFECT-62: rewrites checked
    except Exception:
        hit = None
    if hit is None:
        await _classifier_gate(_classify_messages, req.messages, admission, request)

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
            "user": client_id,   # the real caller (was a fixed placeholder identity)
        }

    # Step 1: Neural Complexity Routing (MLP Router)
    score = await scorer.score(query)
    decision = dispatcher.decide(score, query=query)

    if req.stream:
        admission_ticket = await admission.acquire(client_id, charge_rate=False)  # charged on arrival
        try:
            rag = RAGQAPipeline()
        except BaseException:
            admission.release(admission_ticket)
            raise
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())

        def sse_chat_generator():
            try:
                full_ans = []
                collected_chunks = []
                incomplete = False
                for event in rag.answer_query_stream(
                    query,
                    max_new_tokens=req.max_tokens,
                    temperature=_temperature(req.temperature),
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
                    elif event.get("type") == "error":  # e.g. retrieval outage (DEFECT-28)
                        err = {"error": {"message": event.get("error"), "type": "service_unavailable",
                                         "code": event.get("status", 503)}}
                        yield f"data: {json.dumps(err)}\n\n"
                        incomplete = True
                        break
                    elif event.get("type") == "chunks":
                        collected_chunks = event.get("chunks", [])
                    elif event.get("type") == "done":
                        incomplete = bool(event.get("incomplete"))
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

                # Save generated completion to cache only when document chunks were retrieved
                complete_text = "".join(full_ans)
                if (complete_text and collected_chunks and not incomplete
                        and standard_generation(req.temperature, req.max_tokens)):
                    try:
                        cache.store(query, {"answer": complete_text, "chunks": collected_chunks}, owner=client_id)
                        if query != raw_query:
                            cache.store(raw_query, {"answer": complete_text, "chunks": collected_chunks}, owner=client_id)
                    except Exception:
                        pass
                yield "data: [DONE]\n\n"
            finally:
                admission.release(admission_ticket)

        return ReleasingStreamingResponse(
            closing_stream(sse_chat_generator()),
            on_close=lambda: admission.release(admission_ticket),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Cold non-streaming generation: guarded by admission slot.
    # DEFECT-41: the same grounded pipeline as the streaming path and /query: retrieval, the answering rules and
    # security directive, the escaped context fence, GPU->CPU fallback. Only the caller's last user question is
    # used; caller-supplied system/assistant messages are checked by the injection fence but never forwarded.
    async with admission.slot(client_id, charge_rate=False):
        is_hard = decision.target_tier == ModelTier.GPU_LLM
        rag = RAGQAPipeline()
        res = await run_in_threadpool(
            rag.answer_query,
            query,
            max_new_tokens=req.max_tokens,
            temperature=_temperature(req.temperature),
            route_decision={"route": "hard" if is_hard else "easy",
                            "target_tier": decision.target_tier.value, "probability_hard": score},
        )
        ans_text = res.get("answer", "")
        retrieved_chunks = res.get("chunks") or []
        if (retrieved_chunks and not is_no_relevant_docs_answer(ans_text)
                and standard_generation(req.temperature, req.max_tokens)):
            await cache.astore(query, {"answer": ans_text, "chunks": retrieved_chunks}, owner=client_id)
            if query != raw_query:
                await cache.astore(raw_query, {"answer": ans_text, "chunks": retrieved_chunks}, owner=client_id)
        stats = res.get("stats") or {}
        meta = {
            "score": round(score, 4),
            "tier": res.get("target_tier", decision.target_tier.value),
            "route": res.get("route", "hard" if is_hard else "easy"),
            "reason": decision.reason,
            "chunks_retrieved": len(retrieved_chunks),
            "sources": [c.get("title") for c in sanitize_chunks(retrieved_chunks)],
            "routing_time_seconds": round(time.time() - t0, 4),
        }
        if res.get("fallback"):
            meta["fallback"] = res["fallback"]
        if query != raw_query:
            meta["normalized_query"] = query
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "model": stats.get("model") or ("Qwen/Qwen2.5-7B-Instruct" if is_hard else "qwen2.5-3b-instruct-cpu"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ans_text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": -1, "completion_tokens": stats.get("num_tokens", -1),
                      "total_tokens": stats.get("num_tokens", -1)},
            "router_meta": meta,
            "user": client_id,   # the real caller (was a fixed placeholder identity)
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
    raw_query = req.query.strip()
    if len(raw_query) < 2:
        raise HTTPException(status_code=400, detail="Query must contain at least 2 non-whitespace characters")
    await admission.charge(_caller_id(request))   # DEFECT-47: rate limit before any work, cache hits included
    await _check_offender(admission, request)     # DEFECT-59: blocked after repeated injection attempts
    # VULN-04: Validate prompt against adversarial injection & jailbreak patterns
    try:   # patterns + English only here; the classifier runs on a cache miss (DEFECT-51)
        await run_in_threadpool(lambda: sanitize_and_fence_prompt(raw_query, use_classifier=False, user_message=True))
    except PromptInjectionAlert as e:
        raise await _injection_refused(admission, request, e)

    from pipeline.text_processing.typo_normalizer import TypoNormalizer
    query = await run_in_threadpool(TypoNormalizer.get_instance().normalize, raw_query)

    t0 = time.time()

    # Identify caller for token-bucket and queue accounting
    client_host = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    client_id = client_host or "anonymous"

    # VULN-03: If forwarded by internal web chat proxy over direct loopback, attribute to the user
    internal_user = _internal_user(request)  # DEFECT-30: signed by the web chat bridge, else 403
    if internal_user:
        client_id = internal_user
    elif "session" in request.scope:
        u = request.session.get("user")
        if u and isinstance(u, dict) and u.get("email"):
            client_id = u["email"]

    # "Regenerate": only valid with a token that was issued together with a cached answer to this user
    # for this exact question. Capped per user; regenerating a SHARED answer only creates a private
    # override, and the shared entry is evicted once enough distinct users regenerate it.
    regen = None
    if req.regenerate_token:
        if not await cache.aregen_quota_ok(client_id):
            raise HTTPException(status_code=429, detail="Regenerate limit reached. Please try again later.")
        regen = await cache.aconsume_regen_token(req.regenerate_token, client_id, raw_query)
        if regen is None:
            raise HTTPException(status_code=403, detail="Regenerate is only available for an answer that was served from the cache.")
        evicted = (await cache.arecord_shared_regen(regen["matched_prompt"], client_id,
                                                    matched_hash=regen.get("matched_hash"))
                   if regen["kind"] == "exact" else False)
        await cache.alog_regen_event(regen, client_id, evicted)
    regen_kind = regen["kind"] if regen else None
    use_cache = (not req.bypass_cache) and regen is None
    may_store = (((not req.bypass_cache) or regen is not None)
                 and standard_generation(req.temperature, req.max_tokens, req.top_k))

    # DEFECT-33: counting/listing theses by metadata is not supported; say so (never cached, no model call)
    if is_metadata_query(raw_query):
        if req.stream:
            return StreamingResponse(_metadata_sse(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        res = metadata_query_result(raw_query)
        res["cached"] = False
        return res

    # Step 0: Check Semantic Vector Cache in Redis (Fast path < 5ms bypasses admission queue)
    hit = None
    if use_cache:
        try:
            hit = await _cache_lookup(cache, query, raw_query, client_id)   # DEFECT-62: rewrites checked
        except Exception:
            hit = None
    if hit is None:
        await _classifier_gate(classifier_fence, raw_query, admission, request)

    if hit is not None:
        cached_resp = hit.get("response", {})
        if isinstance(cached_resp, str):
            try:
                cached_resp = json.loads(cached_resp)
            except Exception:
                cached_resp = {"answer": cached_resp}

        sim = hit.get("similarity", 1.0)
        matched_prompt = hit.get("matched_prompt", query)
        cache_kind = "override" if hit.get("override") else ("exact" if hit.get("fast_exact") else "semantic")
        regen_token = await cache.aissue_regen_token(client_id, raw_query, cache_kind, matched_prompt, sim)

        if req.stream:
            def cached_stream():
                if query != raw_query:   # DEFECT-40: tell the user what was searched for
                    yield f"data: {json.dumps({'type': 'correction', 'normalized_query': query})}\n\n"
                # Yield cache hit notification event
                yield f"data: {json.dumps({'type': 'cache', 'status': 'HIT', 'similarity': sim, 'matched_prompt': matched_prompt, 'badge': 'Cached answer', 'cache_kind': cache_kind, 'regenerate_token': regen_token, 'cached_at': cached_resp.get('cached_at', time.time())})}\n\n"
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
        cached_resp["badge"] = "Cached answer"
        cached_resp["cache_kind"] = cache_kind
        cached_resp["regenerate_token"] = regen_token
        cached_resp["query"] = raw_query
        if query != raw_query:
            cached_resp["normalized_query"] = query
        cached_resp["provenance"] = {
            "status": "cached",
            "cached_at": cached_resp.get("cached_at", time.time()),
            "similarity": sim,
            "model_version": cached_resp.get("model_version", "institution-rag-v1"),
        }
        cached_resp["similarity"] = sim
        cached_resp["matched_prompt"] = matched_prompt
        cached_resp["routing_time_seconds"] = round(time.time() - t0, 4)
        return cached_resp

    # Cache Miss: Cold inference required. Guarded by Admission Queue Manager.
    if req.stream:
        admission_ticket = await admission.acquire(client_id, charge_rate=False)  # charged on arrival
        try:
            rag = RAGQAPipeline(top_k=req.top_k)
        except BaseException:
            admission.release(admission_ticket)
            raise

        def event_stream():
            try:
                collected_answer = []
                collected_chunks = []
                route_info = {}
                incomplete = False
                if query != raw_query:   # DEFECT-40: tell the user what was searched for
                    yield f"data: {json.dumps({'type': 'correction', 'normalized_query': query})}\n\n"
                for event in rag.answer_query_stream(
                    query,
                    top_k=req.top_k,
                    max_new_tokens=req.max_tokens,
                    temperature=_temperature(req.temperature),
                ):
                    etype = event.get("type")
                    if etype == "token":
                        collected_answer.append(event.get("delta", ""))
                    elif etype == "chunks":
                        event["chunks"] = sanitize_chunks(event.get("chunks", []))
                        collected_chunks = event["chunks"]
                    elif etype == "route":
                        route_info = event
                    elif etype == "done":
                        incomplete = bool(event.get("incomplete"))
                    yield f"data: {json.dumps(event)}\n\n"

                full_text = "".join(collected_answer)
                if is_no_relevant_docs_answer(full_text):
                    collected_chunks = []
                    yield f"data: {json.dumps({'type': 'chunks', 'chunks': []})}\n\n"

                # Only cache responses when document chunks were successfully retrieved and not a refusal
                if may_store and full_text and not incomplete and collected_chunks and not is_no_relevant_docs_answer(full_text):
                    try:
                        cache_payload = {
                            "query": raw_query,
                            "answer": full_text,
                            "route": route_info.get("route", "easy"),
                            "target_tier": route_info.get("target_tier", "cpu_slm"),
                            "chunks": collected_chunks,
                        }
                        if query != raw_query:
                            cache_payload["normalized_query"] = query
                        cache.store(query, cache_payload, owner=client_id, regen_kind=regen_kind)
                        if query != raw_query:
                            cache.store(raw_query, cache_payload, owner=client_id, regen_kind=regen_kind)
                    except Exception:
                        pass
                yield "data: [DONE]\n\n"
            finally:
                admission.release(admission_ticket)

        return ReleasingStreamingResponse(
            closing_stream(event_stream()),
            on_close=lambda: admission.release(admission_ticket),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming cold inference: wrap inside admission slot
    async with admission.slot(client_id, charge_rate=False):
        from pipeline.retriever import RedisChunkRetriever
        from pipeline.gpu_llm_runner import GPULLMEngine

        retriever = RedisChunkRetriever()
        t0_pre = time.time()

        # Step 1: Concurrent Fan-Out (Parallel Scoring + Async Retrieval)
        score_task = asyncio.create_task(scorer.score(query))
        retrieval_task = asyncio.create_task(retriever.asearch(query, top_k=req.top_k))

        try:
            score, candidate_chunks = await asyncio.gather(score_task, retrieval_task)
        except Exception as exc:
            if not is_retrieval_outage(exc):
                raise
            score_task.cancel()
            logger.warning("Retrieval unavailable: %r", exc, exc_info=exc)
            raise RetrievalUnavailable(RETRIEVAL_UNAVAILABLE_MESSAGE) from exc
        pre_duration = round(time.time() - t0_pre, 3)

        decision = dispatcher.decide(score, query=query)
        is_hard = (decision.target_tier == ModelTier.GPU_LLM)

        # Step 2: Dynamic In-Memory Tier Document Slicing
        if req.top_k is not None:
            chunks = candidate_chunks[:req.top_k]
        elif is_hard:
            chunks = candidate_chunks[:8]  # Up to 8 docs for GPU LLM 7B
        else:
            chunks = candidate_chunks[:3]  # Up to 3 docs for CPU SLM 3B

        if not chunks:
            refusal_text = "No specific institutional repository theses were found on this topic."
            res = {
                "query": raw_query,
                "answer": refusal_text,
                "route": "hard" if is_hard else "easy",
                "target_tier": "gpu_vllm" if is_hard else "cpu_slm",
                "model": "retriever-filter",
                "score": round(score, 4),
                "chunks": [],
                "cached": False,
                "stats": {
                    "retrieval_seconds": pre_duration,
                    "routing_time_seconds": round(time.time() - t0, 4),
                },
            }
            if query != raw_query:
                res["normalized_query"] = query
            return res

        if decision.target_tier == ModelTier.GPU_LLM:
            from pipeline.rag_chain import GPU_MAX_NEW_TOKENS
            # DEFECT-46: capped so prompt + answer fit the 7B context window (a larger request failed in vLLM)
            gpu_max_tokens = min(req.max_tokens if (req.max_tokens is not None and req.max_tokens > 0) else 2048,
                                 GPU_MAX_NEW_TOKENS)

            vllm_up = await vllm.check_health()
            if vllm_up:
                try:
                    messages = GPULLMEngine.build_rag_messages(query, chunks)
                    t_gen = time.time()
                    resp = await vllm.generate_chat(
                        messages,
                        max_tokens=gpu_max_tokens,
                        temperature=_temperature(req.temperature),
                    )
                    gen_seconds = time.time() - t_gen
                    answer_text = resp["choices"][0]["message"]["content"]
                    n_tok = (resp.get("usage") or {}).get("completion_tokens") or len(answer_text.split())
                    served_chunks = [] if is_no_relevant_docs_answer(answer_text) else sanitize_chunks(chunks)
                    res = {
                        "query": raw_query,
                        "answer": answer_text,
                        "route": "hard",
                        "target_tier": "gpu_vllm",
                        "model": resp.get("model", "Qwen/Qwen2.5-7B-Instruct"),
                        "score": round(score, 4),
                        "chunks": served_chunks,
                        "cached": False,
                        "stats": {   # same fields as the CPU tier's answers (NFR-3 accounting)
                            "retrieval_seconds": pre_duration,
                            "routing_time_seconds": round(time.time() - t0, 4),
                            "generation_seconds": round(gen_seconds, 3),
                            "num_tokens": n_tok,
                            "tokens_per_second": round(n_tok / max(gen_seconds, 0.001), 2),
                            "model": resp.get("model", "Qwen/Qwen2.5-7B-Instruct"),
                            "server": "gpu-daemon-port-8000",
                        },
                    }
                    if query != raw_query:
                        res["normalized_query"] = query
                    if may_store and served_chunks and not is_no_relevant_docs_answer(answer_text):
                        await cache.astore(query, res, owner=client_id, regen_kind=regen_kind)
                        if query != raw_query:
                            await cache.astore(raw_query, res, owner=client_id, regen_kind=regen_kind)
                    return res
                except Exception as exc:
                    logger.warning("GPU tier could not answer (%s); rerouting to the CPU tier", exc)

            # FR-2: the GPU tier is down or failed for this request -> the chain answers on the CPU tier
            # (skip_gpu: no second GPU attempt, so a timing-out GPU is not waited for twice).
            rag = RAGQAPipeline(top_k=req.top_k)
            res = await run_in_threadpool(
                rag.answer_query,
                query,
                top_k=req.top_k,
                max_new_tokens=gpu_max_tokens,
                temperature=_temperature(req.temperature),
                route_decision={"route": "hard", "target_tier": "gpu_vllm", "probability_hard": score,
                                "skip_gpu": True},
                chunks=chunks,
            )
            if "chunks" in res:
                if is_no_relevant_docs_answer(res.get("answer", "")):
                    res["chunks"] = []
                else:
                    res["chunks"] = sanitize_chunks(res["chunks"])
            res["query"] = raw_query
            if query != raw_query:
                res["normalized_query"] = query
            res["cached"] = False
            res["routing_time_seconds"] = round(time.time() - t0, 4)
            if may_store and res.get("chunks") and not is_no_relevant_docs_answer(res.get("answer", "")):
                await cache.astore(query, res, owner=client_id, regen_kind=regen_kind)
                if query != raw_query:
                    await cache.astore(raw_query, res, owner=client_id, regen_kind=regen_kind)
            return res

        # Route: Easy -> CPU SLM
        slm_max_tokens = req.max_tokens if (req.max_tokens is not None and req.max_tokens > 0) else 1024
        rag = RAGQAPipeline(top_k=req.top_k)
        res = await run_in_threadpool(
            rag.answer_query,
            query,
            top_k=req.top_k,
            max_new_tokens=slm_max_tokens,
            temperature=_temperature(req.temperature),
            route_decision={"route": "easy", "target_tier": "cpu_slm", "probability_hard": score},
            chunks=chunks,
        )
        if "chunks" in res:
            if is_no_relevant_docs_answer(res.get("answer", "")):
                res["chunks"] = []
            else:
                res["chunks"] = sanitize_chunks(res["chunks"])
        res["query"] = raw_query
        if query != raw_query:
            res["normalized_query"] = query
        res["route"] = "easy"
        res["target_tier"] = "cpu_slm"
        res["score"] = round(score, 4)
        res["cached"] = False
        res["routing_time_seconds"] = round(time.time() - t0, 4)
        if may_store and res.get("chunks") and not is_no_relevant_docs_answer(res.get("answer", "")):
            await cache.astore(query, res, owner=client_id, regen_kind=regen_kind)
            if query != raw_query:
                await cache.astore(raw_query, res, owner=client_id, regen_kind=regen_kind)
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
        message: str = Field(..., min_length=1, max_length=1500)
        history: Optional[List[Dict[str, Any]]] = Field(default=[], max_length=50)
        thesis_id: Optional[str] = Field(default=None, max_length=100)
        force: Optional[str] = Field(default=None, max_length=20)
        regenerate_token: Optional[str] = Field(default=None, max_length=128)

    CSRF_COOKIE_NAME = "csrftoken"
    CSRF_HEADER_NAME = "x-csrf-token"

    def generate_csrf_token() -> str:
        return secrets.token_urlsafe(32)

    def set_csrf_cookie(response: Any, token: str, request: Optional[Request] = None):
        is_secure = (os.getenv("COOKIE_SECURE", "0") == "1") or (
            request is not None and (
                request.url.scheme == "https" or
                request.headers.get("x-forwarded-proto") == "https"
            )
        )
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=token,
            max_age=int(os.getenv("SESSION_TTL", str(8 * 3600))),
            httponly=False,
            samesite="lax",
            secure=is_secure,
            path="/",
        )

    def verify_csrf_token(request: Request):
        """Verify CSRF token for state-changing browser requests (Double Submit Cookie)."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return

        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
        csrf_header = request.headers.get(CSRF_HEADER_NAME)

        # DEFECT-48: always required (login CSRF: a fresh browser had neither cookie nor session, so the check was
        # skipped); the page sets the cookie on GET / before any POST.
        if True:
            if not csrf_cookie or not csrf_header or not consteq(csrf_cookie, csrf_header):
                logger.warning("CSRF check failed: cookie=%s, header=%s", bool(csrf_cookie), bool(csrf_header))
                raise HTTPException(
                    status_code=403,
                    detail="CSRF token missing or invalid. Please refresh the page.",
                )

    def get_current_web_user(request: Request):
        u = request.session.get("user")
        return u if u and u.get("exp", 0) > time.time() else None

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def serve_index(request: Request):
        response = FileResponse(STATIC_DIR / "index.html")
        csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or generate_csrf_token()
        set_csrf_cookie(response, csrf_token, request)
        return response

    @app.get("/me", include_in_schema=False)
    async def me(request: Request):
        u = get_current_web_user(request)
        csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or generate_csrf_token()
        response = JSONResponse({
            "tier": "internal" if u else "public",
            "name": u.get("name") if u else None,
            "email": u.get("email") if u else None,
            "login_required": True,
            "auth_mode": "otp",
            "backend": "gateway",
            "debug_enabled": os.getenv("DEBUG_UI", "0") == "1",
            "csrf_token": csrf_token,
        })
        set_csrf_cookie(response, csrf_token, request)
        return response

    @app.post("/auth/otp/request", dependencies=[Depends(verify_csrf_token)], include_in_schema=False)
    @app.post("/auth/otp/resend", dependencies=[Depends(verify_csrf_token)], include_in_schema=False)
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
            auth.otp_store.revoke(email, code.challenge)
            logger.error("otp mail failed: %s", e)
            # DEFECT-38: details (exception, errno) stay in the server log
            return JSONResponse({"error": "We could not send the sign-in code right now. Please try again in a few minutes."}, status_code=503)

        # DEFECT-35: the code is bound to this browser (signed session); guesses from elsewhere cannot burn it
        request.session["otp_challenge"] = {"email": email, "c": code.challenge}
        is_resend = request.url.path.endswith("/resend")
        code_desc = "A new 6-digit verification code" if is_resend else "A 6-digit verification code"
        return {
            "ok": True,
            "message": f"{code_desc} has been sent to your email address (valid for {int(C.OTP_TTL)} seconds).",
            "resend_in": int(C.OTP_RESEND_SECS),
        }

    @app.post("/auth/otp/verify", dependencies=[Depends(verify_csrf_token)], include_in_schema=False)
    async def otp_verify(body: OtpVerify, request: Request):
        try:
            email = auth.normalize_email(body.email)
        except auth.AuthError:
            return JSONResponse({"error": "Invalid or expired code."}, status_code=401)
        client_ip = auth.client_ip(request) or "127.0.0.1"
        try:
            auth.otp_store.check_verify_rate_limit(client_ip, email)
        except auth.RateLimited as e:
            return JSONResponse({"error": str(e)}, status_code=429)

        pending = request.session.get("otp_challenge") or {}
        challenge = pending.get("c", "") if pending.get("email") == email else ""
        if not auth.otp_store.verify(email, body.code.strip(), challenge):
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

    @app.post("/auth/logout", dependencies=[Depends(verify_csrf_token)], include_in_schema=False)
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/theses", include_in_schema=False)
    async def theses():
        return []

    @app.post("/chat", dependencies=[Depends(verify_csrf_token)], include_in_schema=False)
    async def chat_frontend(req: ChatRequest, request: Request):
        u = get_current_web_user(request)
        if not u:
            return JSONResponse(
                {"error": "Sign in with your IISc email to use AskIISc."},
                status_code=401,
            )
        admission = request.app.dependency_overrides.get(get_admission_controller, get_admission_controller)()
        try:   # DEFECT-59: blocked after repeated injection attempts
            await _check_offender(admission, request)
        except HTTPException as e:
            return JSONResponse({"error": e.detail}, status_code=e.status_code, headers=e.headers)
        # VULN-04: Validate prompt against adversarial injection & jailbreak patterns
        try:
            # patterns + English only; /query runs the classifier on a cache miss (DEFECT-51), not twice
            await run_in_threadpool(lambda: sanitize_and_fence_prompt(req.message, use_classifier=False, user_message=True))
        except PromptInjectionAlert as e:
            from pipeline.language_gate import LanguageNotSupported
            await _injection_refused(admission, request, e)   # a strike unless it is a language refusal
            msg = (str(e) if isinstance(e, LanguageNotSupported) else
                   "Adversarial prompt injection detected. Your query contains prohibited system override patterns.")
            return JSONResponse({"error": msg}, status_code=400)
        user_email = u.get("email", "")
        return StreamingResponse(
            stream_via_gateway(req.message, "internal", user_email=user_email, regenerate_token=req.regenerate_token),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
