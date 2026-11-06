"""Network gate + Microsoft Entra ID (OIDC, auth-code + PKCE) sign-in.
Tiers: "public" = on an allowed IISc network range, not signed in;  "internal" = verified IISc Entra identity."""
import base64, hashlib, hmac, ipaddress, json, logging, os, re, secrets, smtplib, threading, time
from collections import defaultdict
from email.message import EmailMessage
import httpx, jwt
from jwt import PyJWKClient
from . import config as C

logger = logging.getLogger("askiisc.auth")

class AuthError(Exception): pass
class RateLimited(AuthError):
    def __init__(self, message: str = "rate limited", resend_in: int | None = None):
        super().__init__(message)
        self.resend_in = resend_in
class MailError(Exception): pass

def _nets(items): return [ipaddress.ip_network(x, strict=False) for x in items]

def client_ip(request) -> str | None:
    peer = request.client.host if request.client else None
    if peer and peer in C.TRUSTED_PROXIES:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:  # RIGHT-most entry = the address our own proxy appended; anything left of it is client-controlled
            return xff.split(",")[-1].strip()
    return peer

def ip_allowed(ip: str | None) -> bool:
    try: a = ipaddress.ip_address(ip)
    except (ValueError, TypeError): return False
    return any(a in n for n in _nets(C.ALLOWED_CIDRS))

# ---- OIDC -------------------------------------------------------------------
def _issuer(): return f"https://login.microsoftonline.com/{C.ENTRA_TENANT_ID}/v2.0"
def _b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def new_login_state() -> dict:
    v = secrets.token_urlsafe(64)
    return {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24), "verifier": v,
            "challenge": _b64(hashlib.sha256(v.encode()).digest())}

def authorize_url(st: dict) -> str:
    from urllib.parse import urlencode
    q = urlencode({"client_id": C.ENTRA_CLIENT_ID, "response_type": "code", "response_mode": "query",
                   "redirect_uri": C.PUBLIC_BASE_URL + "/auth/callback", "scope": "openid profile email",
                   "state": st["state"], "nonce": st["nonce"],
                   "code_challenge": st["challenge"], "code_challenge_method": "S256"})
    return f"https://login.microsoftonline.com/{C.ENTRA_TENANT_ID}/oauth2/v2.0/authorize?{q}"

async def exchange_code(code: str, verifier: str) -> str:
    """Server-to-server call to Microsoft's token endpoint. This is the gateway's one allowed egress (with JWKS)."""
    async with httpx.AsyncClient(timeout=10) as cl:
        r = await cl.post(f"https://login.microsoftonline.com/{C.ENTRA_TENANT_ID}/oauth2/v2.0/token", data={
            "client_id": C.ENTRA_CLIENT_ID, "client_secret": C.ENTRA_CLIENT_SECRET, "grant_type": "authorization_code",
            "code": code, "redirect_uri": C.PUBLIC_BASE_URL + "/auth/callback", "code_verifier": verifier})
    if r.status_code != 200: raise AuthError(f"token endpoint HTTP {r.status_code}")
    return r.json()["id_token"]

_jwks = None
def _signing_key(token: str):
    global _jwks
    if _jwks is None:
        _jwks = PyJWKClient(f"https://login.microsoftonline.com/{C.ENTRA_TENANT_ID}/discovery/v2.0/keys", cache_keys=True)
    return _jwks.get_signing_key_from_jwt(token).key

def validate_id_token(token: str, nonce: str, key=None) -> dict:
    """Verify signature, iss, aud, exp, nonce, tenant, domain, guest status, group. `key` injectable for tests."""
    try:
        c = jwt.decode(token, key or _signing_key(token), algorithms=["RS256"], audience=C.ENTRA_CLIENT_ID,
                       issuer=_issuer(), leeway=60, options={"require": ["exp", "iat", "iss", "aud"]})
    except jwt.PyJWTError as e:
        raise AuthError(f"invalid id_token: {type(e).__name__}") from e
    if c.get("tid") != C.ENTRA_TENANT_ID: raise AuthError("wrong tenant")
    if not secrets.compare_digest(str(c.get("nonce", "")), nonce): raise AuthError("nonce mismatch")
    ident = (c.get("preferred_username") or c.get("email") or "").lower()
    if "#ext#" in ident: raise AuthError("guest (B2B) account not allowed")   # interns/temp staff added as guests
    if not domain_ok(ident): raise AuthError("email domain not allowed")
    if C.SENSITIVE_GROUP_ID and C.SENSITIVE_GROUP_ID not in (c.get("groups") or []): raise AuthError("not in required group")
    return {"sub": c.get("oid") or c["sub"], "name": c.get("name", ident), "email": ident}


# ---- Email one-time-code sign-in ----------------------------------------------
_EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._%-]{0,63}(\+[a-z0-9._%-]{1,64})?@([a-z0-9-]+\.)+[a-z]{2,}$")

def domain_ok(addr: str) -> bool:
    dom = addr.rsplit("@", 1)[-1]
    return any(dom == d or (C.ALLOW_SUBDOMAINS and dom.endswith("." + d)) for d in C.ALLOWED_EMAIL_DOMAINS)

def normalize_email(raw: str) -> str:
    """lower-case, strip, drop +tag (alice+x@ is the same mailbox as alice@), enforce domain. Regex also blocks CR/LF header injection."""
    e = (raw or "").strip().lower()
    if len(e) > 254 or not _EMAIL_RE.match(e): raise AuthError("invalid email address")
    local, dom = e.rsplit("@", 1)
    e = local.split("+", 1)[0] + "@" + dom
    if not domain_ok(e): raise AuthError("email domain not allowed")
    return e

_secret = None
def _key() -> bytes:
    global _secret
    if _secret is None: _secret = (C.SESSION_SECRET or secrets.token_urlsafe(32)).encode()
    return _secret

class OtpStore:
    """Distributed OTP store backed by Redis (with fallback to in-memory for unit tests / single-process).
    Codes are stored strictly as HMAC-SHA256 hashes. Shared across all Gateway workers via Redis."""
    def __init__(self, clock=time.time, redis_client=None):
        self.clock = clock
        self.rec = {}
        self.by_email = defaultdict(list)
        self.by_ip = defaultdict(list)
        self.lock = threading.Lock()
        self._redis = redis_client
        self._redis_checked = False

    def _h(self, email: str, code: str) -> str:
        return hmac.new(_key(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()

    def _get_redis(self):
        # When synthetic/mock clock is injected (e.g. in test suites), use in-memory store
        if self.clock is not time.time:
            return None
        if not self._redis_checked:
            self._redis_checked = True
            if self._redis is None:
                try:
                    import redis
                    redis_url = getattr(C, "REDIS_URL", None) or os.getenv("REDIS_URL")
                    if not redis_url:
                        pw = getattr(C, "REDIS_PASSWORD", None) or os.getenv("REDIS_PASSWORD")
                        pw_str = f":{pw}@" if pw else ""
                        host = os.getenv("REDIS_HOST", "localhost")
                        port = os.getenv("REDIS_PORT", "6379")
                        redis_url = f"redis://{pw_str}{host}:{port}/0"
                    self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
                    self._redis.ping()
                except Exception as e:
                    logger.warning("Redis connection failed for OtpStore, using in-memory: %s", e)
                    self._redis = None
        return self._redis

    def _recent(self, log, key, now):
        log[key] = [t for t in log[key] if now - t < 3600]
        return log[key]

    def issue(self, email: str, ip: str) -> str:
        r = self._get_redis()
        now = self.clock()
        ttl = int(C.OTP_TTL)

        if r is not None:
            try:
                # 1. Resend cooldown check
                raw = r.get(f"auth:otp:{email}")
                if raw:
                    try:
                        existing = json.loads(raw)
                        elapsed = now - existing.get("sent", 0)
                        if elapsed < C.OTP_RESEND_SECS:
                            rem = max(1, int(round(C.OTP_RESEND_SECS - elapsed)))
                            raise RateLimited(f"Please wait {rem}s before requesting another code.", resend_in=rem)
                    except (json.JSONDecodeError, TypeError):
                        pass

                # 2. Hourly rate limit per email
                r.zremrangebyscore(f"auth:rate:email:{email}", 0, now - 3600)
                if r.zcard(f"auth:rate:email:{email}") >= C.OTP_MAX_PER_HOUR:
                    raise RateLimited("too many codes requested for this address")

                # 3. Hourly rate limit per IP
                r.zremrangebyscore(f"auth:rate:ip:{ip}", 0, now - 3600)
                if r.zcard(f"auth:rate:ip:{ip}") >= C.OTP_MAX_PER_IP_HOUR:
                    raise RateLimited("too many requests from this network address")

                # Generate 6-digit OTP code
                code = f"{secrets.randbelow(10**6):06d}"
                record = {
                    "h": self._h(email, code),
                    "exp": now + ttl,
                    "tries": 0,
                    "sent": now,
                }
                r.set(f"auth:otp:{email}", json.dumps(record), ex=ttl)

                # Record rate-limiting events in Redis
                token = f"{now}:{secrets.token_hex(4)}"
                r.zadd(f"auth:rate:email:{email}", {token: now})
                r.expire(f"auth:rate:email:{email}", 3600)
                r.zadd(f"auth:rate:ip:{ip}", {token: now})
                r.expire(f"auth:rate:ip:{ip}", 3600)

                with self.lock:
                    self.rec[email] = record

                return code
            except RateLimited:
                raise
            except Exception as e:
                logger.warning("Redis issue failed, falling back to memory: %s", e)

        # In-memory fallback
        with self.lock:
            rec = self.rec.get(email)
            if rec:
                elapsed = now - rec.get("sent", 0)
                if elapsed < C.OTP_RESEND_SECS:
                    rem = max(1, int(round(C.OTP_RESEND_SECS - elapsed)))
                    raise RateLimited(f"Please wait {rem}s before requesting another code.", resend_in=rem)
            if len(self._recent(self.by_email, email, now)) >= C.OTP_MAX_PER_HOUR:
                raise RateLimited("too many codes requested for this address")
            if len(self._recent(self.by_ip, ip, now)) >= C.OTP_MAX_PER_IP_HOUR:
                raise RateLimited("too many requests from this network address")
            code = f"{secrets.randbelow(10**6):06d}"
            record = {"h": self._h(email, code), "exp": now + ttl, "tries": 0, "sent": now}
            self.rec[email] = record
            self.by_email[email].append(now)
            self.by_ip[ip].append(now)
            return code

    def revoke(self, email: str):
        r = self._get_redis()
        if r is not None:
            try:
                r.delete(f"auth:otp:{email}")
            except Exception as e:
                logger.warning("Redis revoke failed: %s", e)
        with self.lock:
            self.rec.pop(email, None)

    def verify(self, email: str, code: str) -> bool:
        r = self._get_redis()
        now = self.clock()

        if r is not None:
            try:
                raw = r.get(f"auth:otp:{email}")
                if not raw:
                    with self.lock:
                        self.rec.pop(email, None)
                    return False
                try:
                    data = json.loads(raw)
                except Exception:
                    r.delete(f"auth:otp:{email}")
                    with self.lock:
                        self.rec.pop(email, None)
                    return False

                if now > data.get("exp", 0):
                    r.delete(f"auth:otp:{email}")
                    with self.lock:
                        self.rec.pop(email, None)
                    return False

                data["tries"] = data.get("tries", 0) + 1
                ok = bool(re.fullmatch(r"\d{6}", code or "")) and hmac.compare_digest(data["h"], self._h(email, code))
                if ok or data["tries"] >= C.OTP_MAX_TRIES:
                    r.delete(f"auth:otp:{email}")
                    with self.lock:
                        self.rec.pop(email, None)
                else:
                    remaining = max(1, int(data["exp"] - now))
                    r.set(f"auth:otp:{email}", json.dumps(data), ex=remaining)
                    with self.lock:
                        self.rec[email] = data
                return ok
            except Exception as e:
                logger.warning("Redis verify failed, falling back to memory: %s", e)

        # In-memory fallback
        with self.lock:
            rec = self.rec.get(email)
            if not rec or now > rec["exp"]:
                self.rec.pop(email, None)
                return False
            rec["tries"] += 1
            ok = bool(re.fullmatch(r"\d{6}", code or "")) and hmac.compare_digest(rec["h"], self._h(email, code))
            if ok or rec["tries"] >= C.OTP_MAX_TRIES:
                self.rec.pop(email, None)
            return ok

otp_store = OtpStore()

def send_mail(to: str, subject: str, body: str):
    """Blocking SMTP send supporting STARTTLS, direct SSL/TLS, and authenticated/intranet relays."""
    if not C.SMTP_HOST:
        raise MailError("SMTP_HOST not configured. Please fill in your SMTP credentials in smtp.env")
    m = EmailMessage()
    m["From"] = C.SMTP_FROM
    m["To"] = to
    m["Subject"] = subject
    m.set_content(body)

    timeout = getattr(C, "SMTP_TIMEOUT", 15.0)
    use_ssl = getattr(C, "SMTP_SSL", False) or C.SMTP_PORT == 465

    try:
        import ssl
        context = ssl.create_default_context()
        if use_ssl:
            with smtplib.SMTP_SSL(C.SMTP_HOST, C.SMTP_PORT, timeout=timeout, context=context) as s:
                if C.SMTP_USER and C.SMTP_PASS:
                    s.login(C.SMTP_USER, C.SMTP_PASS)
                s.send_message(m)
        else:
            with smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORT, timeout=timeout) as s:
                if C.SMTP_STARTTLS:
                    s.starttls(context=context)
                if C.SMTP_USER and C.SMTP_PASS:
                    s.login(C.SMTP_USER, C.SMTP_PASS)
                s.send_message(m)
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"{type(e).__name__}: {e}") from e

def otp_mail_body(code: str) -> str:
    valid_str = f"{int(C.OTP_TTL // 60)} minutes" if (C.OTP_TTL >= 60 and C.OTP_TTL % 60 == 0) else f"{int(C.OTP_TTL)} seconds"
    return (f"Your AskIISc sign-in code is: {code}\n\nIt is valid for {valid_str} ({int(C.OTP_TTL)} seconds) and can be used once.\n"
            "If you did not request this, ignore this email; nobody can sign in without this code.")
