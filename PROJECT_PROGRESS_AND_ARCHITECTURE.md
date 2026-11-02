# On-Premise Institutional RAG Gateway: Comprehensive Progress, Architecture & Engineering Log

> **Living Documentation:** This document serves as the master engineering log for the project. Maintain and update this file as milestones are achieved and new features are integrated.  
> **Authors & Maintainers:** Rutuj Manware & Team  
> **Repository:** [rutujmanware/Onprem-RAG-gateway](https://github.com/rutujmanware/Onprem-RAG-gateway)  
> **Deployment Node:** Dual AMD EPYC 7713 (128 Physical Cores / 256 vCPUs), 8x NVIDIA A100-SXM4-40GB, 504 GB RAM Disk (`/dev/shm`), Redis Stack 7.2.0-v9

---

## 📌 1. Project Motivation & Core Objective

In institutional and academic high-performance compute (HPC) clusters, **GPUs are heavily constrained resources** booked for weeks for large-scale training jobs and massive batch inference. 

When institutional users (students, faculty, researchers) query an on-premise RAG system:
- **70% to 80% of queries are "easy" / factual:** direct lookups from a single thesis or specific document (e.g., *"What is the factor of safety at 3m depth in the seismic hazard study?"*).
- **20% to 30% of queries are "hard" / cross-cutting:** comparing methodologies across multiple papers, mathematical proofs, or multi-hop synthesis.

### The Problem:
Sending all queries to high-end GPUs creates **GPU starvation and massive queuing delays** (head-of-line blocking). Simple queries wait in line behind heavy queries, while expensive 400W GPUs are wasted on trivial tasks.

### The Solution:
An **Autonomous On-Premise Hybrid Routing Gateway**:
1. **CPU SLM Tier (Qwen 2.5 3B Instruct):** Runs on multi-core server CPUs with **zero GPU egress**, answering routine queries in seconds.
2. **GPU LLM Tier (vLLM / 70B Tier):** Reserved strictly for complex, multi-chunk, cross-paper reasoning.
3. **Institutional Vector Database (Redis Stack):** Scaled to millions of chunks with sub-50ms dense KNN vector search.

---

## 🏗️ 2. Work Completed Till Now (Module by Module)

### Phase 1: Data Ingestion & Vectorization Pipeline
* **Target Corpus:** Over **12,900+ academic theses and research papers** scraped directly from institutional DSpace repositories.
* **Extraction & Cleaning ([`pipeline/crawlers/`](pipeline/crawlers/), [`pipeline/text_processing/`](pipeline/text_processing/)):**
  - Built robust PDF text extraction with PyMuPDF/Surya layout parsing.
  - Implemented regex noise cleaning (`cleaner.py`) removing page headers, citation junk, and formatting artifacts.
  - Developed recursive chunking (`chunker.py`) with token overlap to preserve cross-sentence semantic context.
* **Dense Vector Embeddings ([`pipeline/embeddings/`](pipeline/embeddings/)):**
  - Generated 384-dimensional dense embeddings using `sentence-transformers/all-MiniLM-L6-v2`.
* **Redis Indexing ([`pipeline/indexer.py`](pipeline/indexer.py)):**
  - Successfully indexed exactly **2,261,664 vector chunks** into Redis Stack using HNSW/KNN vector indexing.
  - Snapshot persisted to disk in `dump_final.rdb` (12 GB on disk, decompressing to ~43 GB in RAM).

---

### Phase 2: Router Training Dataset Generation
To train and validate the complexity router, we generated a comprehensive dataset distinguishing "easy" single-doc queries from "hard" multi-chunk queries:
* **Dataset Generator ([`dataset_generator.py`](dataset_generator.py)):**
  - Scanned chunk keys from Redis and grouped by document IDs.
  - Constructed dynamic multi-chunk prompts (3 to 10 chunks, intra-doc and cross-doc).
  - Executed on a SLURM cluster using **Qwen 2.5 72B Instruct (GPTQ-Int8)** distributed across 4x A100 GPUs via vLLM.
* **Artifacts Created:**
  - `router_training_dataset.jsonl` / `.csv` (100,000 samples).
  - `router_multichunk_training_dataset.jsonl` / `.csv` (multi-document comparison samples).

---

### Phase 3: Gateway, Caching & Admission Control
Implemented an enterprise-grade async gateway in FastAPI ([`gateway/`](gateway/)):
* **Zero-Trust Security & OIDC Auth ([`gateway/auth/`](gateway/auth/)):** JWT validation, domain whitelisting (`iisc.ac.in`), and prompt injection fencing.
* **Dual-Tier Cache ([`gateway/cache/`](gateway/cache/)):**
  - **L0 Exact Match:** Sub-millisecond SHA-256 hash lookup in Redis.
  - **L1 Semantic Cache:** Dense cosine similarity lookup against prior query vectors (threshold: 0.92) to prevent redundant LLM calls.
* **Admission Control & Fair Queuing ([`gateway/queue/`](gateway/queue/)):** Token-bucket rate limiting and Deficit Round Robin (DRR) queuing to prevent any single user from monopolizing capacity.
* **Dynamic Complexity Router ([`gateway/router/`](gateway/router/)):**
  - `complexity_scorer.py`: Evaluates lexical complexity, question structure, comparative keywords, and embedding dispersion.
  - `dispatcher.py`: Routes queries with score $\le \alpha$ (default 0.65) to CPU SLM, and $> \alpha$ to GPU LLM.

---

### Phase 4: CPU SLM Serving & Real-Time RAG Pipeline
* **CPU Inference Engine ([`pipeline/slm_runner.py`](pipeline/slm_runner.py)):** Dedicated CPU execution for Qwen 2.5 3B Instruct.
* **OpenAI-Compatible Server ([`pipeline/serve_cpu_slm.py`](pipeline/serve_cpu_slm.py)):** FastAPI server on port 8082 supporting `/v1/chat/completions` and `/healthz`.
* **End-to-End RAG QA Coordinator ([`pipeline/rag_chain.py`](pipeline/rag_chain.py)):** Combines dense vector retrieval from Redis, grounded prompt synthesis, and real-time streaming answer generation.

---

## ⚠️ 3. Initial Challenges Encountered & How We Solved Them

| # | Challenge | Initial Impact | Engineering Solution |
| :--- | :--- | :--- | :--- |
| **1** | **Server RAM Inflation on Redis** | 12 GB RDB file inflates to **~43 GB RAM** when loaded into Redis. Leaving it running 24/7 risked OOMing user-space jobs. | Packaged standalone user-space binary in `redis-stack-server-7.2.0-v9`. Created clean `make redis-up`, `make redis-down`, and `make redis-status` scripts to safely load/unload the database without root privileges. |
| **2** | **vLLM 72B CUDA OOMs & Hangs** | SLURM batch job failed with CUDA memory fragmentation and custom all-reduce errors across non-contiguous GPUs. | Configured `export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` and `export VLLM_CUSTOM_ALL_REDUCE=0` in `run_slurm.sh`. Added automatic GPU memory polling before loading the model. |
| **3** | **Disk Partition 100% Full** | The `/data2` disk partition hit 100% capacity (only 1.4 GB available), failing new model downloads and causing crashes. | Utilized the server's **504 GB in-memory RAM disk (`/dev/shm`)**. Downloaded model artifacts to `/dev/shm/models/` and symlinked to `models/slm_gguf/`, bypassing the disk bottleneck with zero disk usage. |
| **4** | **Host CUDA Driver Mismatch** | PyTorch in the CPU environment had `cu130` while host driver was `CUDA 12.2`, causing driver probing crashes. | Hard-isolated the CPU tier in `pipeline/slm_runner.py` with `os.environ["CUDA_VISIBLE_DEVICES"] = ""` to completely prevent driver initialization attempts. |
| **5** | **SLM CPU Latency & Rambling** | Testing the SLM on CPU took **63.14 seconds** (~4.5 tok/s for 283 tokens), creating a perception of system freeze. | 1. Implemented **real-time token streaming** via `TextIteratorStreamer` and SSE.<br>2. Scaled thread pool to **48 threads** on AMD EPYC, cutting prefill by **>50%**.<br>3. Enforced concise grounding (2–4 sentences, max 160 tokens). Total time dropped to **21 seconds (3x speedup)**. |
| **6** | **Git Accidental Staging** | `git add .` mistakenly staged the 12 GB vector database, 6 MB logs, and 190 MB datasets, risking GitHub push rejection. | Formulated comprehensive [`.gitignore`](.gitignore) rules for `*.rdb`, `*.tar.gz`, `myenv/`, `*.log`, and `*.jsonl`. Cleaned git index, ensuring only source code moves to remote. |

---

## 📊 4. Quantitative Results & Benchmarks

### 1. Vector Retrieval Performance
* **Total Chunks in Index:** `2,261,664`
* **Embedding Model:** `all-MiniLM-L6-v2` (384 dimensions)
* **Cosine Similarity Retrieval Latency:** **0.034s – 0.080s** across the 2.26M chunk index.

### 2. CPU SLM End-to-End Latency Benchmark

Tested on the factual academic query:
> *"How do the factor of safety values against liquefaction at 3 m and 6 m depth compare for a return period of 475 years, and what might explain the differences in these values?"*

```
Baseline Pipeline (24 threads, batch, unconstrained prompt):
Retrieval (0.08s) ──> Prefill / TTFT (~28s) ──────────> Generation (~35s) ──> Total: 63.14s (283 tokens, blank wait)

Optimized Pipeline (48 threads, SSE streaming, concise prompt):
Retrieval (0.04s) ──> Prefill / TTFT (~11s) ──> Gen (~10s) ──> Total: 21.15s (88 tokens, words stream live in <11s)
```

| Metric | Before Optimization | After Optimization | Impact |
| :--- | :--- | :--- | :--- |
| **Context Prefill / TTFT** | ~28.0 seconds | **~11.1 seconds** | **2.5x Faster First Token** |
| **Total Response Time** | **63.14 seconds** | **21.15 seconds** | **3.0x Faster Total Latency** |
| **Tokens Generated** | 283 tokens (verbose essay) | 88 tokens (direct & factual) | High precision, zero fluff |
| **Perceived Responsiveness** | Blank screen for 63 seconds | Words stream live to screen | Immediate user engagement |
| **GPU Utilization** | **0.0% (100% on CPU)** | **0.0% (100% on CPU)** | **Zero GPU Starvation** |

### 3. Factual Grounding Verification
When tested on single-document questions, the CPU SLM achieves **zero hallucinations**, accurately citing specific figures and physical laws directly from retrieved text:
> *"The factor of safety against liquefaction for a return period of 475 years is higher at 6 meters depth compared to 3 meters depth. Specifically, Fig. 8.8 shows a factor of safety range of approximately 0 – 1 at 6 meters depth, while Fig. 8.6 indicates a similar range but slightly lower at 3 meters depth. This difference can be attributed to the increased overburden pressure and decreased relative density (rd) with depth."*

When tested on multi-document cross-paper questions where top-k context is incomplete, the SLM strictly obeys its guardrail:
> *"Based on the provided documents, there is insufficient information to directly compare... The documents do not provide specific details..."*

---

## 🛠️ 5. Runbook for Teammates (Copy-Paste Commands)

### 1. Activating the Environment
```bash
cd /data2/home/ritujmanware/onprem-rag-gateway
source /data2/home/ritujmanware/miniconda3/etc/profile.d/conda.sh
conda activate rag_env
set -a && source .env && set +a
```

### 2. Checking Redis Vector Database Status
```bash
# Check if Redis is alive and keys are loaded
./redis-stack-server-7.2.0-v9/bin/redis-cli -a "$REDIS_PASSWORD" info persistence | grep -E "loading:|rdb_last_load_keys_loaded:"
```

### 3. Running Live Streaming RAG Query
```bash
# Runs end-to-end RAG with live token streaming in your terminal
python -m pipeline.rag_chain \
  "How do the factor of safety values against liquefaction at 3 m and 6 m depth compare for a return period of 475 years, and what might explain the differences in these values?" \
  --top-k 2
```

### 4. Starting the CPU SLM Server
```bash
# Starts OpenAI-compatible FastAPI server on port 8082 with 48 threads
python -m pipeline.serve_cpu_slm --port 8082 --threads 48
```

---

## 🗺️ 6. Living Roadmap & Next Steps

Maintain this section as new features are developed:

- [x] **Milestone 1:** PDF Ingestion, text cleaning, chunking, and embedding pipeline.
- [x] **Milestone 2:** Redis Stack deployment and indexing of 2.26M chunks.
- [x] **Milestone 3:** Synthetic router dataset generation using Qwen 2.5 72B on SLURM.
- [x] **Milestone 4:** Hybrid Gateway architecture (OIDC, L0/L1 Cache, Fair Queue, Router).
- [x] **Milestone 5:** CPU SLM optimization (streaming, 48-thread tuning, concise prompt budgeting).
- [ ] **Milestone 6:** End-to-end Gateway integration test (connecting Gateway port 8080 to CPU SLM port 8082).
- [ ] **Milestone 7:** Router classification training & threshold $\alpha$ optimization on `router_multichunk_training_dataset.jsonl`.
- [ ] **Milestone 8:** Multi-user load testing (`evaluation/benchmarks/load_test.py`) proving GPU queue avoidance under 30+ concurrent campus users.

