# Blue-Team Final Summary Report

## 1. Project and team

| | |
|---|---|
| **Repository** | https://github.com/rutujmanware/Onprem-RAG-gateway (AskIISc: on-premise RAG gateway) |
| **Team members** (ALTUS) | Rituj Manware, Ancita Lewis, Vivek Kumar, Kriti Chaudhary|
| **Baseline tag and commit** | `rt-baseline`, [`9f9117268f07e8090748d59cabb995bd50ac0cee`](https://github.com/rutujmanware/Onprem-RAG-gateway/commit/9f9117268f07e8090748d59cabb995bd50ac0cee) (2026-11-12) |
| **Final commit** | [`8d7ac069b0e119b2eb6d94337e6d3749db0f98ae`](https://github.com/rutujmanware/Onprem-RAG-gateway/commit/8d7ac069b0e119b2eb6d94337e6d3749db0f98ae) (`main`, 2026-11-19) |
| **Deployment/test environment** | Shared on-premise Linux host: 2× AMD EPYC 7713 (128 cores), 8× NVIDIA A100 40 GB, 504 GB RAM, other users' jobs running alongside. Python 3.11; Redis Stack 7.2.0-v9 with 2,261,664 thesis chunks; Qwen2.5-7B on vLLM 0.6.3 (one GPU, loopback only, API key); Qwen2.5-3B Q4_K_M on the CPU tier (6 slots × 16 threads); gateway with 6 uvicorn workers on `:8080`, plain HTTP on the campus network. The red teams retested on their own isolated deployments built from the stated commits. |

## 2. Summary

| | |
|---|---|
| **Issues received** | 29 (GitHub issues #2–#17, #19–#23, #25–#32; #1, #18 and #24 are pull requests) |
| **Confirmed Major** | 20 |
| **Confirmed Minor** | 9 |
| **Fixed and retested** | 26 (red-team retest passed) |
| **Fixed but awaiting retest** | 0. #16 was retested only partly (see §6). |
| **Open or partially fixed** | 2: #5 (same-user intent collisions, accepted known limitation) and #17 (injection blocking below the NFR-7 target) |
| **Duplicate/invalid/cannot-reproduce/works-as-designed** | 0 whole issues. One part of #16 (identical questions share one cached answer across users) works as designed (REVIEW-1 in `fixes.md`). |
| **Regression tests added** | 39 new test files since `rt-baseline` (15 → 54). `make test` on the final commit: 1,238 pytest tests passed (574 + 37 + 627), 5 skipped (need services that were not running), 12 expected failures (documented limitations, PRD §9.1), plus the script suites. Four further regression files contain the red team's attack data, so they are kept out of the public repository; `make test` runs them where present. |

## 3. Issue resolution

Fix references are entries in [`fixes.md`](fixes.md) (DEFECT-n / VULN-n); every issue's GitHub thread has the fix description and the commit to retest.

| Issue | Severity | Classification | Fix/PR | Regression test | Red-team result | Final status |
|---|---|---|---|---|---|---|
| [#2](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/2) No-results answer shown with thesis sources | Minor | Correctness | `cade698` | `test_refusal_chunks.py` | Passed | Fixed (closed) |
| [#3](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/3) Repeated thesis titles listed as separate sources | Minor | Correctness | `cade698` | Manual retest only | Passed | Fixed (closed) |
| [#4](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/4) Model API reachable outside the gateway | Major | Security | VULN-09, `15de9f9` | `test_vllm_launcher.py`, `make vllm-check` | Passed | Fixed (closed) |
| [#5](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/5) Semantic cache replays answers across changed intent | Major | Data integrity | VULN-10 (intent guard), Sub-FR-1.4 owner scoping | `test_cache_owner_scoping.py`, `test_cache_intent_guard_extensive.py` (private) | Accepted as known limitation | Partially fixed (closed); see §6 |
| [#6](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/6) Routing at the threshold selects the CPU tier | Minor | Correctness | DEFECT-12 | `test_dispatcher_threshold.py` | Passed | Fixed (closed) |
| [#7](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/7) Ungrounded answers cached on `/v1/chat/completions` | Major | Data integrity | DEFECT-23 | `test_ungrounded_and_admin_guard.py` | Passed | Fixed (closed) |
| [#8](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/8) GPU failure gives HTTP 500 instead of CPU fallback | Major | Reliability | DEFECT-25; first proposed in PR #18 | `test_gpu_fallback.py` | Passed | Fixed |
| [#9](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/9) Exception text in HTTP 500 responses | Minor | Security (information disclosure) | DEFECT-25, -38, -41 | `test_gpu_fallback.py` | Passed | Fixed |
| [#10](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/10) One failing GPU request stalls the worker (`/health` 43 s) | Major | Reliability | DEFECT-25, -26, -47 | `test_event_loop_not_blocked.py` | Passed | Fixed |
| [#11](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/11) Per-user concurrency cap never enforced | Major | Reliability (DoS) | DEFECT-27 | `test_per_user_cap.py` | Passed | Fixed |
| [#12](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/12) Redis outage returns unhandled HTTP 500 | Major | Reliability | DEFECT-28 | `test_retrieval_outage.py` | Passed | Fixed |
| [#13](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/13) `/health` says ok while Redis is down | Minor | Reliability (observability) | DEFECT-42 (`/ready`) | `test_readiness.py` | Passed | Fixed |
| [#14](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/14) Any signed-in user can clear the shared cache | Major | Security (authorization) | DEFECT-24, -29, -54 | `test_ungrounded_and_admin_guard.py`, `test_hidden_routes.py` | Passed | Fixed |
| [#15](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/15) Injection fence checks only the last message | Major | Security (prompt injection) | DEFECT-31, -41 | `test_injection_all_messages.py` | Passed | Fixed |
| [#16](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/16) Semantic cache not scoped per user | Major | Security (cache isolation) | Sub-FR-1.4 owner scoping, DEFECT-30, -39; REVIEW-1 | `test_cache_owner_scoping.py`, `test_cache_regenerate.py` | Passed (partial retest) | Fixed; exact-repeat sharing by design |
| [#17](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/17) Injection blocking 40 % vs NFR-7 ≥ 95 % | Major | Security (prompt injection) | DEFECT-32, -49, -51, -52, -59 | `test_injection_detection.py`, `make injection-eval`, private red-team examples | Passed on the reporter's 15-prompt set (15/15, 0/8 false positives) | Partially fixed: 88.9 % on the 279-attack suite; see §6 |
| [#19](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/19) Clean install pulls the wrong `jwt` package | Minor | Dependency/build | DEFECT-43 | `test_requirements.py` | Passed | Fixed |
| [#20](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/20) Catalog queries return fabricated scores and counts | Major | Correctness (data integrity) | DEFECT-33 | `test_metadata_queries.py` | Passed | Fixed |
| [#21](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/21) Admission permit leaked on client disconnect | Major | Reliability (DoS) | DEFECT-34; first proposed in PR #24 | `test_admission_cancellation.py` | Passed | Fixed |
| [#22](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/22) Third party can lock a user out of OTP sign-in | Major | Security (authentication availability) | DEFECT-35 | `test_otp_isolation.py` | Passed | Fixed |
| [#23](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/23) Generation-failure placeholder cached and replayed | Major | Data integrity | DEFECT-36, `c50a120` | `test_failure_not_cached.py` | Passed | Fixed |
| [#25](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/25) Retrieved context not escaped in the prompt | Major | Security (prompt injection) | DEFECT-37, -58, `25ae01a` | `test_context_escaping.py` | Passed | Fixed |
| [#26](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/26) Protected endpoints reveal themselves with 405/422 | Minor | Security (information disclosure) | DEFECT-24, -54 | `test_hidden_routes.py` | Passed | Fixed |
| [#27](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/27) OTP request returns raw SMTP error text | Minor | Security (information disclosure) | DEFECT-38, `9bb9ef5` | `test_mail_error_disclosure.py` | Passed | Fixed |
| [#28](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/28) `bypass_cache` request overwrites the shared answer | Major | Data integrity (cache poisoning) | DEFECT-39, -56, `1092b9e` | `test_cache_overwrite.py` | Passed | Fixed |
| [#29](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/29) `KEYS` scans block Redis | Major | Performance/availability | DEFECT-21, -29 | `test_cache_index_alter.py` (no `KEYS` anywhere) | Passed | Fixed |
| [#30](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/30) Streaming path routes at 0.5 instead of α = 0.65 | Minor | Correctness (routing) | DEFECT-15 | `test_premise_routing.py` | Passed | Fixed |
| [#31](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/31) Typo normalizer rewrites distinct correct terms | Major | Correctness (cache integrity) | DEFECT-40, -62, `238e3e0` | `test_typo_protection.py` | Passed | Fixed |
| [#32](https://github.com/rutujmanware/Onprem-RAG-gateway/issues/32) `/v1/chat/completions` forwards caller messages raw | Major | Security (grounding bypass) | DEFECT-41 | `test_injection_all_messages.py`, `test_gpu_fallback.py`, `test_ungrounded_and_admin_guard.py` | Passed | Fixed |

## 4. Key fixes

**Correctness**
- Routing uses the configured α = 0.65 on every path, with `P(hard) = α` going to the GPU tier (#6, #30).
- Catalog and count answers report real index data instead of invented similarity scores and totals (#20).
- The typo normalizer no longer rewrites real words or technical coinages. A cache hit for a rewritten question is checked against what the user actually typed (#31).
- Refusals no longer show sources, and duplicate theses are merged into one source (#2, #3).
- Beyond the issues: questions about rare technical terms find their theses (rare-term hit@3 0.467 → 0.758), and false-premise questions are not confirmed.

**Security**
- The perimeter is closed. The 7B and 3B model servers listen on loopback only and require keys (#4). Admin routes need the admin key. Protected routes answer exactly like missing pages (404), whatever the request looks like (#14, #26).
- Errors no longer expose exception text or mail-server details (#9, #27).
- Prompt injection:
  - every message is checked, not only the last (#15);
  - retrieved documents are escaped before they enter the prompt (#25);
  - `/v1/chat/completions` goes through the grounded pipeline, not straight to the model (#32);
  - repeat offenders are blocked for 15 minutes.
- OTP codes are bound to the browser that requested them, so a third party cannot lock someone out (#22).
- The chat page has a Content-Security-Policy and safe answer rendering: no stored XSS, no styled fake sign-in screens, and no links outside the institution.

**Data integrity (semantic cache)**
- Near-duplicate hits come only from the same user's own history (#16). Failed or ungrounded answers are never stored (#7, #23).
- `bypass_cache`, `top_k` and regenerate requests can no longer replace the shared answer (#28).

**Reliability**
- When the GPU tier fails, hard questions fall back to the CPU tier instead of returning HTTP 500 (#8). Slow work runs off the event loop, so `/health` stays responsive (#10).
- The per-user concurrency cap is enforced across workers (#11). Admission permits are returned when a client disconnects (#21).
- A Redis outage gives a clear HTTP 503 (#12). `/ready` reports whether the gateway can actually answer (#13). The OTP store reconnects to Redis.

**Performance**
- Semantic cache hits at 100 QPS for 10 minutes: p99 15.0 ms (target ≤ 35 ms). This came from the ONNX query embedder, and from running the injection classifier only on cache misses.
- CPU-tier answers no longer stall about 90 s.
- There are no `KEYS` or `FT.INFO` calls on the 2.26M-key database, so Redis can't be blocked or deadlocked (#29).

## 5. Security remediation

| Area | Outcome |
|---|---|
| Model servers | vLLM is forced onto loopback (vLLM 0.6.3 ignored `--host`) and requires `VLLM_API_KEY` (VULN-09, #4). The CPU model server requires `SLM_API_KEY` (DEFECT-45). Both are checked by `make vllm-check` and `make prod-status`. |
| Administration | Cache and queue administration requires `ADMIN_API_KEY`, also from the server itself. A signed-in session is never enough (DEFECT-24/29, #14). |
| Identity | The identity header between the chat UI and the gateway is signed, so local users cannot impersonate others (DEFECT-30). `X-Forwarded-For` is never trusted (VULN-01, DEFECT-44). |
| Sessions and sign-in | No hard-coded session secret. Production refuses weak or known secrets (VULN-02, DEFECT-48). OTP codes are bound to the requesting browser (DEFECT-35). Sign-in requests need a CSRF token. |
| Information disclosure | No exception, library or mail-server text in responses (#9, #27). Hidden routes are indistinguishable from missing pages (#26). |
| Prompt injection | The fence covers all messages and escapes retrieved context. Answers are grounded only. Planted links and images are inert. Repeat offenders are blocked (#15, #17, #25, #32). |
| Cache isolation | Owner-scoped semantic hits: 0 cross-account leaks in 500 probes (NFR-4). Shared answers cannot be overwritten (#16, #28). |
| Browser | Stored XSS through the math renderer is closed. Content-Security-Policy, anti-framing and no-store headers are set (DEFECT-53, -55, -57). |
| **Credential rotation** | The previously hard-coded session secret was replaced and is now rejected (`test_forged_session_cookie_with_leaked_key_rejected`). `VLLM_API_KEY`, `SLM_API_KEY` and `ADMIN_API_KEY` did not exist before these fixes; every deployment generates its own random values (README step 3). All secrets live in `.env` / `smtp.env` (ignored by Git, mode 600). No secret values appear in this report. |

## 6. Unresolved issues

**#5: same-structure intent changes within one user's history (PRD §9.1 L1).**
- **Cause:** the rule-based intent guard does not detect a single entity, attribute or condition swapped in the same slot when the two questions still score ≥ 0.90 similarity.
- **Impact:** a user can get the cached answer to their own earlier, different question. This affects answer quality only. Other users' entries are never served (owner scoping, 0/500 cross-account leaks).
- **Workaround:** the "Generate a fresh answer" button on every cached answer, or `bypass_cache`. Each known case is pinned as an expected failure in the guard test suite.
- **Next step:** add a learned equivalence check (for example a cross-encoder) before serving a semantic hit, and tune the threshold against the guard suite. The red team accepted this as the documented limitation.

**#17: prompt-injection blocking below the NFR-7 target (PRD §9.1b L21).**
- **Cause:** attackers keep producing new phrasings, especially output hijacks that set a field to a fixed value and requests to "print the keys".
- **Impact:** on the frozen round-4 suite of 279 attacks the deployed fence blocks 88.9 % (95 % CI 84.7–92.1 %) against the ≥ 95 % target, up from 71.3 % before this work. False positives are 0/453 on genuine English questions (target ≤ 1 %, met). An attack that gets through still meets other defences: escaped context, a security prompt, grounded-only answers, inert links and images, and repeat-offender blocking.
- **Workaround:** none needed beyond those layers.
- **Next step:** retrain the classifier with round 4 and the red team's 15-prompt set, and measure on a fresh suite of unseen phrasings.

**#16: retest only partial.**
- **Cause:** the red team confirmed that two users got independent, uncached answers, but did not rerun the full original sequence.
- **Impact:** none known. Our own measurement found 0 leaks in 500 cross-account probes.
- **Next step:** a full retest by the red team.

**Other documented limitations** (PRD §9; not red-team issues):
- **No TLS on the campus network (L22):** sessions are not `Secure`-only.
- **Expensive `/v1/chat/completions` requests (L25):** one request with 50 long messages can occupy a gateway worker for about 3 s. Accepted.
- **Typo normalizer latency target revised:** the target is now p99 ≤ 5 ms (met). The original ≤ 0.5 ms was not achievable once real terms were checked against the corpus.

## 7. Evidence

- **Issues and retest comments:**
  - [all issues](https://github.com/rutujmanware/Onprem-RAG-gateway/issues?q=is%3Aissue)
  - every thread has the fix description, the commit to test, and the red team's retest result: #2–#7 (2026-11-13/14/17); #8–#17 (2026-11-19, AdityaBarodia); #19–#21 (2026-11-19, SpookyWooky5); #22–#32 (2026-11-19, mugiwara999, on `8d7ac06`).
- **Pull requests:**
  - [#18](https://github.com/rutujmanware/Onprem-RAG-gateway/pull/18) (CPU fallback, issue #8) and [#24](https://github.com/rutujmanware/Onprem-RAG-gateway/pull/24) (admission permits, issue #21) were contributed proposals. Both fixes were implemented on `main` with tests (DEFECT-25, DEFECT-34).
  - Development otherwise went through the `rituj` branch merged into `main`: 45 commits after `rt-baseline`.
- **Commits:**
  - [baseline `9f91172`](https://github.com/rutujmanware/Onprem-RAG-gateway/commit/9f9117268f07e8090748d59cabb995bd50ac0cee) → [final `8d7ac06`](https://github.com/rutujmanware/Onprem-RAG-gateway/commit/8d7ac069b0e119b2eb6d94337e6d3749db0f98ae)
  - [full comparison](https://github.com/rutujmanware/Onprem-RAG-gateway/compare/rt-baseline...8d7ac069b0e119b2eb6d94337e6d3749db0f98ae)
- **Defect log:** [`fixes.md`](fixes.md) has the root cause, fix and verification for VULN-01…10, DEFECT-09…62 and REVIEW-1.
- **Requirements and limitations:** [`product_requirements_document.md`](product_requirements_document.md); measured NFR results are in §5 and known limitations in §9.
- **Regression tests:**
  - [`tests/`](tests/), run with `make test` (see [README §7](README.md#7-verification--test-suites))
  - final run on `8d7ac06`: 574 + 37 + 627 passed, 5 skipped, 12 expected failures, script suites OK
  - there is no hosted CI: the full suite needs Redis, the models and the corpus on the on-premise host
- **Benchmarks** (scripts in [`scripts/`](scripts/); result files under `logs/perf/` on the deployment host):

  | Requirement | Result |
  |---|---|
  | NFR-1 semantic cache hits | p99 15.0 ms at 100 QPS for 10 min, 0 errors; 16.8 ms re-check after DEFECT-62 (`load_test_cache.py`) |
  | NFR-4 cross-account leaks | 0 in 500 probes (`measure_cross_account.py`) |
  | NFR-5 burst handling | 50× burst: no crash, no HTTP 500, full recovery (`measure_burst.py`) |
  | NFR-3 cost | CPU tier answers 81 %, GPU time −92 % (`measure_configs.py`) |
  | NFR-7 injection blocking | 88.9 % blocked, 0/453 false positives (`make injection-eval`) |
  | Typo normalizer latency | p99 ≤ 4.5 ms against the revised ≤ 5 ms target (`measure_typo_latency.py`) |
