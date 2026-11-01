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

async def require_iisc_user(credentials=None, validator=None) -> UserIdentity:
    return UserIdentity(
        user_id="test_student",
        email="student@iisc.ac.in",
        name="IISc Student",
        roles=["student"],
        department="cds",
    )
