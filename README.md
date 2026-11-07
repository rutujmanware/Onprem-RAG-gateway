# On-Premise Institutional RAG Gateway

A zero-egress Retrieval-Augmented Generation (RAG) system for IISc. Theses and papers are scraped from the institute's DSpace repository and stored as vectors in **Redis Stack**. A **complexity router** sends each question either to a small model on **CPU** (Qwen 2.5 3B) or to a larger **GPU** model served by **vLLM**. Admission control, rate limiting and a two-level cache sit in front of both. Users reach it through the **AskIISc web app** and sign in with a one-time code (OTP) emailed to their `@iisc.ac.in` address.

📖 More detail: [PROJECT_PROGRESS_AND_ARCHITECTURE.md](PROJECT_PROGRESS_AND_ARCHITECTURE.md) (design, benchmarks) · [OPTIMIZATION_GUIDE.md](OPTIMIZATION_GUIDE.md) (CPU SLM tuning)

**Contents:** [Architecture](#1-architecture) · [Prerequisites](#2-prerequisites) · [Setup](#3-setup) · [Redis](#4-redis-stack) · [Build the Index](#5-build-the-vector-index) · [CPU SLM](#6-cpu-slm-tier-qwen-25-3b) · [GPU LLM](#7-gpu-llm-tier-vllm) · [Router](#8-complexity-router) · [Gateway](#9-gateway) · [Web App & OTP](#10-web-app--email-otp-sign-in) · [Tests](#11-tests--evaluation) · [Docker](#12-docker-compose) · [Reference](#13-reference) · [Troubleshooting](#14-troubleshooting) · [Structure](#15-project-structure) · [Quick Start](#16-quick-start)

---

## 1. Architecture

```
 Browser ─► AskIISc Web App :9000 ── network gate · email OTP sign-in · session · chat UI
                  │ POST /query (SSE)
                  ▼
            Gateway :8080 ── admission control · L0 exact + L1 semantic cache · MLP complexity router
              easy (≤ α) │                         │ hard (> α)
                         ▼                         ▼
            CPU SLM :8082 (Qwen2.5-3B)      GPU LLM :8000 (vLLM, 7B)
                         └──────────┬──────────────┘
                                    ▼
            Redis Stack :6379 ── index rag_documents_idx · HNSW · COSINE · 384-d
                                    ▲
 Ingestion: DSpace crawler ─► PyMuPDF ─► cleaner ─► chunker (512/64) ─► all-MiniLM-L6-v2 ─► Redis
```

**Request flow:**
1. The user opens the web app and signs in with an emailed 6-digit code.
2. The web app forwards each question to the gateway's `POST /query`.
3. The gateway applies rate and concurrency limits and checks its caches.
4. The router scores the question with a trained MLP and sends it to the CPU or the GPU tier. If the GPU tier is busy, the question falls back to the CPU.
5. The chosen model answers from the top-k chunks retrieved from Redis, and the answer streams back to the browser with its sources.

**Reference corpus:** about 12,900 IISc theses → **2.26M chunks**. The snapshot is about 12 GB on disk and **about 43 GB in RAM**, roughly 19 KB of RAM per chunk.

---

## 2. Prerequisites

**Software:**
- Linux (Ubuntu 22.04 tested; macOS works for CPU-only development)
- **Python 3.10–3.12** and Git
- Optional:
  - Docker + Compose v2, to run Redis
  - NVIDIA driver + CUDA 12.x + nvidia-container-toolkit, for the GPU tier
  - `tmux`, for long scraping runs
  - `jq`, to pretty-print JSON output

| Setup | CPU | RAM | GPU |
|---|---|---|---|
| Minimal dev (few hundred docs) | 8+ cores | 16 GB | none |
| Full corpus | 32+ cores | **64 GB+** | none for the CPU tier |
| GPU tier (7B) | — | — | 1× ≥ 24 GB |
| Router dataset generation | — | ~150 GB `/dev/shm` | 4× ≥ 32 GB free |
| Reference deployment | 2× EPYC 7713 | 504 GB | 8× A100 40 GB |

---

## 3. Setup

```bash
git clone https://github.com/rutujmanware/Onprem-RAG-gateway.git && cd Onprem-RAG-gateway
make setup && source .venv/bin/activate        # creates .venv + installs requirements; activate in every terminal
pip install accelerate "huggingface_hub[cli]" pandas scikit-learn
# CPU-only machine: install the CPU torch wheel first:  pip install torch --index-url https://download.pytorch.org/whl/cpu
# GPU machine (GPU tier, dataset generation):          pip install vllm
cp .env.example .env && chmod 600 .env
```

The key `.env` values for the backend services are below. Sign-in settings go in the web app's own `app.env` (§10), so the `OIDC_*` entries in `.env.example` can stay at their defaults.

```env
REDIS_URL=redis://:<YOUR_REDIS_PASSWORD>@localhost:6379/0    # no password: redis://localhost:6379/0
REDIS_PASSWORD=<YOUR_REDIS_PASSWORD>
REDIS_INDEX_NAME=rag_documents_idx
SLM_BASE_URL=http://localhost:8082/v1
SLM_MODEL_NAME=Qwen/Qwen2.5-3B-Instruct       # or a local path, e.g. models/slm/qwen2.5-3b-instruct
CPU_NUM_THREADS=16                            # your PHYSICAL core count
VLLM_BASE_URL=http://localhost:8000/v1
VLLM_MODEL_NAME=meta-llama/Llama-2-7b-chat-hf
ROUTING_ALPHA=0.65
GLOBAL_CONCURRENCY_LIMIT=30
PER_USER_CONCURRENCY_LIMIT=4
RATE_LIMIT_RPS=5
RATE_LIMIT_BURST=10
# INTRA_COOKIES=JSESSIONID=...                # only if DSpace content needs a login
```

Load `.env` into your shell before running the crawler, ingest or dataset scripts: `set -a && source .env && set +a`

---

## 4. Redis Stack

The code uses RediSearch (`FT.*` commands), so you need **Redis Stack**, not plain Redis. Pick one option.

**A. Docker.** This also gives you the RedisInsight UI at <http://localhost:8001>. `make up`, `make down` and `make logs` manage the same container without a password.
```bash
docker run -d --name onprem-rag-redis -p 6379:6379 -p 8001:8001 -v redis_data:/data \
  -e REDIS_ARGS="--requirepass <YOUR_REDIS_PASSWORD> --save 60 1 --appendonly yes" redis/redis-stack:7.2.0-v9
```

**B. User-space binary.** No sudo or Docker needed. Downloads for other OS builds are at <https://redis.io/downloads/>.
```bash
wget https://packages.redis.io/redis-stack/redis-stack-server-7.2.0-v9.jammy.x86_64.tar.gz
tar -xzf redis-stack-server-7.2.0-v9.jammy.x86_64.tar.gz && mv redis-stack-server-7.2.0-v9* redis-stack-server-7.2.0-v9
mkdir -p redis-stack-server-7.2.0-v9/var/db/redis-stack
./redis-stack-server-7.2.0-v9/bin/redis-stack-server --dir ./redis-stack-server-7.2.0-v9/var/db/redis-stack \
    --requirepass "<YOUR_REDIS_PASSWORD>" --daemonize yes
export REDISCLI_AUTH='<YOUR_REDIS_PASSWORD>' && redis-cli ping        # → PONG
```

**C. Restore a pre-built snapshot (skip §5).** Snapshots are git-ignored. With Redis stopped, copy the snapshot to `redis-stack-server-7.2.0-v9/var/db/redis-stack/dump.rdb`. For Docker, copy it to `/data/dump.rdb` and remove `/data/appendonlydir` first. Then start Redis and wait for `loading:0`:
```bash
watch -n 1 "redis-cli info Persistence | grep -E 'loading:|loading_loaded_perc'"
```

Stop Redis and free its RAM with `redis-cli shutdown nosave`, or `docker stop onprem-rag-redis`.

---

## 5. Build the Vector Index

The index `rag_documents_idx` is created automatically on the first ingest. Download the embedding model **first**:
```bash
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```
> ⚠️ If the model can't load, the embedder falls back to hash-based vectors. Ingest still completes, but retrieval is meaningless.

**Option 1: stream from IISc DSpace.** The crawler downloads each PDF, then extracts, chunks and indexes it, then deletes the PDF. It is resumable: papers already ingested are skipped. Run it inside `tmux`.
```bash
python -m pipeline.crawlers.stream_dspace "https://etd.iisc.ac.in/handle/2005/1" --max 10 --keep-pdfs   # smoke test
python -m pipeline.crawlers.stream_dspace "https://etd.iisc.ac.in/handle/2005/1" --unlimited --concurrency 3 --delay 1.0
```

| Flag | Default | Meaning |
|---|---|---|
| `--max N` / `--unlimited` | all | Stop after N papers / crawl everything |
| `--concurrency` | 1 | Parallel workers (keep ≤ 5 to avoid running out of RAM or an IP ban) |
| `--delay` | 1.0 | Seconds between requests |
| `--keep-pdfs` | off | Keep PDFs after ingest |
| `--output-dir` | `data/raw_pdfs` | Where PDFs and metadata JSON go |
| `--cookies`, `--cookies-file`, `--auth` | — | For intranet-only content |

**Option 2: ingest your own PDFs.** Put them in `data/raw_pdfs/`, then run `make ingest-pdfs`.

**Check retrieval:**
```bash
redis-cli dbsize                                          # chunks stored
make query Q="deep learning for semantic segmentation"    # = python -m pipeline.retriever "..." -k 5
```

---

## 6. CPU SLM Tier (Qwen 2.5 3B)

```bash
huggingface-cli download Qwen/Qwen2.5-3B-Instruct --local-dir models/slm/qwen2.5-3b-instruct   # ~6 GB
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1      # zero-egress once downloaded

make rag-answer Q="What is vermiculite and what are its primary uses?"     # end-to-end RAG, streamed
python -m pipeline.rag_chain "..." -k 3 --temp 0.2 --max-tokens 160 [--no-stream | --json]

make serve-slm        # OpenAI-compatible API on :8082  (= python -m pipeline.serve_cpu_slm --port 8082 --threads 16)
curl -s http://localhost:8082/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"What is RAG?"}],"max_tokens":128}' | jq -r '.choices[0].message.content'
```
Reference speed: about **21 s end-to-end** for a 160-token answer at 48 threads ([OPTIMIZATION_GUIDE.md](OPTIMIZATION_GUIDE.md)). Low on disk? Download into `/dev/shm` and symlink it into `models/`.

---

## 7. GPU LLM Tier (vLLM)

This tier serves "hard" queries. Without a GPU, every query goes to the CPU tier.
```bash
huggingface-cli download meta-llama/Llama-2-7b-chat-hf --local-dir models/llm_7b     # gated: accept the licence on HF
python -m vllm.entrypoints.openai.api_server --model models/llm_7b --served-model-name meta-llama/Llama-2-7b-chat-hf \
    --host 0.0.0.0 --port 8000 --max-model-len 4096 --gpu-memory-utilization 0.90
```
Tuning options are in [configs/vllm_config.yaml](configs/vllm_config.yaml).

---

## 8. Complexity Router

**1. Generate the dataset.** This needs Redis with data and 4 GPUs with ≥ 32 GB free each. `dataset_generator.py` asks Qwen2.5-72B (vLLM) for "easy" single-document questions and "hard" multi-chunk questions about sampled chunks. Edit the partition and conda paths in `run_slurm.sh`, then run `sbatch run_slurm.sh`. Off SLURM, run `python dataset_generator.py` instead. Output: `router_multichunk_training_dataset.{csv,jsonl}`.

**2. Train the MLP.** The model is MiniLM embedding (384) → 128 → 64 → 1, with BCE loss and class-balanced sampling.
```bash
mkdir -p gateway/router/mlp_router/data && cp router_multichunk_training_dataset.csv gateway/router/mlp_router/data/
cd gateway/router/mlp_router && python train.py && python inference.py && cd -     # → best_router_model.pth
```

**3. Tune α.** Run `python -m evaluation.benchmarks.alpha_sweep`, then set `ROUTING_ALPHA` in `.env` and `routing.alpha` in `configs/gateway_config.yaml`. A lower α sends more queries to the GPU.

---

## 9. Gateway

Start Redis, the CPU SLM and (optionally) vLLM first. Keep the gateway internal (`127.0.0.1`): users go through the web app.
```bash
make dev                                   # uvicorn gateway.main:app --port 8080 --reload   (API docs: /docs)
curl -s http://localhost:8080/healthz
curl -s -X POST http://localhost:8080/query -H "Content-Type: application/json" \
  -d '{"query": "What is vermiculite?", "max_tokens": 300}' | jq -r .answer        # add "stream": true for SSE
```
The gateway also offers an OpenAI-compatible `POST /v1/chat/completions`. Settings live in [configs/gateway_config.yaml](configs/gateway_config.yaml):

| Section | Settings |
|---|---|
| `routing` | α 0.65, CPU fallback when the GPU is saturated |
| `admission` | 30 global / 4 per-user concurrent requests, queue depth 100 |
| `rate_limiting` | Token bucket refilling at 5 requests/s, burst 10 (over the limit → HTTP 429) |
| `cache` | L0 exact cache (TTL 1 h), L1 semantic cache (similarity ≥ 0.88, TTL 2 h) |

---

## 10. Web App & Email OTP Sign-in

The web app in `askiisc_chat/` serves the login page and the chat UI. `./app.sh` starts and stops it.

**Sign-in flow:**
1. The user enters an `@iisc.ac.in` address and clicks **Generate OTP**.
2. A 6-digit code is emailed to that address.
3. Entering the code starts an 8-hour session.

Without a session, the chat returns 401. Clients outside `ALLOWED_CIDRS` get 403 on every page.

**Built-in protections:**
- Codes are stored only as HMAC hashes, valid for 10 minutes, and single-use.
- A code is destroyed after 5 wrong tries.
- Requests are limited to one code every 60 s, 5 codes per hour per address and 20 per hour per IP.
- Mail is sent only to allowed domains.

```bash
cd askiisc_chat && pip install -r requirements.txt
cp app.env.example app.env && chmod 600 app.env
sed -i "s|^SESSION_SECRET=.*|SESSION_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')|" app.env
```

Key `app.env` settings. Comments must stay on their own lines.
```env
BIND_HOST=127.0.0.1
BIND_PORT=9000
# client IP ranges allowed to open the page
ALLOWED_CIDRS=127.0.0.0/8
AUTH_MODE=otp
REQUIRE_LOGIN=1
ALLOWED_EMAIL_DOMAINS=iisc.ac.in
# 1 = also accept dept addresses like @csa.iisc.ac.in
ALLOW_SUBDOMAINS=0
# set 1 when served over https
COOKIE_SECURE=0
BACKEND=gateway
GATEWAY_URL=http://127.0.0.1:8080
MAX_TOKENS=300

# --- Email sender: Gmail for testing ---
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_STARTTLS=1
SMTP_USER=<sender>@gmail.com
SMTP_PASS=<16-char Gmail App Password>
SMTP_FROM=<sender>@gmail.com
OTP_DEV_LOG=0
```

- **Gmail (testing):** turn on 2-Step Verification for the sender account, then create an **App Password** (Google Account → Security → App passwords). The normal Gmail password won't work. Gmail is an external service with daily send limits, so use it for testing only.
- **Production:** use the IISc internal relay, e.g. `SMTP_HOST=<relay>`, `SMTP_PORT=25`, `SMTP_FROM=askiisc-noreply@iisc.ac.in`, with `SMTP_USER` and `SMTP_PASS` empty.
- **No mail server (local development only):** leave `SMTP_HOST` empty and set `OTP_DEV_LOG=1`. The code goes to `logs/app.log`, and `./app.sh code` shows it.

```bash
./app.sh start | stop | restart | status | logs | code      # PY=../.venv/bin/python ./app.sh start  to pick the interpreter
```
Open <http://127.0.0.1:9000> and sign in. Add `?debug=1` to the URL to see routing details.

**To open it to campus:**
- Set `BIND_HOST` to the server's intranet IP and add the campus ranges to `ALLOWED_CIDRS`.
- Serve it over HTTPS behind nginx, with `COOKIE_SECURE=1` and `TRUSTED_PROXIES=<nginx IP>`.
- Run a single process, because OTP state is held in memory.

---

## 11. Tests & Evaluation

```bash
make test                     # prompt-injection fence, rate limiter, L0 cache   (or: PYTHONPATH=. pytest -q tests/)
make eval                     # alpha sweep + RAGAS (faithfulness, context recall)
```
- Other evaluation tools: `evaluation/benchmarks/load_test.py` (Locust load test) and `evaluation/judge/local_judge.py` (on-prem LLM judge).
- Datasets: `evaluation/datasets/`.

---

## 12. Docker Compose

[docker-compose.yml](docker-compose.yml) runs `redis`, `cpu-slm` (llama.cpp), `vllm` and `gateway` on the network `rag-internal`.
- Inside the network, set `.env` to use service names: `REDIS_URL=redis://redis:6379/0`, `SLM_BASE_URL=http://cpu-slm:8080/v1`, `VLLM_BASE_URL=http://vllm:8000/v1`.
- The CPU SLM needs a GGUF file at `models/slm_gguf/model.gguf`, e.g. from `Qwen/Qwen2.5-3B-Instruct-GGUF`, symlinked to that name.
- The vLLM service needs the 7B weights in `models/llm_7b/`, started with `command: ["--model", "/models/llm_7b", "--max-model-len", "4096"]`.

```bash
docker compose up -d --build                 # full stack   (CPU-only: docker compose up -d redis cpu-slm gateway)
docker compose logs -f gateway
docker compose down
```

---

## 13. Reference

| Port | Service | Start with |
|---|---|---|
| 9000 | Web app (login + chat) | `./app.sh start` |
| 8080 | Gateway | `make dev` |
| 8082 | CPU SLM | `make serve-slm` |
| 8000 | GPU LLM (vLLM) | `python -m vllm.entrypoints.openai.api_server …` |
| 6379 / 8001 | Redis Stack / RedisInsight | Docker or `redis-stack-server` |

**Make targets:**
- `setup`, `dev`, `serve-slm`
- `up`, `down`, `logs` (Redis in Docker)
- `redis-up`, `redis-down`, `redis-status` (Redis binary)
- `ingest-pdfs`, `query Q=…`, `rag-answer Q=…`
- `test`, `eval`, `clean`, `help`

**Remote server:** reach the services through an SSH tunnel.
```bash
ssh -L 9000:localhost:9000 -L 8080:localhost:8080 -L 8001:localhost:8001 user@server
```

**Redis checks:**
```bash
redis-cli dbsize                                    # number of chunks
redis-cli info memory | grep used_memory_human      # RAM in use
redis-cli FT.INFO rag_documents_idx | head          # index details
```

---

## 14. Troubleshooting

| Symptom | Fix |
|---|---|
| `NOAUTH` / `unknown command 'FT.SEARCH'` | Add the password to `REDIS_URL` / `REDISCLI_AUTH`; make sure you're running **Redis Stack**, not plain Redis |
| `LOADING …` or Redis killed while loading a snapshot | Wait for `loading:0`; the full corpus needs about 43 GB of free RAM |
| Irrelevant results / `No matching chunks` | Embedding model didn't load (§5); re-ingest. Or check `REDIS_INDEX_NAME` |
| SLM very slow | Set `CPU_NUM_THREADS` / `--threads` to your physical core count; keep max tokens around 160 |
| `requires accelerate` / script ignores `.env` | `pip install accelerate` / run `set -a && source .env && set +a` |
| Gateway 429 / can't reach a model | Raise the limits in `gateway_config.yaml` / check `SLM_BASE_URL` and `VLLM_BASE_URL` |
| vLLM CUDA OOM on multi-GPU | `export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True VLLM_CUSTOM_ALL_REDUCE=0` |
| Web app 403 "restricted to the IISc network" | Add your IP range to `ALLOWED_CIDRS`; behind a proxy, set `TRUSTED_PROXIES` |
| `./app.sh start` refuses to start | `app.env` is missing or `SESSION_SECRET` is still `CHANGE_ME` |
| OTP email never arrives | `./app.sh logs` → `otp mail failed`. Gmail `SMTPAuthenticationError` means you need an App Password. Also check spam |
| "Demo mode: code is in the server log" | Set the `SMTP_*` values and `OTP_DEV_LOG=0` |
| "email domain not allowed" | Add the domain to `ALLOWED_EMAIL_DOMAINS`, or set `ALLOW_SUBDOMAINS=1` |
| Logged out after a restart / login loops | `SESSION_SECRET` changed, or `COOKIE_SECURE=1` while serving over http |
| Scraper blocked (HTTP 429) | Lower `--concurrency`, raise `--delay`; it resumes where it stopped |

---

## 15. Project Structure

```
Onprem-RAG-gateway/
├── askiisc_chat/          # web app: app.sh, app.env.example, askiisc/ (app, auth/OTP, config, gateway bridge), static/, tests/
├── configs/               # gateway_config.yaml, slm_config.yaml, vllm_config.yaml
├── gateway/               # FastAPI gateway: main.py, auth/, cache/ (L0/L1), queue/ (admission), router/ (+ mlp_router/), clients/
├── pipeline/              # crawlers/, text_processing/, embeddings/, indexer, batch_ingest_pdfs, retriever,
│                          # query_vector_db, slm_runner, rag_chain, serve_cpu_slm
├── evaluation/            # benchmarks/, judge/, datasets/
├── models/                # local weights (git-ignored)
├── tests/                 # unit and security tests
├── dataset_generator.py   # router dataset (Qwen2.5-72B via vLLM)
├── run_slurm.sh           # SLURM job for dataset generation
├── docker-compose.yml · Dockerfile · Makefile · requirements.txt · .env.example
└── PROJECT_PROGRESS_AND_ARCHITECTURE.md · OPTIMIZATION_GUIDE.md
```
**Not in Git:**
- secrets: `.env`, `askiisc_chat/app.env`
- data: `data/`, `*.rdb` snapshots, router datasets, model weights
- `redis-stack-server-*/`

---

## 16. Quick Start

```bash
git clone https://github.com/rutujmanware/Onprem-RAG-gateway.git && cd Onprem-RAG-gateway
make setup && source .venv/bin/activate && pip install accelerate "huggingface_hub[cli]"
cp .env.example .env                                   # set REDIS_URL, SLM_MODEL_NAME, CPU_NUM_THREADS
docker run -d --name onprem-rag-redis -p 6379:6379 -p 8001:8001 redis/redis-stack:7.2.0-v9
set -a && source .env && set +a
python -m pipeline.crawlers.stream_dspace "https://etd.iisc.ac.in/handle/2005/1" --max 20
make rag-answer Q="What methods are used to measure thermal conductivity of nanomaterials?"
make serve-slm & make dev &                            # CPU SLM :8082, gateway :8080
cd askiisc_chat && pip install -r requirements.txt && cp app.env.example app.env   # set SESSION_SECRET + SMTP_* (§10)
./app.sh start                                         # http://127.0.0.1:9000 → sign in with your @iisc.ac.in email
```
