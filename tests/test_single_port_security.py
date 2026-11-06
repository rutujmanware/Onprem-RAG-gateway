import pytest
import os
from fastapi.testclient import TestClient

# Ensure we test with default docs setting (docs disabled)
os.environ.pop("ENABLE_API_DOCS", None)
os.environ["ADMIN_API_KEY"] = "supersecretadminkey"

from gateway.main import app

client = TestClient(app)

def test_api_docs_hidden():
    """Verify OpenAPI, Swagger UI and ReDoc are completely disabled and return 404."""
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404

def test_frontend_routes_accessible():
    """Verify frontend UI and static assets are fully functional."""
    r = client.get("/")
    assert r.status_code == 200
    assert "AskIISc" in r.text

    r_index = client.get("/index.html")
    assert r_index.status_code == 200

    r_me = client.get("/me")
    assert r_me.status_code == 200
    assert r_me.json().get("login_required") is True

    r_health = client.get("/health")
    assert r_health.status_code == 200
    assert r_health.json() == {"status": "ok"}

def test_internal_endpoints_hidden_from_external():
    """Verify that external simulated clients (via X-Forwarded-For) cannot access internal routes."""
    headers = {"X-Forwarded-For": "203.0.113.195"}  # Simulated external public IP

    # All admin/internal endpoints should return 404 (hidden from public)
    assert client.get("/cache/stats", headers=headers).status_code == 404
    assert client.post("/cache/clear", headers=headers).status_code == 404
    assert client.post("/cache/purge-invalid", headers=headers).status_code == 404
    assert client.post("/query", json={"query": "test"}, headers=headers).status_code == 404

def test_internal_endpoints_accessible_with_admin_key():
    """Verify that requests with the valid X-Admin-Key can access internal endpoints."""
    headers = {
        "X-Forwarded-For": "203.0.113.195",
        "X-Admin-Key": "supersecretadminkey"
    }
    # With admin key, access is granted (not 404)
    r = client.get("/cache/stats", headers=headers)
    assert r.status_code == 200

if __name__ == "__main__":
    test_api_docs_hidden()
    print("✅ test_api_docs_hidden passed (/docs, /redoc, /openapi.json return 404)")
    test_frontend_routes_accessible()
    print("✅ test_frontend_routes_accessible passed (/, /index.html, /me, /health)")
    test_internal_endpoints_hidden_from_external()
    print("✅ test_internal_endpoints_hidden_from_external passed (/cache/* and /query return 404 to public)")
    test_internal_endpoints_accessible_with_admin_key()
    print("✅ test_internal_endpoints_accessible_with_admin_key passed")
    print("🎉 ALL SECURITY CHECKS PASSED!")
