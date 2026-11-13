# 🏛️ On-Premise Institutional RAG Gateway (AskIISc)

A 100% bare-metal, zero-egress Retrieval-Augmented Generation (RAG) platform and API Gateway built for the Indian Institute of Science (IISc). The entire corpus of institutional theses and research papers (**2,261,664 chunks**) is indexed in **Redis Stack** RAM. An ONNX Two-Tower **complexity router** dynamically dispatches queries to either an ultra-fast **CPU Small Language Model** (Qwen 2.5 3B across 6 workers $\times$ 16 threads) or a high-capacity **GPU Large Language Model** (Qwen 2.5 7B served via **vLLM** on an NVIDIA A100), with automatic fallback to CPU on GPU saturation.

The platform runs behind a single-port secure perimeter (**Port 8080**) featuring email OTP authentication, CSRF protection, token-bucket admission control, microsecond-tier typo normalization, and a two-level semantic cache.

📖 **Engineering Manuals:** [complete_system_documentation.md](complete_system_documentation.md) (Complete Architecture) · [product_requirements_document.md](product_requirements_document.md) (PRD & Requirements) · [OPTIMIZATION_GUIDE.md](OPTIMIZATION_GUIDE.md) (CPU SLM Tuning)

---

## 📑 Table of Contents
1. [Architecture & Topology](#1-architecture--topology)
2. [Prerequisites & System Sizing](#2-prerequisites--system-sizing)
3. [Environment Configuration](#3-environment-configuration)
4. [Redis Stack: Native User-Space Setup](#4-redis-stack-native-user-space-setup)
5. [Starting from Pre-Built RDB Snapshot](#5-starting-from-pre-built-rdb-snapshot-226m-chunks)
6. [Data Ingestion: DSpace Crawlers & Batch PDF Indexing](#6-data-ingestion-dspace-crawlers--batch-pdf-indexing)
7. [CPU SLM Tier (Port 8082 - Qwen 2.5 3B)](#7-cpu-slm-tier-port-8082---qwen-25-3b)
8. [GPU LLM Tier (Port 8000 - vLLM Qwen 2.5 7B)](#8-gpu-llm-tier-port-8000---vllm-qwen-25-7b)
9. [Two-Tower Neural Complexity Router](#9-two-tower-neural-complexity-router)
10. [Unified Gateway & Web Chat UI (Port 8080)](#10-unified-gateway--web-chat-ui-port-8080)
11. [One-Shot Production Orchestration](#11-one-shot-production-orchestration)
12. [Verification & Test Suites](#12-verification--test-suites)
13. [Service Port Reference](#13-service-port-reference)
14. [Troubleshooting Guide](#14-troubleshooting-guide)
15. [Quick Start Cheat Sheet](#15-quick-start-cheat-sheet)

---

## 1. Architecture & Topology

> [!IMPORTANT]
> **Zero Docker Guarantee:** This system runs **100% bare-metal on the host OS** using native user-space binaries and systemd-compatible processes. No Docker, no container runtimes, and no external cloud API dependencies are required.

```
 Browser / Client ─► AskIISc Unified Gateway & Web UI :8080
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
      Email OTP Session Auth    Prompt Injection Fence [NFR-7]
               │                         │
               └────────────┬────────────┘
                            ▼
               Typo Normalizer (3.95 µs)
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
 (6 workers x 16 threads)          (NVIDIA A100 80GB, PagedAttention)
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
- **OS:** Linux (Ubuntu 20.04 / 22.04 LTS tested; bare-metal host).
- **Python:** Python 3.10 to 3.12 (Miniconda / virtualenv recommended).
- **NVIDIA Driver & CUDA 12.x:** Required for GPU tier (vLLM); optional if running CPU-only.
- **Git & Build Essentials:** `gcc`, `g++`, `make`, `cmake`.

### Hardware Sizing

| Component | Minimum Dev Spec | Recommended Production Spec |
| :--- | :--- | :--- |
| **Host RAM** | **64 GB RAM** *(Redis requires ~44 GB)* | **128 GB – 256 GB RAM** |
| **CPU** | 8+ cores (x86_64 AVX2) | 32 to 64 cores with **AVX-512** (AMD EPYC / Intel Xeon) |
| **GPU** | None *(runs 100% CPU SLM fallback)* | **1× NVIDIA A100 (40GB or 80GB)** or **RTX A5000 (24GB)** |
| **Disk Storage** | 50 GB SSD | **250 GB+ NVMe PCIe Gen4 SSD** |

---

## 3. Environment Configuration

### Step 1: Clone and Set Up Python Environment
```bash
git clone https://github.com/rutujmanware/Onprem-RAG-gateway.git
cd Onprem-RAG-gateway

# Using Make:
make setup
source .venv/bin/activate

# Or native venv:
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install accelerate "huggingface_hub[cli]" pandas scikit-learn
```

### Step 2: Configure Environment Variables
Copy `.env.example` to `.env` and set secure file permissions:
```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your editor. Key settings (passwords must be set securely):
```env
# Network and Server
HOST=0.0.0.0
PORT=8080
ENVIRONMENT=production
LOG_LEVEL=INFO

# Redis Stack Configuration
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=<YOUR_REDIS_PASSWORD>
REDIS_URL=redis://:<YOUR_REDIS_PASSWORD>@localhost:6379/0
REDIS_INDEX_NAME=rag_documents_idx
SEMANTIC_CACHE_TTL=86400

# Model Service Endpoints
SLM_BASE_URL=http://localhost:8082/v1
SLM_MODEL_NAME=models/slm_gguf/qwen2.5-3b-instruct-q4_k_m.gguf
CPU_NUM_THREADS=16
CPU_BATCH_THREADS=32

VLLM_BASE_URL=http://localhost:8000/v1
VLLM_MODEL_NAME=Qwen/Qwen2.5-7B-Instruct
GPU_DEVICE=0

# Router & Admission Control
ROUTING_ALPHA=0.65
COMPLEXITY_SCORER_PATH=gateway/router/models/scorer.onnx
GLOBAL_CONCURRENCY_LIMIT=30
PER_USER_CONCURRENCY_LIMIT=6
RATE_LIMIT_BURST=60.0
RATE_LIMIT_RPS=20.0

# Session & Authentication
SESSION_SECRET=<YOUR_RANDOM_64_CHAR_HEX_SECRET>
COOKIE_SECURE=0
ALLOWED_EMAIL_DOMAINS=iisc.ac.in
OTP_MAX_PER_HOUR=10
OTP_MAX_PER_IP_HOUR=1000

# Retrieval Thresholds
RETRIEVAL_MIN_SIMILARITY=0.52
RETRIEVAL_SCORE_MARGIN=0.10
RETRIEVAL_MAX_KNEE_DROP=0.06
RETRIEVAL_EF_RUNTIME=64
```

Load `.env` into your active shell session:
```bash
set -a && source .env && set +a
```

---

## 4. Redis Stack: Native User-Space Setup

The system requires **Redis Stack** for RediSearch vector indexes (`FT.*` commands). It runs as a native standalone binary without Docker or root privileges.

### Step 1: Download & Extract Binary
```bash
# Download official Redis Stack Server binary for Linux x86_64
wget https://packages.redis.io/redis-stack/redis-stack-server-7.2.0-v9.jammy.x86_64.tar.gz
tar -xzf redis-stack-server-7.2.0-v9.jammy.x86_64.tar.gz
mv redis-stack-server-7.2.0-v9.jammy.x86_64 redis-stack-server-7.2.0-v9
```

### Step 2: Export Path and Credentials
Add the Redis binary directory to your `$PATH` and export authenticated CLI credentials:
```bash
# 1. Export Redis binary directory to PATH (enables 'redis-stack-server' and 'redis-cli' commands directly)
export PATH="$PWD/redis-stack-server-7.2.0-v9/bin:$PATH"

# 2. Export Redis credentials for CLI and runtime scripts
# Security Note: Setting REDISCLI_AUTH allows 'redis-cli' to authenticate automatically without passing '-a' on the command line (which prevents passwords from leaking in 'ps aux' or /proc)
export REDIS_PASSWORD="<YOUR_REDIS_PASSWORD>"
export REDISCLI_AUTH="<YOUR_REDIS_PASSWORD>"
```
*(Tip: Add `export PATH="$PWD/redis-stack-server-7.2.0-v9/bin:$PATH"` to your `~/.bashrc`)*

---

## 5. Starting from Pre-Built RDB Snapshot (2.26M Chunks)

The pre-indexed corpus is stored in a 12.67 GB snapshot file: **`redis_server_data/dump_final.rdb`**.

### Snapshot Placement
```text
onprem-rag-gateway/
├── redis_server_data/
│   └── dump_final.rdb       <-- Keep the snapshot here (12.67 GB)
```

### Starting Redis Stack with the Snapshot

#### Via Make Shortcut:
```bash
make redis-up
```

#### Via Native Command:
```bash
# Uses exported PATH and REDIS_PASSWORD safely:
redis-stack-server \
    --dir ./redis_server_data \
    --dbfilename dump_final.rdb \
    --port 6379 \
    --requirepass "$REDIS_PASSWORD" \
    --maxmemory 48gb \
    --maxmemory-policy noeviction \
    --daemonize yes
```

### Monitoring the Loading Process (~45 Minutes)
> [!NOTE]
> Loading 2,261,664 384-dimensional dense vectors and HNSW graphs from a 12.67 GB RDB snapshot into RAM takes **35 to 50 minutes** depending on disk read throughput. While initializing, Redis will report `LOADING Redis is loading the dataset in memory`.

#### Interactive Live Progress Monitor:
```bash
# Via Make:
make redis-watch

# Via Native Python Script:
python scripts/track_redis.py
```
*(Displays a live terminal progress bar with percentage, loaded bytes, ETA in seconds, and automatically exits when loading hits 100%)*

#### One-Shot Status Checks:
```bash
# Via Make:
make redis-status

# Via Native redis-cli (automatically authenticated via REDISCLI_AUTH, no '-a' needed):
redis-cli info Persistence | grep -E "loading:|loading_loaded_perc:|loading_eta_seconds:"
```

#### Verifying Loaded Index:
Once loading completes (`loading:0`):
```bash
# Verify total chunks loaded:
redis-cli dbsize
# -> (integer) 2261664

# Verify RediSearch index schema:
redis-cli FT.INFO rag_documents_idx | head -n 20

# Test Redis connection:
redis-cli ping
# -> PONG
```

#### Stopping Redis:
```bash
# Via Make:
make redis-down

# Via Native redis-cli (authenticated via REDISCLI_AUTH, safe from ps inspection):
redis-cli shutdown nosave
```

---

## 6. Data Ingestion: DSpace Crawlers & Batch PDF Indexing

If you are crawling fresh institutional publications or ingesting new PDFs instead of starting from the snapshot:

### Pre-requisite: Embedding Model
Download the offline sentence embedding weights:
```bash
python -c "from transformers import AutoTokenizer, AutoModel; AutoTokenizer.from_pretrained('sentence-transformers/all-MiniLM-L6-v2'); AutoModel.from_pretrained('sentence-transformers/all-MiniLM-L6-v2')"
```

### Option A: Streaming Ingestion directly from IISc DSpace
The streaming crawler downloads each thesis, extracts layout-aware text, chunks it (512 tokens with 64 overlap), embeds it via `all-MiniLM-L6-v2`, indexes it into RediSearch, and deletes the PDF to preserve disk space:

```bash
# Smoke test (ingest 20 papers and preserve raw PDFs in data/raw_pdfs/):
python -m pipeline.crawlers.stream_dspace "https://etd.iisc.ac.in/handle/2005/1" --max 20 --keep-pdfs

# Full production ingestion (run inside tmux):
tmux new -s dspace_crawl
python -m pipeline.crawlers.stream_dspace "https://etd.iisc.ac.in/handle/2005/1" --unlimited --concurrency 3 --delay 1.0
```

### Option B: Batch PDF Directory Ingestion
Drop arbitrary research PDFs into `data/raw_pdfs/`:

```bash
# Via Make:
make ingest-pdfs

# Via Native Python Command:
python -m pipeline.batch_ingest_pdfs \
    --pdf-dir data/raw_pdfs \
    --index-name rag_documents_idx \
    --vector-dim 384
```

---

## 7. CPU SLM Tier (Port 8082 - Qwen 2.5 3B)

The CPU SLM serves routine factual questions ($P(\text{hard}) < 0.65$) and acts as the zero-downtime fallback engine.

### Model Weights
```bash
# Download 4-bit medium quantized GGUF (2.0 GB):
mkdir -p models/slm_gguf
wget -O models/slm_gguf/qwen2.5-3b-instruct-q4_k_m.gguf \
  https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf
```

### Starting CPU SLM Server
```bash
# Via Make:
make serve-slm

# Via Native Command (6 parallel workers, 16 OpenMP threads each):
python -m pipeline.serve_cpu_slm --port 8082 --workers 6 --threads 16
```

### Testing CPU SLM Endpoint
```bash
curl -s http://localhost:8082/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"What is vermiculite?"}],"max_tokens":64}' | jq .
```

---

## 8. GPU LLM Tier (Port 8000 - vLLM Qwen 2.5 7B)

The GPU tier serves complex queries ($P(\text{hard}) \ge 0.65$) requiring cross-document synthesis.

### Model Weights & Starting vLLM Server
```bash
# Download Qwen2.5-7B-Instruct:
huggingface-cli download Qwen/Qwen2.5-7B-Instruct --local-dir models/llm_7b/qwen2.5-7b-instruct

# Via Make:
make serve-gpu

# Via Native Command (always use the hardened launcher, never `python -m vllm.entrypoints.openai.api_server`):
CUDA_VISIBLE_DEVICES=0 python scripts/serve_vllm.py \
    --model models/llm_7b/qwen2.5-7b-instruct \
    --served-model-name "Qwen/Qwen2.5-7B-Instruct" \
    --host 127.0.0.1 \
    --port 8000 \
    --gpu-memory-utilization 0.90 \
    --max-model-len 8192 \
    --dtype bfloat16
```

> **Security note (vLLM 0.6.3):** stock vLLM 0.6.3 pre-binds its socket with `bind(("", port))`, so `--host 127.0.0.1` is ignored and the model API listens on **all interfaces**. `scripts/serve_vllm.py` forces the bind onto `--host`, refuses non-loopback hosts, and refuses to start without `VLLM_API_KEY` (set in `.env`), which vLLM then enforces as `Authorization: Bearer <key>` on every `/v1/*` route. Verify after every restart: `ss -ltnp | grep :8000` must show `127.0.0.1:8000`.

### Testing GPU LLM Endpoint
```bash
curl -s http://localhost:8000/v1/models -H "Authorization: Bearer $VLLM_API_KEY" | jq .   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/models                    # 401
```

---

## 9. Two-Tower Neural Complexity Router

The Two-Tower MLP Router classifies queries in real time into easy ($< 0.65$) or hard ($\ge 0.65$) using a 400-dimensional hybrid representation (384 semantic + 16 structural features).

### Evaluation & Standalone Service
```bash
# Run complexity scoring evaluation:
python scripts/eval_router_threshold.py

# Optional: Run router as standalone microservice on port 8090:
# (Note: Gateway automatically runs the router in-process if 8090 is not active)
make serve-router
```

---

## 10. Unified Gateway & Web Chat UI (Port 8080)

The Gateway binds to **Port 8080** and serves the AskIISc Web Chat interface (`/`), session authentication, and API endpoints.

### Starting Gateway

#### Development Mode (with Live Code Reload):
```bash
# Via Make:
make dev

# Via Native Uvicorn:
uvicorn gateway.main:app --host 0.0.0.0 --port 8080 --reload
```

#### Production Mode (6 Uvicorn Workers):
```bash
# Via Make:
make chat-up

# Via Native Uvicorn:
uvicorn gateway.main:app --host 0.0.0.0 --port 8080 --workers 6
```

### Testing In-Process Endpoints
```bash
# Check health:
curl -s http://localhost:8080/healthz

# Semantic search query:
curl -s -X POST http://localhost:8080/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is vermiculite?", "max_tokens": 150}' | jq .

# Administrative semantic cache statistics:
curl -s http://localhost:8080/cache/stats | jq .
```

---

## 11. One-Shot Production Orchestration

The entire 4-tier stack can be managed using [`scripts/prod_stack.py`](scripts/prod_stack.py) or `make`:

```bash
# Start all 4 services (Redis, vLLM, CPU SLM, Gateway):
make prod-up
# Or natively:
python scripts/prod_stack.py start

# Check live health status of all tiers:
make prod-status
# Or natively:
python scripts/prod_stack.py status

# Graceful restart with zero downtime:
make prod-restart
# Or natively:
python scripts/prod_stack.py restart

# Stop services (preserves Redis Stack database in RAM):
make prod-down
# Or natively:
python scripts/prod_stack.py stop
```

---

## 12. Verification & Test Suites

The codebase includes an extensive suite of unit, security, and intensive end-to-end integration tests:

### Running Full Pytest Suite (84 Tests):
```bash
# Via Make:
make test

# Via Native Pytest:
pytest tests/ -v
```

### Running Intensive Full-Stack Verification (36 Checks):
Verifies live MLP routing, CPU SLM TTFT, GPU vLLM throughput, Redis semantic cache exact and paraphrase hits, and output sanitization:
```bash
# Via Make:
make test-intensive

# Via Native Command:
python tests/test_intensive_full_stack.py
```

### Resetting OTP Rate Limits during Testing:
```bash
# Clear limits for a specific user:
python scripts/clear_otp_limits.py user@iisc.ac.in

# Clear all rate limits:
python scripts/clear_otp_limits.py --all
# Or via Make:
make otp-clear
```

---

## 13. Service Port Reference

| Port | Service Component | Host Binding | Access Restriction |
| :---: | :--- | :---: | :--- |
| **`8080`** | **Unified Gateway & AskIISc Web UI** | `0.0.0.0` | **Public/Campus Facing**; Session Auth, CSRF, Token-Bucket Admission |
| **`8082`** | **CPU SLM Daemon (Qwen 2.5 3B)** | `127.0.0.1` | **Internal Loopback Only**; 6 workers $\times$ 16 threads |
| **`8000`** | **GPU LLM Engine (vLLM Qwen 2.5 7B)** | `127.0.0.1` | **Internal Loopback Only**; PagedAttention v2 on NVIDIA A100 |
| **`6379`** | **Redis Stack Server (Vector DB & Cache)** | `127.0.0.1` | **Internal Loopback Only**; Protected by `REDIS_PASSWORD` |

---

## 14. Troubleshooting Guide

| Issue / Symptom | Root Cause | Exact Remediation |
| :--- | :--- | :--- |
| **`NOAUTH Authentication required`** | `REDIS_PASSWORD` not supplied to CLI or script. | Run `export REDISCLI_AUTH="<YOUR_REDIS_PASSWORD>"`. Ensure `REDIS_URL` in `.env` has format `redis://:<PW>@localhost:6379/0`. |
| **`LOADING Redis is loading the dataset in memory`** | Redis is actively reading `dump_final.rdb` into RAM. | **Do not terminate Redis.** Run `make redis-watch` to view live progress bar and ETA. Wait for `loading:0`. |
| **`unknown command 'FT.SEARCH'`** | Plain Redis is running instead of Redis Stack. | Stop standard redis (`sudo systemctl stop redis`). Start native binary (`redis-stack-server` or `make redis-up`). |
| **Zero chunks retrieved / out-of-domain refusal** | Similarity below 0.52 floor or index empty. | Check that `FT.INFO rag_documents_idx` shows `num_docs: 2261664`. Ensure `all-MiniLM-L6-v2` is installed. |
| **Gateway returns 429 Too Many Requests** | Client token bucket capacity exhausted. | Wait 2 seconds for bucket refill (20 tokens/sec), or increase `RATE_LIMIT_BURST` and `RATE_LIMIT_RPS` in `.env`. |
| **OTP Email not received** | SMTP credentials invalid or rate limit hit. | Run `python scripts/clear_otp_limits.py <your_email>`. Verify SMTP credentials with `python scripts/test_smtp.py <your_email>`. |
| **vLLM CUDA Out-of-Memory** | GPU VRAM over-allocation. | Set `--gpu-memory-utilization 0.85` or decrease `--max-model-len 4096` in `Makefile` / `scripts/prod_stack.py`. |
| **CSRF Error: 403 Forbidden** | Missing or mismatched CSRF cookie/header. | Ensure client passes `X-CSRF-Token` header matching the `csrftoken` cookie issued by `GET /me` or `GET /`. |

---

## 15. Quick Start Cheat Sheet

```bash
# 1. Environment & Dependencies
git clone https://github.com/rutujmanware/Onprem-RAG-gateway.git && cd Onprem-RAG-gateway
make setup && source .venv/bin/activate
cp .env.example .env && chmod 600 .env
set -a && source .env && set +a

# 2. Redis Binary & Path
export PATH="$PWD/redis-stack-server-7.2.0-v9/bin:$PATH"
export REDISCLI_AUTH="<YOUR_REDIS_PASSWORD>"

# 3. Start Redis & Monitor Loading (~45 mins on first boot)
make redis-up
make redis-watch

# 4. Verify Database Ready (authenticated via REDISCLI_AUTH, no plaintext password in ps)
redis-cli dbsize       # Should output: 2261664

# 5. Launch Full Production Stack (CPU SLM :8082, vLLM :8000, Gateway :8080)
make prod-up
make prod-status

# 6. Verify System Health & Security
make test-intensive

# 7. Access Web Chat UI
# Open in browser: http://localhost:8080/
```
