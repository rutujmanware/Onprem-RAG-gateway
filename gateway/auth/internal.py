"""Signed internal user identity (DEFECT-30).

The web chat bridge (/chat -> stream_via_gateway) calls /query over loopback and names the signed-in user in
`X-Internal-User`. The gateway used to trust that header from any loopback caller, but this is a shared host:
every local user could claim any identity (read that user's owner-scoped cache hits, spend their regenerate
quota and concurrency slots). The header is now honoured only with `X-Internal-Auth: <unix-time>.<hmac>`,
an HMAC-SHA256 over the user and the time, keyed from INTERNAL_AUTH_KEY or, by default, SESSION_SECRET
(owner-readable .env). Signatures are valid for MAX_AGE_SECONDS.

Standard library only: askiisc_chat/askiisc/gateway_backend.py carries the same signing code for its
standalone mode (tests/test_internal_identity.py checks both agree).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Dict, Optional

USER_HEADER = "X-Internal-User"
AUTH_HEADER = "X-Internal-Auth"
MAX_AGE_SECONDS = 120
_LABEL = b"askiisc-internal-user-v1"


def _secret() -> str:
    secret = (os.getenv("INTERNAL_AUTH_KEY") or os.getenv("SESSION_SECRET") or "").strip()
    if secret:
        return secret
    try:  # scripts started without the environment: read the repo's .env (without modifying os.environ)
        from pathlib import Path
        from dotenv import dotenv_values
        env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
        return (env.get("INTERNAL_AUTH_KEY") or env.get("SESSION_SECRET") or "").strip()
    except Exception:
        return ""


def _key() -> Optional[bytes]:
    secret = _secret()
    if len(secret) < 32:  # no usable secret: internal identities are refused
        return None
    return hmac.new(secret.encode(), _LABEL, hashlib.sha256).digest()


def _sig(key: bytes, user: str, ts: int) -> str:
    return hmac.new(key, f"{ts}|{user}".encode(), hashlib.sha256).hexdigest()


def internal_user_headers(user: str, now: Optional[float] = None) -> Dict[str, str]:
    """Headers naming `user` to the gateway. Empty if no user or no secret is configured."""
    key = _key()
    if not user or key is None:
        return {}
    ts = int(now if now is not None else time.time())
    return {USER_HEADER: user, AUTH_HEADER: f"{ts}.{_sig(key, user, ts)}"}


def verify_internal_user(user: str, auth: str, now: Optional[float] = None) -> bool:
    key = _key()
    if not user or not auth or key is None or "." not in auth:
        return False
    ts_s, sig = auth.split(".", 1)
    try:
        ts = int(ts_s)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > MAX_AGE_SECONDS:
        return False
    return hmac.compare_digest(sig, _sig(key, user, ts))
