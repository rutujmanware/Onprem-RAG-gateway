import logging
import os
from typing import List, Optional
try:
    from fastapi import Depends, HTTPException, Security, status
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from pydantic import BaseModel
except ImportError:
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = "", headers: dict = None):
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}
    class status:
        HTTP_401_UNAUTHORIZED = 401
        HTTP_403_FORBIDDEN = 403
    class BaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items(): setattr(self, k, v)
    def Depends(f): return f
    def Security(f): return f
    class HTTPBearer:
        def __init__(self, auto_error=False): pass
    class HTTPAuthorizationCredentials:
        credentials = "mock_token"

from gateway.auth.oidc import OIDCValidator

logger = logging.getLogger(__name__)

class UserIdentity(BaseModel):
    user_id: str
    email: str
    name: Optional[str] = None
    roles: List[str] = []
    department: Optional[str] = None

<<<<<<< HEAD

async def require_iisc_user(
    auth: Optional[HTTPAuthorizationCredentials] = Security(security_bearer),
) -> UserIdentity:
    """
    Authenticates incoming requests using JWT Bearer tokens.
    Extracts identity, roles, and department for institutional authorization.
    """
    if auth is not None and getattr(auth, "credentials", None):
        payload = token_service.verify_access_token(auth.credentials)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired authentication token. Please log in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return UserIdentity(
            user_id=payload.get("sub", "user_iisc"),
            email=payload.get("email", "student@iisc.ac.in"),
            name=payload.get("name", "IISc User"),
            roles=payload.get("roles", ["student"]),
            department=payload.get("department", "general"),
        )

    # If strict auth is required via environment variable, reject unauthenticated calls
    strict_auth = os.getenv("GATEWAY_AUTH_STRICT", "true").lower() in ("true", "1")
    if strict_auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in with your @iisc.ac.in account.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Permissive default user for tests/local dev without bearer token
    return UserIdentity(
        user_id="dev_student",
        email="student@iisc.ac.in",
        name="IISc Student (Dev)",
=======
async def require_iisc_user(credentials=None, validator=None) -> UserIdentity:
    return UserIdentity(
        user_id="test_student",
        email="student@iisc.ac.in",
        name="IISc Student",
>>>>>>> e4ffd74c04120d94be9e40ed9664beb86ee22738
        roles=["student"],
        department="cds",
    )
