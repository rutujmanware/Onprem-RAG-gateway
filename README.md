# 🏛️ On-Premise Institutional RAG Gateway (AskIISc)

A 100% bare-metal, zero-egress Retrieval-Augmented Generation (RAG) platform and API Gateway built for the Indian Institute of Science (IISc). The entire corpus of institutional theses and research papers (**2,261,664 chunks**) is indexed in **Redis Stack** RAM. A PyTorch Two-Tower **complexity router** dynamically dispatches queries to either an ultra-fast **CPU Small Language Model** (Qwen 2.5 3B across 6 workers $\times$ 16 threads) or a high-capacity **GPU Large Language Model** (Qwen 2.5 7B served via **vLLM** on an NVIDIA A100), with automatic fallback to CPU on GPU saturation.

The platform runs behind a single-port secure perimeter (**Port 8080**) featuring email OTP authentication, CSRF protection, token-bucket admission control, low-latency typo normalization (p99 ≤ 5 ms), and a two-level semantic cache.

📖 **Engineering Manuals:** [complete_system_documentation.md](complete_system_documentation.md) (Complete Architecture) · [product_requirements_document.md](product_requirements_document.md) (PRD & Requirements) · [docs/OPTIMIZATION_GUIDE.md](docs/OPTIMIZATION_GUIDE.md) (CPU SLM Tuning) · [fixes.md](fixes.md) (Defect log)

---

## 📑 Table of Contents
1. [Architecture & Topology](#1-architecture--topology)
2. [Prerequisites & System Sizing](#2-prerequisites--system-sizing)
3. [Step-by-Step Setup (Fresh Machine)](#3-step-by-step-setup-fresh-machine)
4. [Daily Operation](#4-daily-operation)
5. [Running the Services Individually](#5-running-the-services-individually)
6. [Building the Corpus Yourself (Ingestion)](#6-building-the-corpus-yourself-ingestion)
7. [Verification & Test Suites](#7-verification--test-suites)
8. [Configuration Reference](#8-configuration-reference)
9. [Service Port Reference](#9-service-port-reference)
10. [Troubleshooting Guide](#10-troubleshooting-guide)
11. [Quick Start Cheat Sheet](#11-quick-start-cheat-sheet)

---

## 1. Architecture & Topology

> [!IMPORTANT]
> **Zero Docker Guarantee:** This system runs **100% bare-metal on the host OS** using native user-space binaries. No Docker, no root privileges, no container runtimes, and no external cloud API dependencies are required.

```
 Browser / Client ─► AskIISc Unified Gateway & Web UI :8080
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
      Email OTP Session Auth    Prompt Injection Fence [NFR-7]
               │                         │
               └────────────┬────────────┘
                            ▼
               Typo Normalizer (p99 ≤ 5 ms)
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
      L0 Exact Cache (< 1ms)    L1 HNSW Semantic Cache (< 35ms)
                            │ (Cache Miss)
                            ▼
               Admission Controller & Fair Token Bucket
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
     Task A: Complexity Router  Task B: Dynamic RediSearch Retriever
         (Two-Tower MLP 400-d)       (HNSW 384-d, 2.26M Chunks, EF=64)
               │                         │
               └────────────┬────────────┘
                            ▼
            Barrier Join & Document Slicing
           ┌────────────────┴────────────────┐
     Easy (P < 0.65)                   Hard (P >= 0.65)
           ▼                                 ▼
 CPU SLM :8082 (Qwen2.5-3B)        GPU LLM :8000 (vLLM Qwen2.5-7B)
 (6 slots x 16 threads)            (NVIDIA A100 80GB, PagedAttention)
           │                                 │
           │                       (SLA Timeout / 5xx Error)
           │                                 ▼
           └──────────────┬──────────────────┘
                          ▼
            Anti-Pollution Shield [NFR-6]
     (Blocks 0-chunk & refusal caching from L1 index)
                          ▼
                 Streaming SSE Response
```

**Corpus Scale:** 12,900+ IISc theses $\to$ **2,261,664 chunks** indexed in RAM. Snapshot: **12.67 GB on disk** (`dump_final.rdb`), **~43.8 GB in RAM**.

---

## 2. Prerequisites & System Sizing

### Software Requirements
- **OS:** Linux x86_64 (Ubuntu 20.04 / 22.04 LTS tested).
- **Python:** 3.10 to 3.12 (developed and tested on 3.11). Miniconda or `venv` both work.
- **NVIDIA driver with CUDA 12.x:** for the GPU tier (vLLM 0.6.3). Without a GPU, every question is answered by the CPU tier.
- **Build tools:** `gcc`, `g++`, `make`, `cmake` (to compile `llama-cpp-python` for the CPU tier).
- **Tools used in this README:** `git`, `wget`, `curl`; `jq` (optional, pretty-prints JSON); `tmux` (optional, for long ingestion runs); `firefox` (optional, for the browser security tests).
- **An SMTP relay** that can send mail to your users: sign-in uses one-time codes sent by email.

### Hardware Sizing

| Component | Minimum Dev Spec | Recommended Production Spec |
| :--- | :--- | :--- |
| **Host RAM** | **64 GB RAM** *(Redis requires ~44 GB)* | **128 GB – 256 GB RAM** |
| **CPU** | 8+ cores (x86_64 AVX2) | 32 to 64 cores with **AVX-512** (AMD EPYC / Intel Xeon) |
| **GPU** | None *(runs 100% CPU SLM fallback)* | **1× NVIDIA A100 (40GB or 80GB)** or **RTX A5000 (24GB)**; vLLM needs ~19 GB free |
| **Disk Storage** | 50 GB SSD | **250 GB+ NVMe PCIe Gen4 SSD** *(models ~20 GB + snapshot 12.67 GB)* |

---

## 3. Step-by-Step Setup (Fresh Machine)

> [!NOTE]
> **Every command in this README is run from the repository root** (the folder created by `git clone`). All paths are relative to it, so the repository can live anywhere; nothing needs to be edited by hand except the settings named in step 3.

### Step 1: Clone the repository
```bash
git clone https://github.com/rutujmanware/Onprem-RAG-gateway.git
cd Onprem-RAG-gateway
```

### Step 2: Create the Python environment
```bash
make setup                       # creates .venv/ and installs requirements.txt
source .venv/bin/activate
make install-serving             # vLLM 0.6.3 (GPU tier) + llama-cpp-python (CPU tier); compiles, takes a few minutes
```
- `make setup` uses `python3`. To use a specific interpreter: `make setup PYTHON=python3.11`.
- Prefer conda? Create and activate an env (`conda create -n rag_env python=3.11 && conda activate rag_env`), then run `pip install -r requirements.txt` and `make install-serving`. `make` uses the active conda env when there is no `.venv/`.
- To make `make` always use one interpreter on your machine without changing tracked files, create `Makefile.local` (ignored by Git) containing `PYTHON := /path/to/your/env/bin/python`.
- All four services are started with the same interpreter, so vLLM and llama-cpp-python must be installed in this same environment.
- Machine without a GPU: install only the CPU runtime with `pip install "llama-cpp-python>=0.2.90"` and skip vLLM.

### Step 3: Create the configuration files

**3a. Main settings (`.env`).** Copy the template and generate the secrets (random 64-hex-character values):
```bash
cp .env.example .env
chmod 600 .env
for k in REDIS_PASSWORD SESSION_SECRET ADMIN_API_KEY SLM_API_KEY VLLM_API_KEY; do
  sed -i "s|^$k=.*|$k=$(python3 -c 'import secrets; print(secrets.token_hex(32))')|" .env
done
sed -i "s|^REDIS_URL=.*|REDIS_URL=redis://:$(grep '^REDIS_PASSWORD=' .env | cut -d= -f2)@localhost:6379/0|" .env
```
Then open `.env` in an editor and check these values for your site:

| Setting | What to set |
| :--- | :--- |
| `ALLOWED_EMAIL_DOMAINS` | E-mail domain(s) allowed to sign in (default `iisc.ac.in`; comma-separated). |
| `VLLM_GPU` | Index of the GPU for vLLM (see `nvidia-smi`; pick one with ~19 GB free). |
| `CPU_NUM_THREADS`, `SLM_SLOTS` | CPU tier sizing (default 6 slots × 16 threads = 96 cores; lower them on smaller machines). |
| `PYTHON` *(optional)* | Interpreter for `make` if it is neither `.venv/` nor an active conda env. |

Everything else has working defaults; [§8](#8-configuration-reference) lists the most important ones.

**3b. Mail settings (`smtp.env`)**, used to send sign-in codes:
```bash
cp smtp.env.example smtp.env
chmod 600 smtp.env
# edit smtp.env: SMTP_HOST, SMTP_PORT, SMTP_STARTTLS/SMTP_SSL, SMTP_USER, SMTP_PASS, SMTP_FROM
python scripts/test_smtp.py you@your-domain     # sends a test mail
```

Both files are ignored by Git; keep them readable by your account only.

### Step 4: Download the models (~20 GB, needs internet once)
```bash
make download-models
```
This downloads into `models/` and exports the ONNX embedders:

| Model | Location | Used by |
| :--- | :--- | :--- |
| `sentence-transformers/all-MiniLM-L6-v2` (+ ONNX export) | `models/embeddings/all-MiniLM-L6-v2/` | query/document embeddings, semantic cache |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (+ ONNX) | `models/embeddings/paraphrase-multilingual-MiniLM-L12-v2/` | prompt-injection classifier |
| `Qwen2.5-3B-Instruct` Q4_K_M GGUF (2.0 GB) | `models/slm_gguf/qwen2.5-3b-instruct-q4_k_m.gguf` | CPU tier |
| `Qwen/Qwen2.5-7B-Instruct` (15.2 GB) | `models/llm_7b/qwen2.5-7b-instruct/` | GPU tier |

The complexity router (`gateway/router/models/scorer.onnx`), the injection classifier weights (`pipeline/data/injection_classifier.json`) and the thesis spelling dictionary (`models/spell/thesis_title_terms.txt`) are part of the repository. One model only: `python scripts/download_models.py --embeddings | --slm | --gpu-llm`.

### Step 5: Install Redis Stack (no root needed)
Redis Stack provides the vector index (RediSearch). Download it into the repository root:
```bash
# Ubuntu 22.04 (jammy); on Ubuntu 20.04 replace "jammy" with "focal"
wget https://packages.redis.io/redis-stack/redis-stack-server-7.2.0-v9.jammy.x86_64.tar.gz
tar -xzf redis-stack-server-7.2.0-v9.jammy.x86_64.tar.gz
ls redis-stack-server-7.2.0-v9/bin/redis-stack-server     # must exist; rename the extracted folder to this name if it differs
```
The configuration Redis runs with is generated from `.env` at start-up (`run/redis.conf`, readable by you only): password from `REDIS_PASSWORD`, loopback only, port `REDIS_PORT`, snapshot never rewritten. The `etc/redis-stack.conf` inside the download is not used.

### Step 6: Load the thesis corpus
The pre-built snapshot `dump_final.rdb` (12.67 GB, 2,261,664 chunks with their vector index) is **not stored in Git**. Its download link is shared personally by the project maintainers; ask them for it, download the file and place it in **either** of these folders (found automatically, as `dump_final.rdb` or `dump.rdb`):
```text
Onprem-RAG-gateway/
├── redis_server_data/
│   └── dump_final.rdb                  <-- 12.67 GB snapshot (recommended)
└── redis-stack-server-7.2.0-v9/var/db/redis-stack/
    └── dump.rdb                        <-- or here, Redis Stack's own data folder
```
Anywhere else: set `REDIS_DATA_DIR` (folder) and `REDIS_DBFILENAME` (file name) in `.env`.

*(No snapshot? Build the index yourself, see [§6](#6-building-the-corpus-yourself-ingestion). Without one, `make redis-up` / `make prod-up` refuse to start Redis rather than run with an empty database.)*

Start Redis and wait for the snapshot to load (**35–50 minutes**, depending on disk speed):
```bash
make redis-up          # starts Redis Stack in the background (log: logs/redis.log)
make redis-watch       # live progress bar; exits at 100 %
```
Check the result (the `redis-cli` commands authenticate through `REDISCLI_AUTH`, so the password never appears on the command line):
```bash
export REDISCLI_AUTH="$(grep '^REDIS_PASSWORD=' .env | cut -d= -f2)"
alias redis-cli=./redis-stack-server-7.2.0-v9/bin/redis-cli

redis-cli ping                                          # -> PONG
redis-cli dbsize                                        # -> (integer) 2261664 or more
redis-cli FT._LIST                                      # -> includes rag_documents_idx
redis-cli FT.SEARCH rag_documents_idx "*" LIMIT 0 0     # -> 2261664 (first line of the reply)
```
> [!WARNING]
> Never run `FT.INFO` (it can deadlock this Redis Stack version, see fixes.md DEFECT-21) or `KEYS` (it blocks Redis on 2.26M keys) on this database.

### Step 7: Start the whole stack
```bash
make prod-up           # Redis (if not running) -> vLLM :8000 -> CPU SLM :8082 -> Gateway :8080
```
vLLM needs 2–5 minutes to load the 7B model, so the first status report may show it as `DOWN`. Check again until every tier is up:
```bash
make prod-status       # status of all four services
make vllm-check        # the GPU model server must be loopback-only and require VLLM_API_KEY
curl -s http://localhost:8080/ready    # -> {"status":"ready","checks":{...all true}}
```
Service logs are in `logs/` (`gateway.log`, `slm.log`, `vllm.log`, `redis.log`).

### Step 8: Use it
Open **`http://<server-address>:8080/`** in a browser (on the server itself: `http://localhost:8080/`), enter an e-mail address from `ALLOWED_EMAIL_DOMAINS`, type the code you receive, and ask a question about the theses.

For API clients and admin endpoints, see [§5.4](#54-gateway-port-8080).

### Step 9 (optional): Run the test suite
```bash
make test
```
See [§7](#7-verification--test-suites) for what needs which service.

---

## 4. Daily Operation

```bash
source .venv/bin/activate                 # or: conda activate <your env>

make prod-status                          # status of all tiers
make prod-restart                         # restart Gateway + CPU SLM (vLLM and Redis keep running)
make prod-restart-vllm                    # also restart vLLM (slow model reload; needed after vLLM launch changes)
make prod-down                            # stop Gateway + CPU SLM (keeps Redis data in RAM and vLLM loaded)
make prod-down-vllm                       # also stop vLLM
make redis-down                           # stop Redis (the next start reloads the snapshot: 35-50 min)

make logs                                 # follow the gateway log
make cache-stats                          # semantic cache statistics (needs ADMIN_API_KEY from .env)
make cache-clear                          # empty the semantic cache (the thesis index is untouched)
make queue-stats                          # admission queue telemetry
make otp-clear EMAIL=user@iisc.ac.in      # clear sign-in rate limits for one user (omit EMAIL for all)
```
Every `make` target is a thin wrapper; the underlying command is printed when it runs (e.g. `python scripts/prod_stack.py restart`). `make help` lists all targets.

---

## 5. Running the Services Individually

`make prod-up` starts everything; the commands below start one service at a time (for development or debugging). Load the settings into your shell first:
```bash
set -a && source .env && set +a
```

### 5.1 Redis Stack (port 6379)
```bash
make redis-up        # = python scripts/prod_stack.py redis-start
make redis-status    # loading progress (one-shot)
make redis-down
```

### 5.2 CPU SLM tier (port 8082, Qwen 2.5 3B)
Serves routine factual questions ($P(\text{hard}) < 0.65$) and acts as the zero-downtime fallback: when the GPU tier is down, times out or fails before its first token, hard questions are answered here (responses carry `fallback: "gpu_unavailable"`). If the 3B model refuses on-topic documents while the 7B tier is unavailable, it is asked once more with the next three retrieved passages.
```bash
make serve-slm
# = python -m pipeline.serve_cpu_slm --port 8082 --slots 6 --threads 16
#   (one process, 6 model slots, 16 threads each; busy -> 503 after SLM_QUEUE_TIMEOUT)

# Test (needs SLM_API_KEY from .env):
curl -s http://localhost:8082/v1/chat/completions \
  -H "Content-Type: application/json" -H "Authorization: Bearer $SLM_API_KEY" \
  -d '{"messages":[{"role":"user","content":"What is vermiculite?"}],"max_tokens":64}' | jq .
```

### 5.3 GPU LLM tier (port 8000, vLLM Qwen 2.5 7B)
Serves complex questions ($P(\text{hard}) \ge 0.65$) that need cross-document synthesis.
```bash
make serve-gpu
# = CUDA_VISIBLE_DEVICES=$VLLM_GPU python scripts/serve_vllm.py \
#     --model models/llm_7b/qwen2.5-7b-instruct --served-model-name "Qwen/Qwen2.5-7B-Instruct" \
#     --host 127.0.0.1 --port 8000 --gpu-memory-utilization 0.45 --max-model-len 8192 --dtype bfloat16

# Test:
curl -s http://localhost:8000/v1/models -H "Authorization: Bearer $VLLM_API_KEY" | jq .   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/models                    # 401
```
> **Security note (vLLM 0.6.3):** always start vLLM through `scripts/serve_vllm.py`, never `python -m vllm.entrypoints.openai.api_server`. Stock vLLM 0.6.3 pre-binds its socket with `bind(("", port))`, so `--host 127.0.0.1` is ignored and the model API listens on **all interfaces**. The launcher forces the bind onto `--host`, refuses non-loopback hosts, and refuses to start without `VLLM_API_KEY`, which vLLM then enforces as `Authorization: Bearer <key>` on every `/v1/*` route. Verify after every restart with `make vllm-check`.

### 5.4 Gateway (port 8080)
Serves the AskIISc web chat (`/`), sign-in, and the API endpoints.
```bash
# Development (single worker, live code reload):
make dev
# = uvicorn gateway.main:app --host 0.0.0.0 --port 8080 --reload --no-proxy-headers

# Production (6 workers) is started by `make prod-up`; the equivalent command is:
# uvicorn gateway.main:app --host 0.0.0.0 --port 8080 --workers 6 --no-access-log --no-proxy-headers
#   (--no-proxy-headers: never trust X-Forwarded-For)
```
Endpoints:
```bash
curl -s http://localhost:8080/healthz       # process is up
curl -s http://localhost:8080/ready         # 200 when Redis + thesis index + at least one model tier are up

# Ask from the server itself (loopback is trusted for the question endpoints; PRD §9 L7/L23):
make ask Q="What is vermiculite?"
curl -s -X POST http://localhost:8080/query -H "Content-Type: application/json" \
  -d '{"query": "What is vermiculite?", "max_tokens": 150}' | jq .

make cache-stats   # admin routes need ADMIN_API_KEY from .env, even on this server (sent as X-Admin-Key)
```

Non-public routes (`/query`, `/v1/chat/completions`, `/cache/*`, `/queue/stats`) answer a caller without access exactly like a missing page (404), whatever the method, body or headers (DEFECT-54). A wrong `ADMIN_API_KEY` therefore also gives 404, not 401/403.

Every response carries a Content-Security-Policy and related headers (DEFECT-55): the browser runs only the page's own scripts (`/static/app.js` and the vendored libraries; no inline script), loads no images from other servers, and refuses to show the page inside another site's frame. Keep page logic in `app.js` when editing the UI: an inline `<script>` or `onclick=` would be blocked. Model answers are rendered without `style`, `id` or `class` attributes (DEFECT-57); math keeps KaTeX's styling. Answers never load images, and only links to the gateway or `*.iisc.ac.in` are clickable; other links appear as text (DEFECT-58: instructions planted in a retrieved document can make the 7B model add them).

Accepted limitation: a single `/v1/chat/completions` request with 50 long messages can occupy a gateway worker for about 3 s (PRD §9.1b L25).

### 5.5 Two-Tower complexity router
Classifies questions as easy ($< 0.65$) or hard ($\ge 0.65$) from a 400-dimensional representation (384 semantic + 16 structural features). The gateway runs it in-process; nothing to start.
```bash
make eval            # = python scripts/eval_router_threshold.py   (offline threshold evaluation)
make serve-router    # optional standalone service on port 8090 (the gateway uses it when it is running)
```

---

## 6. Building the Corpus Yourself (Ingestion)

Use this instead of the snapshot (step 6), or to add documents. Redis must be running: start it empty on purpose with `REDIS_ALLOW_EMPTY=1 make redis-up` (it starts in seconds), and the embedding model must be downloaded (step 4). The index `rag_documents_idx` is created automatically on the first ingest.

### Option A: Streaming ingestion from IISc DSpace
Downloads each thesis, extracts layout-aware text, chunks it (512 tokens with 64 overlap), embeds it with `all-MiniLM-L6-v2`, indexes it into RediSearch, and deletes the PDF to save disk space:
```bash
# Smoke test (20 papers, keeps the PDFs in data/raw_pdfs/):
python -m pipeline.crawlers.stream_dspace "https://etd.iisc.ac.in/handle/2005/1" --max 20 --keep-pdfs

# Full ingestion (takes days; run inside tmux):
tmux new -s dspace_crawl
python -m pipeline.crawlers.stream_dspace "https://etd.iisc.ac.in/handle/2005/1" --unlimited --concurrency 3 --delay 1.0
```

### Option B: A folder of PDFs
Put the PDFs in `data/raw_pdfs/`, then:
```bash
make ingest-pdfs
# = python -m pipeline.batch_ingest_pdfs --pdf-dir data/raw_pdfs   (options: --index-name rag_documents_idx --vector-dim 384)
```

For a different corpus, rebuild the thesis spelling dictionary afterwards: `python scripts/build_spell_dictionary.py` (writes `models/spell/thesis_title_terms.txt`).

To keep the result as a snapshot, run `redis-cli SAVE` once (Redis is started with automatic saving off) and copy the resulting file from `redis_server_data/` (named by `REDIS_DBFILENAME`).

---

## 7. Verification & Test Suites

```bash
make test
```
`make test` passes on a fresh clone with nothing running (Python 3.10+). Checks that need live services skip with a reason instead of failing, and run once those services are available:

| Needs | Tests | How to enable |
|---|---|---|
| Nothing | unit, security, routing, gate and route-level tests (fakes for Redis and models) | always run |
| Firefox | chat-page rendering security (`tests/test_frontend_xss.py`, headless) | install `firefox` |
| Redis Stack | exact-match cache, regenerate tokens, purge | step 5–6, `make redis-up` |
| Redis Stack + embedding model | semantic (paraphrase) cache, owner scoping | step 4, `make download-models` |
| Ingested thesis index | live retrieval (topic, rare-term, dynamic floor) | step 6 or §6 |
| Running gateway on :8080 | live `/query` and `/chat` checks | `make prod-up` |

- `make test` never downloads models (`HF_HUB_OFFLINE=1`; override with `make test HF_HUB_OFFLINE=0`).
- Run it through `make`: several files in `tests/` are standalone scripts, so a bare `pytest tests/` is not supported.
- Red-team regression tests that contain the red team's attack data are kept out of the public repository; `make test` runs them where present and otherwise prints that it skipped them. Known limits are recorded as `xfail`; see §9 of the [PRD](product_requirements_document.md).

Other checks (need the running stack):
```bash
make test-intensive     # live MLP routing, CPU SLM TTFT, GPU throughput, cache exact/paraphrase hits, output sanitization
make cache-check        # cache safety: intent guard, owner scoping, exact sharing, regenerate tokens
make injection-eval     # prompt-injection fence (NFR-7) on the frozen suites in tests/data/injection_eval/ (ROUND=round4)
make load-test-cache    # NFR-1: cache-hit load at 100 QPS for 10 min, pass/fail on semantic p99 <= 35 ms
```
`make load-test-cache` sends all its traffic as one synthetic user, which the per-user rate limit stops on a normal gateway (HTTP 429). The PRD's NFR-1 figures were measured against a measurement gateway with production settings and the rate limit lifted ([`scripts/eval_gateway.py`](scripts/eval_gateway.py); `--gateway` points the test at it).
Since DEFECT-51 the gateway runs the injection classifier only on cache misses; the patterns and the English-only check run on every question. The classifier was trained on rounds 1–3; PRD NFR-7 says how round 4 was used.

Resetting sign-in rate limits during testing:
```bash
python scripts/clear_otp_limits.py user@iisc.ac.in    # one user
python scripts/clear_otp_limits.py --all              # everyone (or: make otp-clear)
```

---

## 8. Configuration Reference

All settings live in `.env` (template: [.env.example](.env.example), which documents every option). The most important ones:

```env
# Network and Server
HOST=0.0.0.0
PORT=8080

# Redis Stack
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=<generated in step 3>
REDIS_URL=redis://:<same password>@localhost:6379/0
REDIS_INDEX_NAME=rag_documents_idx
# REDIS_DATA_DIR=redis_server_data     # snapshot folder, relative to the repository root
# REDIS_DBFILENAME=dump_final.rdb
SEMANTIC_CACHE_TTL=14400     # cached answers live 4 hours
REGEN_TOKEN_TTL=900          # "Generate a fresh answer" token lifetime (cached answers only)
REGEN_MAX_PER_HOUR=20        # regenerations per user per hour
REGEN_EVICT_USERS=3          # distinct users regenerating a shared answer before it is evicted

# Model Service Endpoints
SLM_BASE_URL=http://localhost:8082/v1
SLM_MODEL_NAME=models/slm_gguf/qwen2.5-3b-instruct-q4_k_m.gguf
CPU_NUM_THREADS=16
SLM_SLOTS=6
VLLM_BASE_URL=http://localhost:8000/v1
VLLM_MODEL_NAME=Qwen/Qwen2.5-7B-Instruct
VLLM_API_KEY=<generated>   # required by scripts/serve_vllm.py; sent as Bearer by all clients
VLLM_GPU=0                 # GPU index for vLLM (shared machine: pick a GPU with steady usage and ~19 GB free)
GPU_INPROCESS_FALLBACK=0   # keep 0: with vLLM down, hard questions are answered by the CPU tier (FR-2)

# Router & Admission Control
ROUTING_ALPHA=0.65
COMPLEXITY_SCORER_PATH=gateway/router/models/scorer.onnx
GLOBAL_CONCURRENCY_LIMIT=30
PER_USER_CONCURRENCY_LIMIT=6   # in-flight inferences per user across all workers (Redis); HTTP 429 above it
RATE_LIMIT_BURST=10      # per caller and per gateway worker; charged on arrival, cache hits included (DEFECT-47)
RATE_LIMIT_RPS=5
INJECTION_STRIKES_PER_HOUR=5   # injection refusals per identified user per hour before a 15-min block (0 = off)
INJECTION_BLOCK_SECONDS=900
MAX_REQUEST_BODY_BYTES=262144   # larger bodies -> 413 (DEFECT-48)
GATEWAY_ALLOWED_CIDRS=          # optional network gate, e.g. 10.0.0.0/8; empty = off (loopback always allowed)

# Session & Authentication
SESSION_SECRET=<generated>
ADMIN_API_KEY=<generated>  # admin routes (/cache/*, /queue/stats); empty = nobody can administer
SLM_API_KEY=<generated>    # required by the CPU model server on :8082 (DEFECT-45)
COOKIE_SECURE=0            # plain HTTP on the campus network (no TLS); set 1 only behind HTTPS
ENGLISH_ONLY=1             # refuse non-English questions politely (0 = accept all languages)
ALLOWED_EMAIL_DOMAINS=iisc.ac.in
OTP_MAX_PER_HOUR=10                # codes per address per hour from one IP
OTP_MAX_PER_EMAIL_HOUR=30          # codes per address per hour in total (mail-bombing ceiling)
OTP_MAX_FAILS_PER_EMAIL_HOUR=20    # wrong guesses per address per hour (a fresh code's first try always counts)
OTP_MAX_PER_IP_HOUR=1000

# Retrieval Thresholds
RETRIEVAL_MIN_SIMILARITY=0.52
RETRIEVAL_SCORE_MARGIN=0.10
RETRIEVAL_MAX_KNEE_DROP=0.06
RETRIEVAL_EF_RUNTIME=64
```
After changing `.env`, apply it with `make prod-restart` (or `make prod-restart-vllm` for vLLM settings).

---

## 9. Service Port Reference

| Port | Service Component | Host Binding | Access Restriction |
| :---: | :--- | :---: | :--- |
| **`8080`** | **Unified Gateway & AskIISc Web UI** | `0.0.0.0` | **Public/Campus Facing**; Session Auth, CSRF, Token-Bucket Admission |
| **`8082`** | **CPU SLM Daemon (Qwen 2.5 3B)** | `127.0.0.1` | **Internal Loopback Only**, `SLM_API_KEY` required; 1 process, 6 slots $\times$ 16 threads |
| **`8000`** | **GPU LLM Engine (vLLM Qwen 2.5 7B)** | `127.0.0.1` | **Internal Loopback Only**, `VLLM_API_KEY` required; PagedAttention v2 on NVIDIA A100 |
| **`6379`** | **Redis Stack Server (Vector DB & Cache)** | `127.0.0.1` | **Internal Loopback Only**; Protected by `REDIS_PASSWORD` |
| `8090` | Router microservice (optional) | `127.0.0.1` | Not needed: the gateway runs the router in-process |

---

## 10. Troubleshooting Guide

| Issue / Symptom | Root Cause | Exact Remediation |
| :--- | :--- | :--- |
| **`make` uses the wrong Python / `ModuleNotFoundError`** | `make` picks `.venv/bin/python`, else the active conda env, else `python3`. | Activate your environment, or put `PYTHON := <path to python>` in `Makefile.local` (ignored by Git), or pass it on the command line (`make test PYTHON=...`). |
| **`REDIS_PASSWORD is empty`** on `make redis-up` | `.env` not created or secrets not generated. | Run step 3a. |
| **`No thesis snapshot found`** on `make redis-up` / `make prod-up` | No `dump_final.rdb` / `dump.rdb` in `redis_server_data/`, in `redis-stack-server-7.2.0-v9/var/db/redis-stack/`, or at `REDIS_DATA_DIR` / `REDIS_DBFILENAME`. | Put the snapshot in one of those folders (step 6). To start empty on purpose: `REDIS_ALLOW_EMPTY=1 make redis-up`. |
| **`/ready` says `redis_thesis_index: false` although Redis is up** | Redis started without loading the snapshot (an empty database), or is still loading it. | `make redis-status`: `loading:1` means wait. If Redis came up within seconds, it did not load the snapshot: `make redis-down`, then `make redis-up` and check that it prints `loading <snapshot path>`. |
| **`NOAUTH Authentication required`** | `redis-cli` or a script started without the password. | `export REDISCLI_AUTH="$(grep '^REDIS_PASSWORD=' .env \| cut -d= -f2)"`. `REDIS_URL` in `.env` must be `redis://:<REDIS_PASSWORD>@localhost:6379/0`. |
| **`LOADING Redis is loading the dataset in memory`** | Redis is reading `dump_final.rdb` into RAM. | **Do not stop Redis.** `make redis-watch` shows progress and ETA. Wait for `loading:0`. |
| **`unknown command 'FT.SEARCH'`** | Plain Redis is running on 6379 instead of Redis Stack. | Stop it, then `make redis-up`. |
| **Is the gateway able to answer?** | `/health` (and `/healthz`) only say the process is up. | Use `/ready`: HTTP 200 when Redis with the thesis index and at least one model tier are up, otherwise 503; `curl -s localhost:8080/ready` on the server shows which check failed. |
| **HTTP 503 "The thesis search service is temporarily unavailable"** | Redis is down, still loading its snapshot, or the `rag_documents_idx` index is missing (the cause and stack trace are in `logs/gateway.log`). | Start Redis (`make redis-up`) and wait until `redis-cli ping` returns `PONG` (a cold start loads for up to ~1 h); check the index with `FT._LIST`. The gateway reconnects on its own. |
| **Zero chunks retrieved / out-of-domain refusal** | Similarity below the 0.52 floor, or the index is empty. | Check that `FT.SEARCH rag_documents_idx "*" LIMIT 0 0` returns 2261664 (never `FT.INFO`: it can deadlock Redis, DEFECT-21). Check that step 4 downloaded `all-MiniLM-L6-v2`. |
| **Gateway returns 429 Too Many Requests** | Client token bucket exhausted (10 burst, 5/s per caller per worker; every question counts, cached ones too), or more than `PER_USER_CONCURRENCY_LIMIT` answers in flight. | Wait a few seconds, or raise `RATE_LIMIT_BURST` / `RATE_LIMIT_RPS` in `.env`. |
| **429 "Too many of your recent requests were refused as prompt-injection attempts"** | The user had `INJECTION_STRIKES_PER_HOUR` (5) injection refusals within an hour (DEFECT-59). | Wait for `Retry-After` (15 min by default). A false positive streak is unlikely (0/453 genuine questions refused in NFR-7); to lift a block early: `DEL admission:blocked:<sha256(user)[:24]>` in Redis. |
| **OTP e-mail not received** | SMTP settings wrong, or a sign-in rate limit was hit. | `python scripts/test_smtp.py <your_email>` to check `smtp.env`; `python scripts/clear_otp_limits.py <your_email>` to clear limits. |
| **Sign-in refused for an address** | Its domain is not in `ALLOWED_EMAIL_DOMAINS`. | Add the domain in `.env`, then `make prod-restart`. |
| **vLLM CUDA Out-of-Memory** | GPU VRAM over-allocation. | Lower `--max-model-len` or change `--gpu-memory-utilization` in `scripts/prod_stack.py` / `Makefile`, or pick another GPU with `VLLM_GPU`. |
| **vLLM: `No available memory for the cache blocks`** | vLLM 0.6.3 counts other users' memory changes on a shared GPU during start-up profiling. | Choose a GPU with steady usage and ~19 GB free (`nvidia-smi`), set `VLLM_GPU` in `.env`, then `make prod-restart-vllm`. |
| **vLLM: `Address already in use` on restart** | Previous server's connections on port 8000 still closing. | Already handled by `scripts/serve_vllm.py` (`SO_REUSEADDR`); if it persists, check `ss -ltnp \| grep :8000` for a stray process. |
| **`make vllm-check` fails / 🚨 warning in `make prod-status`** | vLLM was started without `scripts/serve_vllm.py`, so it is bound to `0.0.0.0` without an API key. | `make prod-restart-vllm`, then `make vllm-check`. |
| **CPU tier fails, `:8082` returns 401** | `SLM_API_KEY` missing or different between the CPU server and the gateway. | Set `SLM_API_KEY` in `.env` and restart both (`make prod-restart`). |
| **Cached answer not reused for a rephrased question** | Near-duplicate hits are per user (another user's entry is never reused; only exact repeats are shared), or the intent guard rejected the match (different question type, opposite terms, quantifiers, swapped roles), or similarity < 0.90. | Expected behaviour; the gateway logs `rejected by entity/intent guard: <reason>`. See PRD §9.1. |
| **CSRF Error: 403 Forbidden** | Missing or mismatched CSRF cookie/header. Required on every browser POST, also before sign-in (DEFECT-48). | Ensure client passes `X-CSRF-Token` header matching the `csrftoken` cookie issued by `GET /me` or `GET /`. API clients using `Authorization: Bearer` are exempt. |
| **400 "AskIISc answers questions in English only"** | The question was judged not to be English (`pipeline/language_gate.py`). | Ask in English; set `ENGLISH_ONLY=0` to accept all languages. Questions of fewer than 4 words always pass. |
| **413 Request body too large** | Body over `MAX_REQUEST_BODY_BYTES` (256 KiB). | Questions are short; raise the limit only if a legitimate client needs it. |

---

## 11. Quick Start Cheat Sheet

```bash
# 1. Clone
git clone https://github.com/rutujmanware/Onprem-RAG-gateway.git && cd Onprem-RAG-gateway

# 2. Python environment
make setup && source .venv/bin/activate && make install-serving

# 3. Configuration (then check ALLOWED_EMAIL_DOMAINS, VLLM_GPU in .env; fill in smtp.env)
cp .env.example .env && chmod 600 .env
for k in REDIS_PASSWORD SESSION_SECRET ADMIN_API_KEY SLM_API_KEY VLLM_API_KEY; do
  sed -i "s|^$k=.*|$k=$(python3 -c 'import secrets; print(secrets.token_hex(32))')|" .env
done
sed -i "s|^REDIS_URL=.*|REDIS_URL=redis://:$(grep '^REDIS_PASSWORD=' .env | cut -d= -f2)@localhost:6379/0|" .env
cp smtp.env.example smtp.env && chmod 600 smtp.env

# 4. Models (~20 GB)
make download-models

# 5. Redis Stack
wget https://packages.redis.io/redis-stack/redis-stack-server-7.2.0-v9.jammy.x86_64.tar.gz
tar -xzf redis-stack-server-7.2.0-v9.jammy.x86_64.tar.gz

# 6. Corpus: put dump_final.rdb into redis_server_data/, then (35-50 min)
make redis-up && make redis-watch

# 7. Whole stack
make prod-up
make prod-status && make vllm-check
curl -s http://localhost:8080/ready

# 8. Browser: http://<server-address>:8080/
```
