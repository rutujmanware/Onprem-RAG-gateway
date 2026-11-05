import os, sys, re
sys.path.insert(0, "tests"); sys.path.insert(0, ".")
import test_e2e as T
from playwright.sync_api import sync_playwright
from askiisc import config as C, auth
ps, pl, pa = T.free_port(), T.free_port(), T.free_port()
T.serve(T.make_stub("SLM", -0.05), ps); T.serve(T.make_stub("LLM", -0.05), pl)
C.SLM_URL, C.LLM_URL = f"http://127.0.0.1:{ps}/v1", f"http://127.0.0.1:{pl}/v1"
C.AUTH_MODE, C.REQUIRE_LOGIN, C.ALLOWED_CIDRS = "otp", True, ["127.0.0.0/8"]
sent = []; auth.send_mail = lambda to, s, b: sent.append(b)
from askiisc.app import app
from askiisc.retrieval import Retriever; from askiisc.router import Router
fr = T.FakeRedis(); app.state.retriever = Retriever(fr); app.state.router = Router()
T.serve(app, pa)
with sync_playwright() as p:
    pg = p.chromium.launch().new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"http://127.0.0.1:{pa}/"); pg.wait_for_selector("text=Sign in to AskIISc")
    pg.fill("#em", "mallory@gmail.com"); pg.click("#gen"); pg.wait_for_selector("text=Enter your IISc email")
    pg.fill("#em", "alice@iisc.ac.in"); pg.click("#gen"); pg.wait_for_selector("#otp")
    pg.fill("#otp", "123456"); pg.click("#ver"); pg.wait_for_selector("text=Invalid or expired")
    code = re.search(r"code is: (\d{6})", sent[-1]).group(1)
    pg.fill("#otp", code); pg.click("#ver"); pg.wait_for_selector("text=Signed in: alice@iisc.ac.in")
    pg.fill("#q", "who is the author"); pg.press("#q", "Enter"); pg.wait_for_selector(".badge.slm"); assert "P(hard)" not in pg.inner_text("#log") and "answered by" not in pg.inner_text("#log")
    print("gate hidden:", not pg.is_visible("#gate")); print("errors:", errs)
