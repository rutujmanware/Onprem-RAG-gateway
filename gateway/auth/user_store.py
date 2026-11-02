import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    """Hashes a password using PBKDF2-HMAC-SHA256 with a unique random salt."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
    )
    return f"{salt}${dk.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    """Verifies a password against a stored PBKDF2 salt$hash string."""
    try:
        salt, h = hashed.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
        )
        return secrets.compare_digest(dk.hex(), h)
    except Exception:
        return False


class UserStore:
    """
    Manages persistent user accounts and OTPs backed by Redis with
    resilient in-memory fallback for offline testing.
    """

    def __init__(self, redis_client: Any = None):
        self.redis = redis_client
        self._local_users: Dict[str, Dict[str, Any]] = {}
        self._local_otps: Dict[str, Dict[str, Any]] = {}
        self._seed_default_demo_accounts()

    def set_redis(self, redis_client: Any) -> None:
        self.redis = redis_client

    def _seed_default_demo_accounts(self) -> None:
        """Pre-provisions verified demo accounts with password 'campus-pass'."""
        demo_accounts = [
            {
                "email": "student@iisc.ac.in",
                "name": "IISc Student",
                "department": "cds",
                "roles": ["student"],
                "password_hash": hash_password("campus-pass"),
                "user_id": "std_student",
                "created_at": int(time.time()),
            },
            {
                "email": "faculty.venkatesh@ece.iisc.ac.in",
                "name": "Prof. R. Venkatesh",
                "department": "ece",
                "roles": ["faculty", "examiner"],
                "password_hash": hash_password("campus-pass"),
                "user_id": "fac_ece_01",
                "created_at": int(time.time()),
            },
            {
                "email": "admin@serc.iisc.ac.in",
                "name": "Cluster Administrator",
                "department": "serc",
                "roles": ["cluster_admin", "faculty_lead"],
                "password_hash": hash_password("campus-pass"),
                "user_id": "admin_serc_01",
                "created_at": int(time.time()),
            },
        ]
        for acc in demo_accounts:
            self._local_users[acc["email"]] = acc

    async def get_user(self, email: str) -> Optional[Dict[str, Any]]:
        clean_email = email.strip().lower()
        if self.redis is not None and hasattr(self.redis, "get"):
            try:
                raw = await self.redis.get(f"user:account:{clean_email}")
                if raw:
                    data = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                    return json.loads(data)
            except Exception as e:
                logger.debug("Redis get_user exception: %s", e)

        return self._local_users.get(clean_email)

    async def save_user(self, user_data: Dict[str, Any]) -> None:
        clean_email = user_data["email"].strip().lower()
        self._local_users[clean_email] = user_data

        if self.redis is not None and hasattr(self.redis, "set"):
            try:
                serialized = json.dumps(user_data)
                await self.redis.set(f"user:account:{clean_email}", serialized)
            except Exception as e:
                logger.debug("Redis save_user exception: %s", e)

    async def generate_otp(
        self, email: str, purpose: str = "register", ttl_seconds: int = 300
    ) -> str:
        clean_email = email.strip().lower()
        otp = f"{secrets.randbelow(900000) + 100000}"
        expiry = time.time() + ttl_seconds
        key = f"otp:{purpose}:{clean_email}"

        self._local_otps[key] = {"otp": otp, "expires_at": expiry}

        if self.redis is not None and hasattr(self.redis, "setex"):
            try:
                await self.redis.setex(key, ttl_seconds, otp)
            except Exception as e:
                logger.debug("Redis setex otp exception: %s", e)

        return otp

    async def verify_and_consume_otp(
        self, email: str, entered_otp: str, purpose: str = "register"
    ) -> bool:
        clean_email = email.strip().lower()
        key = f"otp:{purpose}:{clean_email}"
        entered = str(entered_otp).strip()

        # Check Redis first if available
        if self.redis is not None and hasattr(self.redis, "get"):
            try:
                stored = await self.redis.get(key)
                if stored:
                    stored_str = (
                        stored.decode("utf-8") if isinstance(stored, bytes) else str(stored)
                    )
                    if secrets.compare_digest(stored_str.strip(), entered):
                        if hasattr(self.redis, "delete"):
                            await self.redis.delete(key)
                        self._local_otps.pop(key, None)
                        return True
            except Exception as e:
                logger.debug("Redis verify otp exception: %s", e)

        # Fallback to local store
        record = self._local_otps.get(key)
        if record:
            if time.time() <= record["expires_at"]:
                if secrets.compare_digest(record["otp"], entered):
                    self._local_otps.pop(key, None)
                    return True
            else:
                self._local_otps.pop(key, None)

        return False


user_store = UserStore()

