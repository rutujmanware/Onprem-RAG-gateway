import json, os, socket, threading, time, struct
import numpy as np, pytest, uvicorn, httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

os.environ.update(EMBED_BACKEND="hash", EMBED_DIM="64", TRACE_FILE="/tmp/askiisc_trace_test.jsonl",
                  ROUTER_PATH="/tmp/askiisc_router_test.joblib", ALPHA="0.5",
                  ALLOWED_CIDRS="10.0.0.0/8", COOKIE_SECURE="0", SESSION_SECRET="test-secret", ENTRA_TENANT_ID="tenant-1",
                  ENTRA_CLIENT_ID="client-1", PUBLIC_BASE_URL="http://testserver", ALLOWED_EMAIL_DOMAINS="iisc.ac.in", REQUIRE_LOGIN="0", AUTH_MODE="entra")

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def make_stub(name, logprob):
    a = FastAPI(); a.state.last = None
    @a.post("/v1/chat/completions")
    async def c(body: dict):
        a.state.last = body
        async def g():
            for w in [f"{name}-answer", " from", " [1]"]:
                ch = {"choices": [{"delta": {"content": w}, "logprobs": {"content": [{"logprob": logprob}]} if body.get("logprobs") else None}]}
                yield f"data: {json.dumps(ch)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(g(), media_type="text/event-stream")
    @a.get("/v1/models")
    async def m(): return {"data": []}
    return a

def serve(app, port):
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        try: httpx.get(f"http://127.0.0.1:{port}/openapi.json"); return srv
        except Exception: time.sleep(0.05)
    raise RuntimeError

class FakeRedis:
    def __init__(self): self.calls = []; self.restricted_dist = None   # set to a float to simulate a very close restricted chunk
    async def execute_command(self, *a):
        self.calls.append(a)
        if a[0] == "FT.SEARCH" and "{restricted}" in a[2]:
            if self.restricted_dist is None: return [0]
            return [1, b"secret:1", [b"text", b"SECRET embargoed result", b"thesis_id", b"T9", b"dist", str(self.restricted_dist).encode()]]
        if a[0] == "FT.SEARCH":
            return [2, b"chunk:1", [b"text", b"Transformers use attention.", b"thesis_id", b"T1", b"title", b"A Thesis",
                                    b"chapter", b"2", b"version", b"3", b"dist", b"0.12"],
                       b"chunk:2", [b"text", b"Second chunk.", b"thesis_id", b"T1", b"dist", b"0.3"]]
        if a[0] == "FT.TAGVALS": return [b"T1", b"T2"]
    async def ping(self): return True

@pytest.fixture(scope="module")
def env():
    from askiisc import config as C, router as R, embedder as E
    # train a tiny router on the hash embedder through the real training script path
    import csv, subprocess, sys
    easy = ["who is the author", "what year was it submitted", "which department", "what is the title", "who is the advisor", "when was it defended"] * 6
    hard = ["compare the assumptions of chapter 2 and 4 and explain why results differ", "why does the proposed method fail and how could it be derived differently",
            "explain the trade-off between accuracy and cost and evaluate limitations", "contrast the approaches and justify the choice of the model"] * 9
    with open("/tmp/lab.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["query", "label", "thesis_id"])
        for i, q in enumerate(easy): w.writerow([q, 0, f"E{i%6}"])
        for i, q in enumerate(hard): w.writerow([q, 1, f"H{i%6}"])
    r = subprocess.run([sys.executable, "train_router.py", "/tmp/lab.csv", "--out", C.ROUTER_PATH], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": "."}); assert r.returncode == 0, r.stderr[-800:]
    ps, pl = free_port(), free_port()
    slm, llm = make_stub("SLM", -0.05), make_stub("LLM", -0.05)
    s1, s2 = serve(slm, ps), serve(llm, pl)
    C.SLM_URL, C.LLM_URL = f"http://127.0.0.1:{ps}/v1", f"http://127.0.0.1:{pl}/v1"
    from askiisc.app import app
    from askiisc.retrieval import Retriever
    from askiisc.router import Router
    fr = FakeRedis(); app.state.retriever = Retriever(fr); app.state.router = Router()
    yield dict(app=app, fr=fr, slm=slm, llm=llm, C=C, s=(s1, s2))
    s1.should_exit = s2.should_exit = True

def events(resp_text):
    out = []
    for blk in resp_text.strip().split("\n\n"):
        ev = blk.split("\n")[0][7:]; d = json.loads(blk.split("\n")[1][6:]); out.append((ev, d))
    return out

def client(env, ip="10.1.2.3"):
    from fastapi.testclient import TestClient
    return TestClient(env["app"], client=(ip, 5555))

def chat(env, c=None, **kw):
    if c is None:
        with client(env) as c: return events(c.post("/chat", json=kw).text)
    return events(c.post("/chat", json=kw).text)

def search_calls(env, kind):
    return [a for a in env["fr"].calls if a[0] == "FT.SEARCH" and ("{restricted}" in a[2]) == (kind == "restricted")]

def test_easy_goes_slm_hard_goes_llm(env):
    e = chat(env, message="who is the author")
    meta = dict(e)["meta"]; assert meta["route"] == "slm" and meta["reason"] == "mlp"
    assert "".join(d["t"] for k, d in e if k == "token") == "SLM-answer from [1]"
    e = chat(env, message="compare the assumptions of chapter 2 and 4 and explain why results differ")
    assert dict(e)["meta"]["route"] == "llm"
    assert dict(e)["done"]["model"] == "llm"

def test_citations_and_prompt_layout(env):
    chat(env, message="who is the author", thesis_id="T1", force="slm")
    msgs = env["slm"].state.last["messages"]
    assert msgs[0]["role"] == "system" and "thesis 'T1'" in msgs[0]["content"]       # stable prefix first
    assert "Transformers use attention." in msgs[-1]["content"] and msgs[-1]["content"].rstrip().endswith("who is the author")
    q = search_calls(env, "public")[-1]
    assert "@thesis_id:{T1}" in q[2] and "KNN 6 @embedding $B" in q[2] and isinstance(q[6], bytes) and len(q[6]) == 64 * 4

def test_force_override(env):
    assert dict(chat(env, message="who is the author", force="llm"))["meta"]["route"] == "llm"

def test_cascade_escalates_on_low_confidence(env):
    env["slm"].state.last = None
    import askiisc.app  # noqa
    # make the SLM stub low-confidence by patching logprob via a new stub is heavy; patch the threshold instead
    env["C"].CASCADE_MIN_CONF = 0.99   # stub confidence ~0.951
    try: e = chat(env, message="who is the author")
    finally: env["C"].CASCADE_MIN_CONF = 0.60
    kinds = [k for k, _ in e]; assert "escalate" in kinds
    d = dict(e)["done"]; assert d["model"] == "llm" and d["escalated"] is True

def test_fallback_when_llm_down(env):
    old = env["C"].LLM_URL; env["C"].LLM_URL = "http://127.0.0.1:1/v1"
    try: e = chat(env, message="compare the assumptions of chapter 2 and 4 and explain why results differ")
    finally: env["C"].LLM_URL = old
    assert "fallback" in [k for k, _ in e] and dict(e)["done"]["model"] == "slm"

def test_both_down_reports_error(env):
    o = (env["C"].LLM_URL, env["C"].SLM_URL); env["C"].LLM_URL = env["C"].SLM_URL = "http://127.0.0.1:1/v1"
    try: e = chat(env, message="who is the author")
    finally: env["C"].LLM_URL, env["C"].SLM_URL = o
    assert e[-1][0] == "error"

def test_redis_failure_degrades_with_warning(env):
    class Bad:
        async def execute_command(self, *a): raise ConnectionError("down")
    from askiisc.retrieval import Retriever
    good = env["app"].state.retriever; env["app"].state.retriever = Retriever(Bad())
    try: e = chat(env, message="who is the author")
    finally: env["app"].state.retriever = good
    assert "warning" in [k for k, _ in e] and dict(e)["meta"]["citations"] == []

@pytest.fixture
def otp(env):
    """OTP mode, login required, captured mail, fresh store + controllable clock."""
    from askiisc import auth
    C = env["C"]; sent = []; clock = [1000.0]
    old = (C.AUTH_MODE, C.REQUIRE_LOGIN, auth.otp_store, auth.send_mail)
    C.AUTH_MODE, C.REQUIRE_LOGIN = "otp", True
    auth.otp_store = auth.OtpStore(clock=lambda: clock[0]); auth.send_mail = lambda to, subj, body: sent.append((to, subj, body))
    class H: pass
    h = H(); h.sent, h.clock, h.C, h.auth = sent, clock, C, auth
    h.code = lambda: __import__("re").search(r"code is: (\d{6})", sent[-1][2]).group(1)
    yield h
    C.AUTH_MODE, C.REQUIRE_LOGIN, auth.otp_store, auth.send_mail = old

def req(c, email="alice@iisc.ac.in"): return c.post("/auth/otp/request", json={"email": email})
def ver(c, code, email="alice@iisc.ac.in"): return c.post("/auth/otp/verify", json={"email": email, "code": code})

def test_otp_happy_path_and_chat_access(env, otp):
    with client(env) as c:
        assert c.post("/chat", json={"message": "hi"}).status_code == 401 and c.get("/theses").status_code == 401   # login mandatory
        env["slm"].state.last = None
        assert req(c).status_code == 200 and otp.sent[0][0] == "alice@iisc.ac.in"
        code = otp.code(); assert len(code) == 6
        assert ver(c, "000000" if code != "000000" else "111111").status_code == 401
        assert ver(c, code).status_code == 200
        assert c.get("/me").json()["email"] == "alice@iisc.ac.in"
        e = events(c.post("/chat", json={"message": "who is the author", "force": "slm"}).text)
        assert dict(e)["done"]["model"] == "slm" and c.get("/theses").status_code == 200

def test_otp_single_use_expiry_and_lockout(env, otp):
    with client(env) as c:
        req(c); code = otp.code(); assert ver(c, code).status_code == 200
        assert ver(c, code).status_code == 401                                     # single use
    with client(env) as c:
        otp.clock[0] += 120; req(c); code = otp.code()
        otp.clock[0] += otp.C.OTP_TTL + 1; assert ver(c, code).status_code == 401  # expired
    with client(env) as c:
        otp.clock[0] += 120; req(c); code = otp.code(); bad = "000000" if code != "000000" else "111111"
        for _ in range(otp.C.OTP_MAX_TRIES): assert ver(c, bad).status_code == 401
        assert ver(c, code).status_code == 401                                     # burned even though now correct

def test_otp_only_allowed_domain_and_no_mail_sent_otherwise(env, otp):
    with client(env) as c:
        for bad in ("mallory@gmail.com", "a@iisc.ac.in.evil.com", "a@evil-iisc.ac.in", "a@csa.iisc.ac.in", "x\r\nBcc: v@x.com@iisc.ac.in", "", "not-an-email"):
            assert req(c, bad).status_code == 400, bad
        assert otp.sent == []
        otp.C.ALLOW_SUBDOMAINS = True
        try: assert req(c, "a@csa.iisc.ac.in").status_code == 200
        finally: otp.C.ALLOW_SUBDOMAINS = False

def test_otp_rate_limits_and_plus_alias(env, otp):
    with client(env) as c:
        assert req(c).status_code == 200
        assert req(c).status_code == 429                                           # resend cooldown
        assert req(c, "alice+spam@IISC.ac.in").status_code == 429                  # +tag/case do not dodge the limit
        for _ in range(otp.C.OTP_MAX_PER_HOUR - 1):
            otp.clock[0] += 61; assert req(c).status_code == 200
        otp.clock[0] += 61; assert req(c).status_code == 429                       # hourly cap per mailbox
    with client(env) as c:
        otp.clock[0] += 4000
        for i in range(otp.C.OTP_MAX_PER_IP_HOUR): assert req(c, f"user{i}@iisc.ac.in").status_code == 200
        assert req(c, "another@iisc.ac.in").status_code == 429                     # per-IP cap

def test_otp_code_for_one_address_does_not_work_for_another(env, otp):
    with client(env) as c:
        req(c, "alice@iisc.ac.in"); code = otp.code()
        assert ver(c, code, "bob@iisc.ac.in").status_code == 401

def test_otp_mail_failure_returns_503_and_leaves_no_code(env, otp):
    def boom(*a): raise otp.auth.MailError("down")
    otp.auth.send_mail = boom
    with client(env) as c:
        assert req(c).status_code == 503
        assert "alice@iisc.ac.in" not in otp.auth.otp_store.rec
        otp.clock[0] += 0; otp.auth.send_mail = lambda *a: otp.sent.append(a); assert req(c).status_code == 200   # retry not blocked by cooldown

def test_code_not_in_http_responses(env, otp):
    with client(env) as c:
        r = req(c); assert otp.code() not in r.text

def test_otp_wrong_mode_disabled(env, otp):
    otp.C.AUTH_MODE = "entra"
    with client(env) as c: assert req(c).status_code == 404

def test_slm_gets_fewer_chunks_than_7b_and_sources_update_on_escalation(env):
    C = env["C"]; C.SLM_TOP_K = 1
    try:
        e = chat(env, message="who is the author", force="slm")
        assert [c["n"] for c in dict(e)["meta"]["citations"]] == [1]
        pm = env["slm"].state.last["messages"][-1]["content"]; assert "Transformers use attention." in pm and "Second chunk." not in pm
        C.CASCADE_MIN_CONF = 0.99
        try: e = chat(env, message="who is the author")          # SLM low-confidence -> escalate to 7B
        finally: C.CASCADE_MIN_CONF = 0.60
        kinds = [k for k, _ in e]; assert "sources" in kinds
        assert len([x for k, x in e if k == "sources"][0]["citations"]) == 2            # 7B sees both
        assert "Second chunk." in env["llm"].state.last["messages"][-1]["content"]
    finally: C.SLM_TOP_K = 2

def test_access_tiers_off_means_no_filter_and_no_probe(env):
    C = env["C"]; C.ACCESS_TIERS = False; n0 = len(env["fr"].calls)
    try:
        e = chat(env, message="who is the author")
        qs = [a for a in env["fr"].calls[n0:] if a[0] == "FT.SEARCH"]
        assert len(qs) == 1 and "@access" not in qs[0][2] and dict(e)["meta"]["tier"] == "open"
    finally: C.ACCESS_TIERS = True

# ---------------- npz (no-Redis) backend ----------------
def make_npz(tmp_path):
    import json
    from askiisc.embedder import hash_embed
    texts = [("c0", "T1", "public", "attention mechanism in transformers"), ("c1", "T1", "restricted", "embargoed patent claims on the sensor"),
             ("c2", "T2", "public", "soil moisture estimation with satellites"), ("c3", "T2", "", "untagged chunk about graph databases")]
    V = np.stack([hash_embed(t[3], 64) for t in texts])
    meta = [{"id": t[0], "text": t[3], "thesis_id": t[1], "access": t[2], "title": "Th " + t[1], "chapter": "1", "version": "1"} for t in texts]
    f = tmp_path / "chunks.npz"
    np.savez_compressed(f, vectors=V, meta_json=np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8)); return str(f)

def test_npz_search_filters_and_ranks(tmp_path):
    import asyncio
    from askiisc.retrieval import NpzRetriever
    from askiisc.embedder import hash_embed
    r = NpzRetriever.load(make_npz(tmp_path)); q = hash_embed("attention mechanism in transformers", 64)
    top = asyncio.run(r.search(q, k=2)); assert top[0].id == "c0" and top[0].distance < 1e-5
    pub = asyncio.run(r.search(hash_embed("embargoed patent claims on the sensor", 64), k=4, access="public"))
    assert {c.id for c in pub} == {"c0", "c2"}                     # restricted and UNTAGGED are both hidden
    assert [c.id for c in asyncio.run(r.search(q, thesis_id="T2", k=4))] == ["c2", "c3"] or {c.id for c in asyncio.run(r.search(q, thesis_id="T2", k=4))} == {"c2", "c3"}
    assert asyncio.run(r.theses("public")) == ["T1", "T2"] and asyncio.run(r.theses()) == ["T1", "T2"]
    assert asyncio.run(r.search(q, thesis_id="nope")) == []

def test_npz_rejects_wrong_dimension(tmp_path):
    from askiisc.retrieval import NpzRetriever
    f = make_npz(tmp_path); z = np.load(f)
    with pytest.raises(ValueError): NpzRetriever(z["vectors"][:, :32], [{}] * 4)

def test_npz_through_app_public_refusal_uses_real_vector_math(env, tmp_path):
    from askiisc.retrieval import NpzRetriever
    good = env["app"].state.retriever; env["app"].state.retriever = NpzRetriever.load(make_npz(tmp_path))
    try:
        env["slm"].state.last = None
        e = chat(env, message="embargoed patent claims on the sensor")
        assert [k for k, _ in e] == ["refused", "done"] and env["slm"].state.last is None
        e = chat(env, message="attention mechanism in transformers", force="slm")
        assert dict(e)["done"]["model"] == "slm" and "embargoed" not in json.dumps(env["slm"].state.last)
        with client(env) as c: assert c.get("/health").json()["redis"] is True and c.get("/theses").json() == ["T1", "T2"]
    finally: env["app"].state.retriever = good

def test_export_chunks_roundtrip(tmp_path):
    import sys; sys.path.insert(0, "."); from deploy.export_chunks import export
    from askiisc.retrieval import NpzRetriever
    from askiisc.embedder import hash_embed
    class FakeSync:
        def __init__(s): s.d = {b"chunk:1": {b"text": b"hello thesis", b"embedding": hash_embed("hello thesis", 64).tobytes(), b"thesis_id": b"T1"},
                                  b"chunk:2": {b"text": b"bad vec", b"embedding": b"123", b"thesis_id": b"T1"}, b"other:3": {}}
        def scan_iter(s, match, count): return (k for k in s.d if k.startswith(match.rstrip("*").encode()))
        def pipeline(s, t): return s
        def hgetall(s, k): s.q = getattr(s, "q", []) + [k]
        def execute(s): out = [s.d[k] for k in s.q]; s.q = []; return out
    n, bad = export(FakeSync(), "chunk:", str(tmp_path / "o.npz"), default_access="public", dim=64)
    assert (n, bad) == (1, 1)
    r = NpzRetriever.load(str(tmp_path / "o.npz")); assert r.meta[0]["access"] == "public" and r.meta[0]["thesis_id"] == "T1"

def test_ui_served(env):
    with client(env) as c: assert "AskIISc" in c.get("/").text

# ---------------- access control ----------------
def test_outside_network_is_blocked_everywhere(env):
    for ip in ("8.8.8.8", "192.168.1.5"):
        with client(env, ip) as c:
            assert c.get("/").status_code == 403 and c.post("/chat", json={"message": "hi"}).status_code == 403
            assert c.get("/auth/login", follow_redirects=False).status_code == 403

def test_xff_ignored_unless_from_trusted_proxy(env):
    with client(env, "8.8.8.8") as c:
        assert c.get("/me", headers={"x-forwarded-for": "10.1.1.1"}).status_code == 403   # spoof attempt
    env["C"].TRUSTED_PROXIES = ["8.8.8.8"]
    try:
        with client(env, "8.8.8.8") as c:
            assert c.get("/me", headers={"x-forwarded-for": "6.6.6.6, 10.1.1.1"}).status_code == 200   # proxy-appended right-most
            assert c.get("/me", headers={"x-forwarded-for": "10.1.1.1, 6.6.6.6"}).status_code == 403
    finally: env["C"].TRUSTED_PROXIES = []

def test_public_user_only_gets_public_chunks_and_refuses_sensitive(env):
    env["fr"].restricted_dist = 0.05
    try:
        env["slm"].state.last = env["llm"].state.last = None
        e = chat(env, message="tell me about the embargoed result")
        assert [k for k, _ in e] == ["refused", "done"] and "restricted" in e[0][1]["msg"]
        assert env["slm"].state.last is None and env["llm"].state.last is None      # no model call at all
        assert "SECRET" not in json.dumps(e)
        assert all("{public}" in a[2] for a in search_calls(env, "public")[-1:])
        # restricted chunk NOT closer than the public one -> normal answer, and still no SECRET text in the prompt
        env["fr"].restricted_dist = 0.5
        e = chat(env, message="who is the author", force="slm")
        assert dict(e)["done"]["model"] == "slm" and "SECRET" not in json.dumps(env["slm"].state.last)
    finally: env["fr"].restricted_dist = None

def test_internal_user_gets_restricted_chunks(env):
    sess_login(env, "alice@iisc.ac.in")
    # Authenticated: no access filter on the search (restricted + public both visible)
    with client(env) as c:
        c.cookies.set("askiisc_session", env["cookie"])
        assert c.get("/me").json()["tier"] == "internal"
        e = chat(env, c, message="who is the author", force="slm")
        assert dict(e)["meta"]["tier"] == "internal"
        q = search_calls(env, "public")[-1][2]
        assert "@access" not in q

def sess_login(env, email, **claims):
    """Drive the real /auth/login -> /auth/callback flow with the token endpoint + signature key faked."""
    import jwt, time
    from cryptography.hazmat.primitives.asymmetric import rsa
    from askiisc import auth
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    holder = {}
    async def fake_exchange(code, verifier):
        c = {"iss": "https://login.microsoftonline.com/tenant-1/v2.0", "aud": "client-1", "tid": "tenant-1", "iat": int(time.time()),
             "exp": int(time.time()) + 600, "nonce": holder["nonce"], "preferred_username": email, "oid": "oid-1", "name": "Alice"} | claims
        return jwt.encode(c, key, algorithm="RS256")
    orig = (auth.exchange_code, auth._signing_key); auth.exchange_code = fake_exchange; auth._signing_key = lambda t: key.public_key()
    try:
        with client(env) as c:
            r = c.get("/auth/login", follow_redirects=False); assert r.status_code in (302, 307)
            assert "code_challenge_method=S256" in r.headers["location"]
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(r.headers["location"]).query); holder["nonce"] = q["nonce"][0]
            r = c.get("/auth/callback", params={"code": "x", "state": q["state"][0]}, follow_redirects=False)
            env["cookie"] = c.cookies.get("askiisc_session"); return r.status_code
    finally: auth.exchange_code, auth._signing_key = orig

def test_login_rejections(env):
    assert sess_login(env, "alice@iisc.ac.in") in (302, 307)
    assert sess_login(env, "mallory@gmail.com") == 403                                   # wrong domain
    assert sess_login(env, "intern_gmail.com#EXT#@iisc.onmicrosoft.com") == 403          # B2B guest (domain check also catches)
    assert sess_login(env, "intern_x.com#EXT#@iisc.ac.in") == 403                        # passes domain check; ONLY the guest check stops it
    assert sess_login(env, "alice@iisc.ac.in", tid="other-tenant") == 403                # foreign tenant
    assert sess_login(env, "alice@iisc.ac.in", aud="someone-elses-app") == 403           # token minted for another app
    assert sess_login(env, "alice@iisc.ac.in", exp=1) == 403                             # expired
    env["C"].SENSITIVE_GROUP_ID = "grp-1"
    try:
        assert sess_login(env, "alice@iisc.ac.in", groups=[]) == 403
        assert sess_login(env, "alice@iisc.ac.in", groups=["grp-1"]) in (302, 307)
    finally: env["C"].SENSITIVE_GROUP_ID = ""

def test_callback_rejects_bad_state_and_forged_cookie(env):
    with client(env) as c:
        c.get("/auth/login", follow_redirects=False)
        assert c.get("/auth/callback", params={"code": "x", "state": "wrong"}).status_code == 400
        assert c.get("/auth/callback", params={"code": "x", "state": "wrong"}).status_code == 400   # state consumed
    with client(env) as c:
        c.cookies.set("askiisc_session", "forged.value.here")
        assert c.get("/me").json()["tier"] == "public"

def test_session_expiry(env):
    sess_login(env, "alice@iisc.ac.in")
    with client(env) as c:
        c.cookies.set("askiisc_session", env["cookie"]); assert c.get("/me").json()["tier"] == "internal"
        env["C"].SESSION_TTL = -1
        import askiisc.app as A; real = A.time.time; A.time.time = lambda: real() + 10**6
        try: assert c.get("/me").json()["tier"] == "public"
        finally: A.time.time = real; env["C"].SESSION_TTL = 8 * 3600

def test_demo_otp_to_log_works_only_when_enabled(env, otp, caplog):
    import re, logging
    C, auth = otp.C, otp.auth
    def no_relay(*a): raise auth.MailError("SMTP_HOST not configured")   # what the real send_mail does with no relay
    auth.send_mail = no_relay
    old = (C.SMTP_HOST, C.OTP_DEV_LOG)
    try:
        C.SMTP_HOST = ""
        C.OTP_DEV_LOG = False                      # default: no relay and no demo mode -> sign-in must FAIL, not silently succeed
        with client(env) as c: assert req(c).status_code == 503
        C.OTP_DEV_LOG = True
        with client(env) as c, caplog.at_level(logging.WARNING):
            assert req(c).status_code == 200
            m = re.search(r"DEMO OTP .* for alice@iisc.ac.in : (\d{6})", caplog.text); assert m, caplog.text
            assert ver(c, m.group(1)).status_code == 200 and c.get("/me").json()["email"] == "alice@iisc.ac.in"
            assert req(c, "mallory@gmail.com").status_code == 400      # demo mode still refuses non-IISc addresses
    finally: C.SMTP_HOST, C.OTP_DEV_LOG = old

# ---------------- gateway backend (answers come from the team gateway's POST /query) ----------------
def make_gateway(mode="stream", route=("easy", "cpu_slm")):
    a = FastAPI(); a.state.calls = []
    @a.post("/query")
    async def q(body: dict):
        a.state.calls.append(body)
        if mode == "500": return __import__("fastapi").responses.JSONResponse({"detail": "boom"}, status_code=500)
        chunks = [{"chunk_key": "doc:chunk:x_1", "score": "0.457", "doc_id": "dspace_1", "handle_id": "1",
                   "metadata": json.dumps({"title": "Creep of TiAl", "authors": ["A"]}), "text": "TiAl creeps at high temperature."}]
        if mode == "json":
            return {"query": body["query"], "answer": "Whole answer in one piece.", "route": route[0], "target_tier": route[1], "score": 0.01, "chunks": chunks}
        async def g():
            if mode == "cache":     # real captured shape from the team gateway: cache HIT, route semantic_cache, tokens, done cached
                yield 'data: ' + json.dumps({"type": "cache", "status": "HIT", "similarity": 1.0, "matched_prompt": "x"}) + "\n\n"
                yield 'data: ' + json.dumps({"type": "route", "route": "easy", "target_tier": "semantic_cache", "probability_hard": 0.0}) + "\n\n"
                yield 'data: ' + json.dumps({"type": "token", "delta": "Cached "}) + "\n\n"
                yield 'data: ' + json.dumps({"type": "token", "delta": "text"}) + "\n\n"
                yield 'data: ' + json.dumps({"type": "done", "cached": True, "similarity": 1.0, "stats": {"cached": True}}) + "\n\n"
                yield "data: [DONE]\n\n"; return
            yield 'data: ' + json.dumps({"type": "route", "route": route[0], "target_tier": route[1], "probability_hard": 0.02}) + "\n\n"
            yield 'data: ' + json.dumps({"type": "chunks", "chunks": chunks}) + "\n\n"
            if mode != "empty":
                yield 'data: ' + json.dumps({"type": "token", "delta": "Hello"}) + "\n\n"
                yield 'data: ' + json.dumps({"type": "token", "content": " world"}) + "\n\n"
            yield 'data: ' + json.dumps({"type": "done", "stats": {}}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(g(), media_type="text/event-stream")
    @a.get("/healthz")
    async def h(): return {"status": "ok"}
    return a

@pytest.fixture
def gw(env):
    C = env["C"]; old = (C.BACKEND, C.GATEWAY_URL, C.REQUIRE_LOGIN); servers = []
    def start(**kw):
        app = make_gateway(**kw); port = free_port(); servers.append(serve(app, port))
        C.BACKEND, C.GATEWAY_URL = "gateway", f"http://127.0.0.1:{port}"; return app
    yield start
    for s in servers: s.should_exit = True
    C.BACKEND, C.GATEWAY_URL, C.REQUIRE_LOGIN = old

def test_gateway_stream_is_translated_for_the_browser(env, gw):
    g = gw(); e = chat(env, message="What is creep?")
    kinds = [k for k, _ in e]; d = dict(e)
    assert kinds[0] == "meta" and d["meta"]["route"] == "slm" and kinds[-1] == "done" and d["done"]["model"] == "slm"
    assert "".join(x["t"] for k, x in e if k == "token") == "Hello world"
    src = d["sources"]["citations"][0]; assert src["title"] == "Creep of TiAl" and src["thesis_id"] == "dspace_1" and src["distance"] == 0.457
    assert g.state.calls == [{"query": "What is creep?", "stream": True, "max_tokens": env["C"].MAX_TOKENS}]

def test_gateway_hard_route_is_shown_as_llm(env, gw):
    gw(route=("hard", "gpu_llm")); e = chat(env, message="compare chapters")
    assert dict(e)["meta"]["route"] == "llm" and dict(e)["done"]["model"] == "llm"

def test_gateway_cache_hit_is_not_labelled_as_slm(env, gw):
    gw(mode="cache"); e = chat(env, message="x"); d = dict(e)
    assert d["meta"]["route"] == "cache" and d["done"]["model"] == "cache"
    assert "".join(x["t"] for k, x in e if k == "token") == "Cached text"

def test_gateway_single_json_answer_also_works(env, gw):
    gw(mode="json"); e = chat(env, message="x"); d = dict(e)
    assert d["token"]["t"] == "Whole answer in one piece." and d["meta"]["route"] == "slm" and "done" in d

def test_gateway_failures_become_one_clean_error(env, gw):
    gw(mode="500"); e = chat(env, message="x"); assert [k for k, _ in e] == ["error"] and "HTTP 500" in e[0][1]["msg"] and "boom" not in e[0][1]["msg"]
    gw(mode="empty"); e = chat(env, message="x"); assert e[-1][0] == "error" and "no answer" in e[-1][1]["msg"] and "done" not in [k for k, _ in e]
    env["C"].GATEWAY_URL = f"http://127.0.0.1:{free_port()}"            # nothing listening
    e = chat(env, message="x"); assert [k for k, _ in e] == ["error"] and "not reachable" in e[0][1]["msg"]

def test_gateway_mode_still_requires_sign_in_and_never_calls_gateway_unauthenticated(env, gw):
    g = gw(); env["C"].REQUIRE_LOGIN = True
    with client(env) as c:
        assert c.post("/chat", json={"message": "x"}).status_code == 401 and c.get("/theses").status_code == 401
    assert g.state.calls == []
    env["C"].REQUIRE_LOGIN = False
    with client(env) as c: assert c.get("/theses").json() == [] and c.get("/me").json()["backend"] == "gateway"

def test_health_reports_gateway(env, gw):
    gw()
    with client(env) as c: assert c.get("/health").json()["gateway"] is True
    env["C"].GATEWAY_URL = f"http://127.0.0.1:{free_port()}"
    with client(env) as c: assert c.get("/health").json()["gateway"] is False
