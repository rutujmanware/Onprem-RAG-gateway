"""DEFECT-54: guarded routes are indistinguishable from missing pages for callers their guard rejects.

Before: /query/ redirected (307) to /query, a body that is not UTF-8 got "400 error parsing the body", and a
non-ASCII admin key crashed the key comparison (HTTP 500), each revealing a hidden route to any campus host.
"""

import itertools

import pytest
from fastapi.testclient import TestClient

import gateway.main as gm
from gateway.auth.internal import consteq, verify_internal_user

KEY = "k" * 64
GUARDED = ["/query", "/v1/chat/completions", "/cache/stats", "/cache/purge", "/cache/clear",
           "/cache/purge-invalid", "/queue/stats"]
METHODS = ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]
HEADERS = [{}, {"X-Admin-Key": b"wrong"}, {"X-Admin-Key": "été".encode()}, {"Authorization": b"Bearer \xff"},
           {"X-Internal-User": b"a@iisc.ac.in", "X-Internal-Auth": "1.é".encode()},
           {"Content-Type": b"text/plain"}]
BODIES = [None, b"{", b"\xe9", b'{"query": "hello there"}', b"[]"]


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setattr(gm, "ADMIN_KEY", KEY)
    return TestClient(gm.app, client=("10.1.2.3", 5000), raise_server_exceptions=False, follow_redirects=False)


def _same(r, ref):
    body = (lambda x: b"" if x.request.method == "HEAD" else x.content)
    return (r.status_code, body(r), r.headers.get("location"), r.headers.get("allow")) == \
           (ref.status_code, body(ref), ref.headers.get("location"), ref.headers.get("allow"))


def test_every_guarded_route_answers_like_a_missing_page(remote):
    for path in GUARDED:
        for variant, m, h, b in itertools.product((path, path + "/", path + "/x"), METHODS, HEADERS, BODIES):
            ref = remote.request(m, "/no-such-page" + ("/" if variant.endswith("/") else ""), headers=h, content=b)
            r = remote.request(m, variant, headers=h, content=b)
            assert _same(r, ref), (variant, m, h, b, r.status_code, r.text[:120])


def test_key_holders_and_the_server_still_reach_the_routes(remote):
    # wrong method / trailing slash now visible: proves the guard let the caller through to routing
    r = remote.request("POST", "/cache/stats", headers={"X-Admin-Key": KEY})
    assert r.status_code == 405
    assert remote.get("/cache/stats/", headers={"X-Admin-Key": KEY}).status_code == 307
    local = TestClient(gm.app, raise_server_exceptions=False, follow_redirects=False)   # loopback ("testclient")
    assert local.post("/query/", json={"query": "hi"}).status_code == 307
    assert local.post("/query", content=b"{").status_code == 422   # a real validation answer for allowed callers


def test_public_routes_unchanged(remote):
    assert remote.get("/health").status_code == 200
    assert remote.get("/").status_code == 200


def test_consteq_never_raises():
    assert consteq("abc", "abc") and not consteq("abc", "abd")
    assert not consteq("été", "abc") and consteq("é", "é")
    assert not consteq(None, "x") and not consteq("x", None)
    assert consteq(b"\xff", b"\xff")
    assert verify_internal_user("a@iisc.ac.in", "1.é") is False
    assert verify_internal_user("é@iisc.ac.in", f"{int(__import__('time').time())}.abc") is False
