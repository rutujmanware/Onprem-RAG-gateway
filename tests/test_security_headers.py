"""DEFECT-55: browser hardening headers on every response; the page has no inline script.

Before: production sent only content-type, etag and set-cookie. No CSP, so an injected script or an outside image
in an answer (which leaks the question to another server without a click) was not blocked; the sign-in page could
be framed by any site; /me (email and CSRF token) could be cached.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import gateway.main as gm

STATIC = Path(__file__).resolve().parent.parent / "askiisc_chat" / "static"


@pytest.fixture
def client():
    return TestClient(gm.app, raise_server_exceptions=False, follow_redirects=False)


@pytest.fixture
def remote():
    return TestClient(gm.app, client=("10.1.2.3", 5000), raise_server_exceptions=False, follow_redirects=False)


def _csp(r):
    return {d.split()[0]: d.split()[1:] for d in r.headers["content-security-policy"].split(";") if d.strip()}


def test_every_response_carries_the_headers(client, remote):
    responses = [client.get("/"), client.get("/me"), client.get("/health"), client.get("/static/app.js"),
                 client.get("/static/vendor/purify.min.js"), client.get("/no-such-page"),
                 remote.post("/query", json={"query": "hello"}),       # hidden route: same as a missing page
                 client.post("/auth/otp/request", json={})]            # CSRF refusal (403)
    for r in responses:
        h = r.headers
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff", r.url
        assert h["referrer-policy"] == "no-referrer" and "content-security-policy" in h, r.url


def test_csp_blocks_inline_script_outside_images_and_framing(client):
    csp = _csp(client.get("/"))
    assert csp["script-src"] == ["'self'"]                        # no 'unsafe-inline' / 'unsafe-eval'
    assert csp["img-src"] == ["'self'", "data:"]                  # no outside images (exfiltration)
    assert csp["frame-ancestors"] == ["'none'"] and csp["object-src"] == ["'none'"]
    assert csp["connect-src"] == ["'self'"] and csp["base-uri"] == ["'none'"]


def test_page_and_personal_data_are_never_cached(client):
    assert client.get("/").headers["cache-control"] == "no-store"
    assert client.get("/me").headers["cache-control"] == "no-store"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"   # revalidated: deploys apply at once
    assert "no-store" not in client.get("/static/vendor/purify.min.js").headers.get("cache-control", "")


def test_page_has_no_inline_script_or_handlers():
    html = (STATIC / "index.html").read_text()
    scripts = re.findall(r"<script\b([^>]*)>", html)
    assert scripts and all(re.search(r'\bsrc="/static/', a) for a in scripts), scripts
    assert not re.search(r"<[a-z][^>]*\son[a-z]+\s*=", html, re.I)      # no onclick= etc.
    assert '<script src="/static/app.js"></script>' in html
    js = (STATIC / "app.js").read_text()
    assert "eval(" not in js and "new Function" not in js


def test_hidden_routes_still_match_a_missing_page(remote):
    a, b = remote.get("/cache/stats"), remote.get("/no-such-page")
    assert (a.status_code, a.content) == (b.status_code, b.content)
    assert {k: v for k, v in a.headers.items() if k != "content-length"} == \
           {k: v for k, v in b.headers.items() if k != "content-length"}
