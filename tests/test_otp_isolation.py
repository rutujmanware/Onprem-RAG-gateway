"""DEFECT-35: one person cannot stop another from signing in (OTP).

Reported: (a) wrong guesses by anyone who knows an address burned that user's pending code; (b) code requests by
anyone exhausted the per-address limits so the user could not get a new code. Codes are now bound to the
requesting browser (challenge in the signed session) and request limits are per (address, IP).
"""

import re
import time

import pytest
from fastapi.testclient import TestClient

from askiisc_chat.askiisc import auth
from askiisc_chat.askiisc import config as C

V = "victim@iisc.ac.in"


def store():
    clock = [1000.0]
    s = auth.OtpStore(clock=lambda: clock[0])   # in-memory mode
    return s, clock


def test_attackers_wrong_guesses_do_not_burn_the_victims_code():
    s, _ = store()
    victim = s.issue(V, "10.0.0.1")
    attacker = s.issue(V, "10.6.6.6")            # attacker's own challenge (code went to the victim's inbox)
    for g in range(7):
        assert s.verify(V, f"{g:06d}", attacker.challenge) is False
        assert s.verify(V, f"{g:06d}", "") is False                  # no challenge at all
        assert s.verify(V, f"{g:06d}", "made-up-challenge") is False
    assert s.verify(V, victim, victim.challenge) is True


def test_a_code_only_works_with_its_own_challenge():
    s, _ = store()
    a, b = s.issue(V, "10.0.0.1"), s.issue(V, "10.0.0.2")
    assert s.verify(V, a, b.challenge) is False or a == b
    assert s.verify(V, a, a.challenge) is True
    assert s.verify(V, a, a.challenge) is False                      # single use


def test_wrong_guesses_still_burn_a_challenge_after_max_tries():
    s, _ = store()
    c = s.issue(V, "10.0.0.1")
    wrong = "000000" if c != "000000" else "000001"
    for _ in range(int(C.OTP_MAX_TRIES)):
        assert s.verify(V, wrong, c.challenge) is False
    assert s.verify(V, c, c.challenge) is False


def test_brute_force_budget_is_capped_but_the_victims_first_try_always_counts():
    s, clock = store()
    for _ in range(10):                                              # attacker spends the hourly failure budget
        a = s.issue(V, f"10.6.6.{_}")
        for g in range(int(C.OTP_MAX_TRIES)):
            s.verify(V, f"{g:06d}", a.challenge)
        clock[0] += 61
    a = s.issue(V, "10.6.7.1")
    s.verify(V, "999999" if a != "999999" else "999998", a.challenge)
    assert s.verify(V, a, a.challenge) is False, "a challenge that already failed is locked once the cap is reached"
    v = s.issue(V, "10.0.0.1")
    assert s.verify(V, v, v.challenge) is True, "the victim's fresh code still works"


def test_attackers_request_flood_does_not_block_the_victim():
    s, clock = store()
    blocked = 0
    for _ in range(12):
        try:
            s.issue(V, "10.6.6.6")
        except auth.RateLimited:
            blocked += 1
    assert blocked >= 11                                             # attacker is rate limited
    code = s.issue(V, "10.0.0.1")                                    # the victim, from their own IP, is not
    assert s.verify(V, code, code.challenge) is True


def test_per_ip_hourly_cap_and_per_address_ceiling():
    s, clock = store()
    for _ in range(int(C.OTP_MAX_PER_HOUR)):
        s.issue(V, "10.6.6.6")
        clock[0] += C.OTP_RESEND_SECS + 1
    with pytest.raises(auth.RateLimited):
        s.issue(V, "10.6.6.6")
    issued = int(C.OTP_MAX_PER_HOUR)
    ip = 0
    while issued < C.OTP_MAX_PER_EMAIL_HOUR:
        ip += 1
        s.issue(V, f"10.7.0.{ip}")
        issued += 1
    with pytest.raises(auth.RateLimited):                            # mail-bombing ceiling across all IPs
        s.issue(V, "10.8.0.1")


# ------------------------------------------------------------- through the real endpoints
@pytest.fixture
def gw(monkeypatch):
    import gateway.main as gm
    import askiisc.auth as gw_auth          # the module object the gateway's web routes use
    sent = {}
    monkeypatch.setattr(gw_auth, "send_mail", lambda to, subj, body: sent.setdefault(to, []).append(re.search(r"\d{6}", body).group(0)))
    st = gw_auth.OtpStore(clock=time.time, redis_client=None)
    monkeypatch.setattr(st, "_get_redis", lambda: None)
    monkeypatch.setattr(gw_auth, "otp_store", st)
    return gm.app, sent, st


def browser(app):
    c = TestClient(app)
    c.cookies.set("csrftoken", "t")
    return c


H = {"X-CSRF-Token": "t"}


def test_endpoints_victim_signs_in_despite_attacker_guesses(gw):
    app, sent, st = gw
    email = f"victim.{int(time.time() * 1000)}@iisc.ac.in"
    victim, attacker = browser(app), browser(app)
    assert victim.post("/auth/otp/request", json={"email": email}, headers=H).status_code == 200
    code = sent[email][-1]
    for g in range(7):
        r = attacker.post("/auth/otp/verify", json={"email": email, "code": f"{(int(code) + g + 1) % 10**6:06d}"}, headers=H)
        assert r.status_code in (401, 429)
    assert attacker.post("/auth/otp/verify", json={"email": email, "code": code}, headers=H).status_code in (401, 429), \
        "even the right code does not work from a browser that did not request it"
    st.by_ip.clear()                                                 # attacker and victim share the test client IP
    assert victim.post("/auth/otp/verify", json={"email": email, "code": code}, headers=H).status_code == 200
    assert victim.get("/me").json().get("email") == email


def test_otp_store_reconnects_after_redis_was_down_at_first_use(monkeypatch):
    """DEFECT-61: Redis unreachable at a worker's first sign-in used to pin that worker to its in-memory store
    until a restart (codes from it failed on the other workers). It now retries with a growing wait."""
    import redis as redis_mod

    class FakeRedis:
        def __init__(self, up): self.up = up
        def ping(self):
            if not self.up:
                raise ConnectionError("down")
            return True

    state = {"up": False, "attempts": 0}

    def from_url(*a, **k):
        state["attempts"] += 1
        return FakeRedis(state["up"])

    monkeypatch.setattr(redis_mod.Redis, "from_url", staticmethod(from_url))
    st = auth.OtpStore(clock=time.time, redis_client=None)
    assert st._get_redis() is None and state["attempts"] == 1           # down: memory fallback for now
    assert st._get_redis() is None and state["attempts"] == 1           # no retry storm before the backoff ends
    first_wait = st._redis_backoff
    st._redis_retry_at = 0.0                                             # backoff elapsed, still down
    assert st._get_redis() is None and st._redis_backoff == min(60.0, first_wait * 2)
    state["up"] = True
    st._redis_retry_at = 0.0                                             # backoff elapsed, Redis back
    assert isinstance(st._get_redis(), FakeRedis) and st._redis_backoff == 0.0
    assert state["attempts"] == 3
