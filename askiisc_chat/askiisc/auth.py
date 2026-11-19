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
    if not secrets.compare_digest(str(c.get("nonce", "")).encode(), str(nonce).encode()): raise AuthError("nonce mismatch")  # bytes: never raises (DEFECT-54)
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

class IssuedCode(str):
    """The 6-digit code (a str, for the e-mail) plus the challenge id that must accompany it on verification."""
    challenge: str = ""


class OtpStore:
    """Distributed OTP store backed by Redis (with fallback to in-memory for unit tests / single-process).
    Codes are stored strictly as HMAC-SHA256 hashes. Shared across all Gateway workers via Redis.

    DEFECT-35: one person must not be able to stop another from signing in.
      * Every code belongs to a challenge (random id) that is kept in the requesting browser's signed session;
        verification needs the challenge and the code, and wrong guesses count against that challenge only. An
        attacker's guesses burn only the challenges the attacker requested (whose codes went to the victim's
        mailbox), never the victim's; a new request does not cancel an older pending code.
      * Request limits (cooldown, hourly cap) are per (email, requesting IP); a per-email ceiling
        (OTP_MAX_PER_EMAIL_HOUR) only bounds mail volume.
      * Brute force: wrong guesses per email per hour are capped (OTP_MAX_FAILS_PER_EMAIL_HOUR), but the first
        attempt on a fresh challenge is always evaluated, so the cap cannot lock the real user out.
    """

    LUA_VERIFY_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return -1 end
local status, data = pcall(cjson.decode, raw)
if not status or type(data) ~= 'table' then redis.call('DEL', KEYS[1]) return -1 end
local now = tonumber(ARGV[3])
if now > tonumber(data['exp'] or 0) then redis.call('DEL', KEYS[1]) return -2 end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - 3600)
local tries = tonumber(data['tries'] or 0)
if tries > 0 and redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[4]) then return -3 end
tries = tries + 1
data['tries'] = tries
if ARGV[1] ~= '' and data['h'] == ARGV[1] then redis.call('DEL', KEYS[1]) return 1 end
redis.call('ZADD', KEYS[2], now, ARGV[5])
redis.call('EXPIRE', KEYS[2], 3600)
if tries >= tonumber(ARGV[2]) then
    redis.call('DEL', KEYS[1])
else
    local ttl = redis.call('TTL', KEYS[1])
    if ttl > 0 then redis.call('SET', KEYS[1], cjson.encode(data), 'EX', ttl) else redis.call('DEL', KEYS[1]) end
end
return 0
"""

    def __init__(self, clock=time.time, redis_client=None):
        self.clock = clock
        self.rec = {}                       # (email, challenge) -> record (in-memory mode)
        self.by_email = defaultdict(list)   # rate-limit logs (in-memory mode)
        self.by_ip = defaultdict(list)
        self.lock = threading.Lock()
        self._redis = redis_client
        self._redis_checked = False
        self._redis_retry_at = 0.0    # DEFECT-61: next connection attempt (monotonic seconds)
        self._redis_backoff = 0.0

    def _h(self, email: str, code: str) -> str:
        return hmac.new(_key(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()

    def _get_redis(self):
        # When synthetic/mock clock is injected without an explicit redis_client, use in-memory store
        if self.clock is not time.time and self._redis is None:
            return None
        # DEFECT-61: if Redis is unreachable when this worker first needs it, retry later with a growing wait
        # (5 s, doubling, at most 60 s) instead of staying on the per-worker memory store until a restart: codes
        # issued by one worker would not verify on the others, and the send / guess limits became per worker.
        # Once connected, redis-py reconnects by itself after an outage.
        if self._redis is None and time.monotonic() >= self._redis_retry_at:
            self._redis_checked = True
            if self._redis is None:
                try:
                    import redis
                    redis_url = getattr(C, "REDIS_URL", None) or os.getenv("REDIS_URL")
                    pw = getattr(C, "REDIS_PASSWORD", None) or os.getenv("REDIS_PASSWORD")
                    if redis_url and pw and "@" not in redis_url:
                        from urllib.parse import urlparse, urlunparse
                        parsed = urlparse(redis_url)
                        netloc = f":{pw}@{parsed.hostname}:{parsed.port or 6379}"
                        redis_url = urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
                    elif not redis_url:
                        pw_str = f":{pw}@" if pw else ""
                        host = os.getenv("REDIS_HOST", "localhost")
                        port = os.getenv("REDIS_PORT", "6379")
                        redis_url = f"redis://{pw_str}{host}:{port}/0"
                    client = redis.Redis.from_url(redis_url, decode_responses=True)
                    client.ping()
                    self._redis = client
                    if self._redis_backoff:
                        logger.info("OtpStore reconnected to Redis")
                    self._redis_backoff = 0.0
                except Exception as e:
                    self._redis = None
                    self._redis_backoff = min(60.0, (self._redis_backoff * 2) or 5.0)
                    self._redis_retry_at = time.monotonic() + self._redis_backoff
                    logger.warning("Redis connection failed for OtpStore, using in-memory for %.0f s: %s",
                                   self._redis_backoff, e)
        return self._redis

    def _recent(self, log, key, now, window=3600):
        log[key] = [t for t in log[key] if now - t < window]
        return log[key]

    @staticmethod
    def _ipk(ip: str) -> str:
        return hashlib.sha256((ip or "?").encode()).hexdigest()[:16]

    def _limits(self, email, ip, now, counts):
        """counts: callable(kind, key) -> (number in the last hour, seconds since the last one or None)."""
        n_pair, since_pair = counts("pair", f"{email}|{self._ipk(ip)}")
        if since_pair is not None and since_pair < C.OTP_RESEND_SECS:
            rem = max(1, int(round(C.OTP_RESEND_SECS - since_pair)))
            raise RateLimited(f"Please wait {rem}s before requesting another code.", resend_in=rem)
        if n_pair >= C.OTP_MAX_PER_HOUR:
            raise RateLimited("too many codes requested for this address")
        if counts("email", email)[0] >= C.OTP_MAX_PER_EMAIL_HOUR:
            raise RateLimited("too many codes requested for this address")
        if counts("ip", ip)[0] >= C.OTP_MAX_PER_IP_HOUR:
            raise RateLimited("too many requests from this network address")

    def issue(self, email: str, ip: str) -> IssuedCode:
        r = self._get_redis()
        now = self.clock()
        ttl = int(C.OTP_TTL)
        code = IssuedCode(f"{secrets.randbelow(10**6):06d}")
        code.challenge = secrets.token_urlsafe(18)
        record = {"h": self._h(email, code), "exp": now + ttl, "tries": 0, "sent": now}
        keys = {"pair": "auth:rate:pair:", "email": "auth:rate:email:", "ip": "auth:rate:ip:"}

        if r is not None:
            try:
                def counts(kind, key):
                    k = keys[kind] + key
                    r.zremrangebyscore(k, 0, now - 3600)
                    last = r.zrange(k, -1, -1, withscores=True)
                    return r.zcard(k), (now - last[0][1]) if last else None
                self._limits(email, ip, now, counts)
                r.set(f"auth:otp:{email}:{code.challenge}", json.dumps(record), ex=ttl)
                token = f"{now}:{secrets.token_hex(4)}"
                for kind, key in (("pair", f"{email}|{self._ipk(ip)}"), ("email", email), ("ip", ip)):
                    r.zadd(keys[kind] + key, {token: now})
                    r.expire(keys[kind] + key, 3600)
                return code
            except RateLimited:
                raise
            except Exception as e:
                logger.warning("Redis issue failed, falling back to memory: %s", e)

        with self.lock:
            logs = {"pair": self.by_email, "email": self.by_email, "ip": self.by_ip}
            def counts(kind, key):
                lst = self._recent(logs[kind], f"{kind}:{key}", now)
                return len(lst), (now - lst[-1]) if lst else None
            self._limits(email, ip, now, counts)
            self.rec[(email, code.challenge)] = record
            for kind, key in (("pair", f"{email}|{self._ipk(ip)}"), ("email", email), ("ip", ip)):
                logs[kind][f"{kind}:{key}"].append(now)
            return code

    def revoke(self, email: str, challenge: str = ""):
        r = self._get_redis()
        if r is not None:
            try:
                r.delete(f"auth:otp:{email}:{challenge}")
            except Exception as e:
                logger.warning("Redis revoke failed: %s", e)
        with self.lock:
            self.rec.pop((email, challenge), None)

    def check_verify_rate_limit(self, ip: str, email: str = ""):
        """Max 10 verification attempts per minute per IP. (No per-email limit here: it would let anyone who knows
        an address lock its owner out; guesses per email are bounded in verify().)"""
        r = self._get_redis()
        now = self.clock()
        if r is not None:
            try:
                r.zremrangebyscore(f"auth:rate:verify:ip:{ip}", 0, now - 60)
                if r.zcard(f"auth:rate:verify:ip:{ip}") >= 10:
                    raise RateLimited("Too many verification attempts from this IP address. Please wait 60 seconds.")
                r.zadd(f"auth:rate:verify:ip:{ip}", {f"{now}:{secrets.token_hex(4)}": now})
                r.expire(f"auth:rate:verify:ip:{ip}", 60)
                return
            except RateLimited:
                raise
            except Exception as e:
                logger.warning("Redis verify rate limit check failed, falling back to memory: %s", e)
        with self.lock:
            recent_ip = [t for t in self.by_ip.get(f"v:{ip}", []) if now - t < 60]
            if len(recent_ip) >= 10:
                raise RateLimited("Too many verification attempts from this IP address. Please wait 60 seconds.")
            recent_ip.append(now)
            self.by_ip[f"v:{ip}"] = recent_ip

    def verify(self, email: str, code: str, challenge: str = "") -> bool:
        """True only for the right code of a pending challenge requested from this browser."""
        if not challenge:
            return False
        r = self._get_redis()
        now = self.clock()
        candidate_hash = self._h(email, code) if (code and re.fullmatch(r"\d{6}", code)) else ""

        if r is not None:
            try:
                res = int(r.eval(self.LUA_VERIFY_SCRIPT, 2, f"auth:otp:{email}:{challenge}", f"auth:fail:{email}",
                                 candidate_hash, C.OTP_MAX_TRIES, now, C.OTP_MAX_FAILS_PER_EMAIL_HOUR,
                                 f"{now}:{secrets.token_hex(4)}"))
                return res == 1
            except Exception as e:
                logger.warning("Redis verify failed, falling back to memory: %s", e)

        with self.lock:
            key = (email, challenge)
            rec = self.rec.get(key)
            if not rec or now > rec.get("exp", 0):
                self.rec.pop(key, None)
                return False
            fails = self._recent(self.by_email, f"fail:{email}", now)
            if rec.get("tries", 0) > 0 and len(fails) >= C.OTP_MAX_FAILS_PER_EMAIL_HOUR:
                return False
            rec["tries"] = rec.get("tries", 0) + 1
            ok = bool(candidate_hash) and hmac.compare_digest(rec["h"], candidate_hash)
            if not ok:
                fails.append(now)
            if ok or rec["tries"] >= C.OTP_MAX_TRIES:
                self.rec.pop(key, None)
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
