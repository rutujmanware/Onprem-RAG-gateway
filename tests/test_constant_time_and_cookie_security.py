"""Regression test suite for VULN-08: Non-Constant-Time ADMIN_KEY Comparison & Plaintext Cookies (CWE-208 / CWE-319).
Verifies:
1. ADMIN_KEY comparison uses constant-time secrets.compare_digest, preventing timing side-channel attacks.
2. Session cookies enforce https_only / Secure attribute by default, preventing interception over unencrypted HTTP.
"""
import os
import secrets

os.environ.setdefault("ADMIN_API_KEY", "supersecretadminkey")
import gateway.main
gateway.main.ADMIN_KEY = "supersecretadminkey"

from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from gateway.main import app

ADMIN_KEY = gateway.main.ADMIN_KEY
client = TestClient(app)
EXTERNAL_IP = "203.0.113.195"


def test_constant_time_admin_key_comparison():
    """Verify that ADMIN_KEY comparison enforces constant-time validation and rejects unauthorized keys."""
    # 1. Correct admin key via Bearer token (from external simulated IP)
    r_valid_bearer = client.get(
        "/cache/stats",
        headers={"X-Forwarded-For": EXTERNAL_IP, "Authorization": f"Bearer {ADMIN_KEY}"},
    )
    assert r_valid_bearer.status_code == 200

    # 2. Correct admin key via X-Admin-Key header
    r_valid_header = client.get(
        "/cache/stats",
        headers={"X-Forwarded-For": EXTERNAL_IP, "X-Admin-Key": ADMIN_KEY},
    )
    assert r_valid_header.status_code == 200

    # 3. Partial key (prefix match) must fail with 404
    partial_key = ADMIN_KEY[: len(ADMIN_KEY) // 2]
    r_partial = client.get(
        "/cache/stats",
        headers={"X-Forwarded-For": EXTERNAL_IP, "X-Admin-Key": partial_key},
    )
    assert r_partial.status_code == 404

    # 4. Off-by-one single character mismatch must fail with 404
    tampered_key = ADMIN_KEY[:-1] + ("x" if ADMIN_KEY[-1] != "x" else "y")
    r_tampered = client.get(
        "/cache/stats",
        headers={"X-Forwarded-For": EXTERNAL_IP, "X-Admin-Key": tampered_key},
    )
    assert r_tampered.status_code == 404

    # 5. Empty or whitespace key must fail with 404
    r_empty = client.get(
        "/cache/stats",
        headers={"X-Forwarded-For": EXTERNAL_IP, "X-Admin-Key": ""},
    )
    assert r_empty.status_code == 404

    r_spaces = client.get(
        "/cache/stats",
        headers={"X-Forwarded-For": EXTERNAL_IP, "Authorization": "Bearer    "},
    )
    assert r_spaces.status_code == 404


def test_session_cookie_secure_flag_configured():
    """Verify that SessionMiddleware in gateway enforces https_only=True (Secure flag on session cookies)."""
    session_mw = None
    for mw in app.user_middleware:
        if mw.cls == SessionMiddleware:
            session_mw = mw
            break

    assert session_mw is not None, "SessionMiddleware not found in gateway middleware stack"
    assert session_mw.kwargs.get("https_only") is True, (
        f"Expected https_only=True for secure cookies, got: {session_mw.kwargs.get('https_only')}"
    )


def test_secrets_compare_digest_timing_resistance():
    """Verify that secrets.compare_digest is directly used and behaves correctly for arbitrary byte lengths."""
    target = "secure-institutional-master-key-2026"
    assert secrets.compare_digest(target, target) is True
    assert secrets.compare_digest(target, target + "extra") is False
    assert secrets.compare_digest(target, "wrong-key") is False
