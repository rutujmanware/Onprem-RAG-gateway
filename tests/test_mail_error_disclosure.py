"""DEFECT-38: a mail failure during OTP request returns a generic message; details stay in the server log.

Reported: with the SMTP relay unreachable, /auth/otp/request answered 503 with
"Could not send email: ConnectionRefusedError: [Errno 111] Connection refused".
"""

import inspect
import time

from fastapi.testclient import TestClient


def test_gateway_mail_failure_is_generic(monkeypatch, caplog):
    import askiisc.auth as gw_auth
    import gateway.main as gm

    def refuse(*a, **k):
        raise gw_auth.MailError("ConnectionRefusedError: [Errno 111] Connection refused")
    monkeypatch.setattr(gw_auth, "send_mail", refuse)
    st = gw_auth.OtpStore(clock=time.time, redis_client=None)
    monkeypatch.setattr(st, "_get_redis", lambda: None)
    monkeypatch.setattr(gw_auth, "otp_store", st)
    c = TestClient(gm.app)
    c.cookies.set("csrftoken", "t")
    with caplog.at_level("ERROR"):
        r = c.post("/auth/otp/request", json={"email": f"mailfail.{int(time.time())}@iisc.ac.in"}, headers={"X-CSRF-Token": "t"})
    assert r.status_code == 503
    body = r.text
    assert "Errno" not in body and "ConnectionRefused" not in body and "Could not send email:" not in body
    assert "try again" in r.json()["error"]
    assert "Errno 111" in caplog.text, "details are still logged server-side"


def test_standalone_app_does_not_echo_the_exception():
    src = open("askiisc_chat/askiisc/app.py").read()
    assert 'f"Could not send email: {e}"' not in src


def test_no_handler_returns_raw_exception_text():
    """Red-team report: '/v1/chat/completions' returned 500 'GPU LLM generation failed: <exception text>'. That path is
    gone (DEFECT-25/41); no handler may build a client message from an exception again. The only allowed
    str(e) is PromptInjectionAlert, whose message is a fixed text."""
    import re
    for path in ("gateway/main.py", "askiisc_chat/askiisc/app.py"):
        src = open(path).read()
        bad = re.findall(r"""(?:detail=|["']error["']:\s*)f["'][^"']*\{(?:e|exc|err|str\(e\)|str\(exc\))\}""", src)
        assert not bad, (path, bad)
        for m in re.finditer(r"detail=str\((\w+)\)", src):
            before = src[max(0, m.start() - 160):m.start()]
            assert "PromptInjectionAlert" in before, (path, before[-80:])
