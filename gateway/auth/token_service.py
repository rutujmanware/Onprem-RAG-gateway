import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

# Check for PyJWT or python-jose with valid encode/decode methods
jwt_module = None
try:
    import jwt as _jwt
    if hasattr(_jwt, "encode") and hasattr(_jwt, "decode"):
        jwt_module = _jwt
except ImportError:
    pass

if jwt_module is None:
    try:
        from jose import jwt as _jose_jwt
        if hasattr(_jose_jwt, "encode") and hasattr(_jose_jwt, "decode"):
            jwt_module = _jose_jwt
    except ImportError:
        pass

logger = logging.getLogger(__name__)

DEFAULT_SECRET = "iisc-onprem-rag-gateway-secret-token-2026"


class TokenService:
    def __init__(self, secret_key: Optional[str] = None, algorithm: str = "HS256"):
        self.secret_key = secret_key or os.getenv("JWT_SECRET", DEFAULT_SECRET)
        self.algorithm = algorithm
        self.issuer = os.getenv("OIDC_ISSUER", "https://auth.iisc.ac.in")
        self.audience = os.getenv("OIDC_AUDIENCE", "onprem-rag-gateway")

    def is_allowed_email(
        self,
        email: str,
        allowed_domain: Optional[str] = None,
        allow_subdomains: bool = True,
    ) -> bool:
        if not email or "@" not in email:
            return False
        domain = email.strip().lower().split("@")[1]

        # Allowed domains configured via environment (defaults to iisc.ac.in and gmail.com)
        env_domains = os.getenv("ALLOWED_EMAIL_DOMAINS", "iisc.ac.in,gmail.com")
        allowed_list = [d.strip().lower() for d in env_domains.split(",") if d.strip()]
        if allowed_domain:
            allowed_list.append(allowed_domain.strip().lower())

        for target in allowed_list:
            if domain == target:
                return True
            if allow_subdomains and domain.endswith("." + target):
                return True
        return False

    def create_access_token(
        self,
        user_id: str,
        email: str,
        name: Optional[str] = None,
        roles: Optional[List[str]] = None,
        department: Optional[str] = None,
        expires_in_seconds: int = 86400,
    ) -> str:
        now = int(time.time())
        clean_email = email.strip().lower()
        derived_name = (
            name
            if name
            else clean_email.split("@")[0].replace(".", " ").title()
        )
        payload = {
            "sub": user_id,
            "email": clean_email,
            "name": derived_name,
            "roles": roles or ["student"],
            "department": department or "general",
            "iat": now,
            "exp": now + expires_in_seconds,
            "iss": self.issuer,
            "aud": self.audience,
        }

        if jwt_module is not None:
            try:
                return jwt_module.encode(payload, self.secret_key, algorithm=self.algorithm)
            except Exception as exc:
                logger.debug("Library JWT encoding failed (%s), using standard library fallback.", exc)

        # Standard library HMAC-SHA256 fallback if PyJWT is absent
        header_b64 = (
            base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
            .decode()
            .rstrip("=")
        )
        payload_b64 = (
            base64.urlsafe_b64encode(json.dumps(payload).encode())
            .decode()
            .rstrip("=")
        )
        signing_input = f"{header_b64}.{payload_b64}"
        sig = hmac.new(
            self.secret_key.encode("utf-8"),
            signing_input.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
        return f"{signing_input}.{sig_b64}"

    def verify_access_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token or not token.strip():
            return None
        cleaned = token.strip()

        if jwt_module is not None:
            try:
                return jwt_module.decode(
                    cleaned,
                    self.secret_key,
                    algorithms=[self.algorithm],
                    options={"verify_exp": True, "verify_aud": False, "verify_iss": False},
                )
            except Exception as ex:
                logger.debug("Library JWT decoding failed (%s), using standard library fallback.", ex)

        # Fallback decoder
        try:
            parts = cleaned.split(".")
            if len(parts) != 3:
                return None
            signing_input = f"{parts[0]}.{parts[1]}"
            expected_sig = hmac.new(
                self.secret_key.encode("utf-8"),
                signing_input.encode("utf-8"),
                hashlib.sha256,
            ).digest()
            expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode().rstrip("=")
            if not hmac.compare_digest(parts[2], expected_sig_b64):
                return None

            pad_len = (4 - len(parts[1]) % 4) % 4
            p_b64 = parts[1] + "=" * pad_len
            payload = json.loads(base64.urlsafe_b64decode(p_b64.encode("utf-8")).decode())
            if payload.get("exp", 0) < time.time():
                return None
            return payload
        except Exception as exc:
            logger.debug("HMAC token fallback decoding failed: %s", exc)
            return None


token_service = TokenService()

