# AskIISc chat (intranet-only)

Chat UI → FastAPI gateway → [embed query → Redis KNN → MLP easy/hard → SLM or 7B (vLLM)], streamed over SSE.

## Host it on the server
**No sudo (shared department server): use `deploy/RUNBOOK_NOSUDO.md` + `deploy/user/askiisc.sh`; `RETRIEVER_BACKEND=npz` loads chunks from one file into RAM (no Redis module needed; export with `deploy/export_chunks.py`).**
With root: See `deploy/RUNBOOK.md` (step-by-step commands), `deploy/systemd/*.service`, `deploy/askiisc.env.example`, `deploy/redis_index.py` (create/backfill/check index + embedder contract).
Auth is a separate workstream: interim = network gate only (`REQUIRE_LOGIN=0`, `ACCESS_TIERS=0`). **With ACCESS_TIERS=0 every chunk is visible to everyone who passes the network gate: load no embargoed/restricted theses until auth exists.** The CPU SLM only receives `SLM_TOP_K` (default 2) chunks; the 7B gets `TOP_K`.

## Run (development)
```bash
pip install -r requirements.txt
export HF_HUB_OFFLINE=1 DO_NOT_TRACK=1
export REDIS_URL=redis://<redis-ip>:6379  INDEX_NAME=rag_documents_idx
export SLM_URL=http://<laptop-ip>:8082/v1 LLM_URL=http://<gpu-ip>:8000/v1 BIND_HOST=<intranet-ip>
python -m askiisc.app          # open http://<intranet-ip>:9000 (or use unified Gateway at :8080)
```
Model servers: any OpenAI-compatible endpoint. 7B: `vllm serve Qwen/Qwen2.5-7B-Instruct --quantization fp8 --kv-cache-dtype fp8 --max-model-len 32768 --gpu-memory-utilization 0.92 --max-num-seqs 32 --enable-prefix-caching`. SLM: vLLM, or `llama-server -m qwen2.5-3b-instruct-q4_k_m.gguf --port 8082` on CPU.

## Sign-in: email one-time code (default, `AUTH_MODE=otp`) and `REQUIRE_LOGIN=1`
No chat, no thesis list, nothing without a session: `/chat` and `/theses` return 401. Flow: user enters IISc email -> "Generate OTP" -> 6-digit code emailed -> verify -> 8h session cookie.
Protections: code stored only as an HMAC hash, valid 3 min (180s), single use, destroyed after 5 wrong tries, 60 s resend gap, 10 codes/hour per mailbox, 1000/hour per client IP, `+tag`/case aliases normalised, mail sent ONLY to allowed domains, identical error for wrong/expired/locked codes, CR/LF header injection blocked, SMTP failure leaves no live code.

**You must set (server):**
```
SMTP_HOST=<IISc internal mail relay>   SMTP_PORT=25|587  SMTP_STARTTLS=1  SMTP_FROM=askiisc-noreply@iisc.ac.in
SESSION_SECRET=<long random>  ALLOWED_CIDRS=<IISc ranges>  ALLOWED_EMAIL_DOMAINS=iisc.ac.in  COOKIE_SECURE=1
```
- Ask the Computer Centre for an **internal relay** that accepts mail from the server IP. Using smtp.office365.com means external egress plus a mailbox password on the server.
- **Check your real address format.** Default accepts only `@iisc.ac.in`. If people use `@csa.iisc.ac.in` etc., set `ALLOW_SUBDOMAINS=1` (or list each domain). Wrong value = nobody can log in, or the wrong people can.
- Multi-worker concurrency: OTP state and rate limits are backed by atomic Lua scripts in Redis (distributed multi-process safe; no in-memory bottlenecks).
- Weakness vs Microsoft SSO: an OTP proves someone can READ that mailbox, not that they are a current member. Forwarding rules, shared/role mailboxes and unrevoked alumni/ex-staff addresses all pass. Keep `AUTH_MODE=entra` as the stronger option (the Entra code is still here).

## Access control (two tiers; only matters if REQUIRE_LOGIN=0 or you restrict theses further)
| Tier | Who | Sees |
|---|---|---|
| **public** | any client IP inside `ALLOWED_CIDRS` (campus Wi-Fi, VPN, lab/guest Wi-Fi unless you exclude that subnet) | only chunks tagged `access=public` |
| **internal** | signed in via IISc Microsoft Entra ID | everything |
| outside CIDRs | anyone else | HTTP 403 for every URL |

Enforcement is in the Redis query (`@access:{public}`), not in the prompt. If a public user's question is closer to a restricted chunk than to any public one (cosine distance < `SENSITIVE_DIST`), no model is called and they get `REFUSAL_MSG` + a sign-in link.

**One-time setup (ask the IISc Computer Centre, needs a tenant admin):**
1. Entra admin centre → App registrations → New, *single tenant*. Redirect URI (type Web) = `${PUBLIC_BASE_URL}/auth/callback` (https required). Create a client secret.
2. Optional: restrict sign-in to an Entra group (assign users/groups to the app, or set `SENSITIVE_GROUP_ID` and enable "groups" claim in the token config; >200 groups triggers claim overage which this code does NOT handle).
3. Set `ENTRA_TENANT_ID ENTRA_CLIENT_ID ENTRA_CLIENT_SECRET PUBLIC_BASE_URL SESSION_SECRET ALLOWED_CIDRS COOKIE_SECURE=1`. Serve over HTTPS (internal CA) via nginx and set `TRUSTED_PROXIES=<nginx ip>`; nginx must use `proxy_add_x_forwarded_for`.
4. **Tag your data**: every chunk needs an `access` TAG field (`public`/`restricted`), and the index must declare it. Untagged chunks are invisible to public users (fail-closed) but visible to signed-in users.

**Egress exception you must record in your audit:** the gateway (only) calls `login.microsoftonline.com` for the token exchange and signing keys. Allowlist exactly that host for the gateway's IP.

**Rejected at sign-in:** wrong tenant, wrong audience, expired/forged token, nonce/state mismatch, non-`@iisc.ac.in` addresses, B2B guest accounts (`#EXT#`), and (if configured) non-members of the group.

**When you add the L0/L1 semantic cache: include the tier in the cache key.** Otherwise a signed-in user's restricted answer is served to the next public user with a similar question. Same for any logging that stores answers.

## Train the router
`python train_router.py labels.csv` — CSV columns `query,label,thesis_id` (1 = hard → 7B). Prints LogReg baseline vs MLP, split by thesis, picks α by best F1, writes `models/router.joblib`.

## Contract you MUST check against your real setup (all env vars in `askiisc/config.py`)
1. **Embedder** (`EMBED_BACKEND/EMBED_MODEL/EMBED_DIM`) must equal the model that built the Redis vectors. Mismatch gives silently wrong retrieval, no error.
2. **Redis schema**: index `INDEX_NAME`; fields `text`, `embedding` (FLOAT32, COSINE), `thesis_id` (TAG), `title`, `chapter`, `version`. Rename via `F_*` vars. Needs Redis Stack (RediSearch).
3. **Router features**: `askiisc/router.py:featurize` = query embedding + 7 cheap features. If your teammate's MLP uses other features, change that ONE function and use it in training too, or the model gets garbage input.
4. Label 1 = hard. Saved file: joblib dict `{model, alpha}`; model needs `predict_proba`.
5. Without `models/router.joblib` the gateway uses a crude heuristic and says so (`reason` in the badge payload / `/health`).

- Sensitivity is only as good as your `access` tags and `SENSITIVE_DIST` (default 0.30 is a guess; tune it on real queries). A near-miss restricted question may get a normal (public-only) answer instead of a refusal; that is a UX miss, not a leak.
- The refusal itself tells a public user that something restricted exists on that topic.
- Network range is a weak factor: guest Wi-Fi users get public answers by design. Exclude the guest subnet from `ALLOWED_CIDRS` if you want them to get nothing.
- No rate limiting/quotas yet (T5).
- Chat history comes from the client and is not validated beyond role filtering; retrieved text is untrusted (prompt-injection, T3) — prompt says "data not instructions", which is mitigation, not a fix.
- Cascade discards SLM output and reruns on 7B: costs both; tune `CASCADE_MIN_CONF` on your eval set.
- Traces go to `traces.jsonl` and contain chunk ids, not query text.
- `/health` shows which backend is down. Tests: `pytest tests` (29 + deploy/test script; stub models, fake Redis, locally signed fake Entra tokens). Real Entra, real RediSearch and TLS are NOT exercised.


## Running with the team gateway (real answers)
`BACKEND=gateway` forwards each question to the team gateway (`POST /query`, stream=true), which routes (two-tower MLP), retrieves from Redis and
generates with the CPU SLM or the 7B. This app keeps the login, the network gate and the chat page. Copy `app.env.example` to `app.env`, set
`SESSION_SECRET`, then `./app.sh start`; read the demo sign-in code with `./app.sh code`. Append `?debug=1` to the page URL to see routing details.
Limits of this mode: no chat history and no per-thesis filter (the gateway's /query takes only the question).
