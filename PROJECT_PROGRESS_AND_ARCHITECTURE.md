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

---

## 📅 Today's Progress & Architecture Update (November 2, 2026)

### 1. Executive Summary of Achievements
Today, we advanced the gateway from a prototype retrieval pipeline into an intelligent, dynamically routed hybrid system:
1. **Retrieved & Commenced MLP Router Integration:** Extracted the baseline neural query complexity router from branch `origin/kriti`.
2. **Diagnosed & Overcame Severe Training Flaws:** Uncovered a critical length bias shortcut and class imbalance in the initial data.
3. **Engineered 102,000-Sample Rebalanced Dataset:** Merged 50k easy, 50k hard, and 2k diverse augmentations (short hard proofs and long easy syllabus lookups) to eliminate length bias.
4. **Architected Two-Tower Neural Router (`RouterMLP`):** Designed independent semantic (384-d) and lexical/intent (16-d) towers fused into a single classification head, achieving **99.62% Accuracy** and **0.9962 Macro F1** in $<20\text{ ms}$ CPU inference.
5. **Integrated Application Pipeline & 7B Deferred Intercept:** Added Step 0 routing to [`pipeline/rag_chain.py`](pipeline/rag_chain.py) and [`gateway/main.py`](gateway/main.py). Hard queries immediately return a hardcoded notice that the 7B model is not yet ready, saving 100% of GPU/CPU/retrieval resources. Easy queries route to Redis retrieval and the CPU SLM tier.
6. **Verified Factual Grounding on Unseen Knowledge Base Content:** Executed an unseen query against the indexed DSpace thesis corpus, proving factual generation without hallucination.

---

### 2. Architecture Status Clarification: Caching
> [!IMPORTANT]
> **Cache is NOT yet implemented in the live pipeline.**  
> Although prototype modules exist in [`gateway/cache/`](gateway/cache/) (L0 exact match and L1 semantic cache), they have **not** been integrated into the active request flow. All incoming queries currently bypass cache and flow directly into Step 0 dynamic neural complexity routing. Integrating the dual-tier cache remains a planned milestone.

---

### 3. Model Context Window Sizes & Token Budgeting

Understanding the exact token limits across all tiers is crucial for prompt budgeting and preventing truncation:

| Component | Model Name / Engine | Context Window Size | Max Generation Limit | Typical RAG Usage |
| :--- | :--- | :--- | :--- | :--- |
| **Embedder & MLP Router** | `sentence-transformers/all-MiniLM-L6-v2` | **512 tokens** | N/A (Embedding) | ~15 – 60 tokens per query |
| **CPU SLM Tier** | `Qwen/Qwen2.5-3B-Instruct` | **32,768 tokens (32k)**<br>*(up to 128k via sliding window)* | **160 tokens** (default `max_new_tokens`) | ~600 – 1,200 tokens (System prompt + Top-3 chunks + query) |
| **GPU LLM Tier (7B)** | `Qwen/Qwen2.5-7B-Instruct` | **32,768 tokens (32k)**<br>*(up to 128k via sliding window)* | Configurable (up to 8,192 tokens) | Reserved for cross-paper multi-chunk reasoning |
| **GPU LLM Tier (Fallback)** | `meta-llama/Llama-2-7b-chat-hf` | **4,096 tokens (4k)** | 512 tokens | Fallback model in `.env` |
| **Vector Chunks (Redis)** | RediSearch `rag_documents_idx` | **~100 – 150 tokens** per chunk (~500 chars) | N/A (Storage) | Top-3 search returns ~350 – 500 context tokens |

#### Context Budgeting Strategy for CPU SLM:
* **System Prompt (~200 tokens):** Institutional academic persona, anti-hallucination guardrail, citation instructions.
* **Retrieved Context (~350–500 tokens):** Top-3 Redis chunks formatted with document titles and chunk identifiers.
* **User Query (~20–60 tokens):** Single admission query.
* **Output Buffer (160 tokens):** Factual, concise 2–4 sentence generation (<20 seconds on 48 CPU threads).
* **Headroom Available:** Over **31,000 tokens remaining** in the 32k window, allowing future expansions to top-10 chunks or conversation history without risk of context overflow.

---

### 4. Technical Difficulties Encountered & Approaches Used

#### 1. Git History Collision when Pulling Branch `origin/kriti`:
* **Difficulty:** Running `git merge origin/kriti` failed with `fatal: refusing to merge unrelated histories` and working-tree conflicts with untracked evaluation sets.
* **Approach:** Backed up untracked working files, fetched `origin/kriti`, and checked out `gateway/router/mlp_router/` surgically, preserving all local progress while pulling the required model files.

#### 2. The Sentence Length Shortcut Flaw:
* **Difficulty:** Initial evaluation of the baseline MLP revealed that concise hard queries (e.g., *"Derive the mathematical relationship between learning rate and gradient variance in SGD"*) were wrongly classified as `EASY` ($P(\text{hard}) = 0.0001$), while long course logistics queries defaulted to `HARD`. The single-tower MLP had learned sequence length rather than complexity because easy queries in the training set were short and hard queries were verbose.
* **Approach:** Synthesized a balanced 102,000-sample dataset combining 50k easy, 50k hard, and 2,000 diverse short hard/long easy augmentations. Designed a **Two-Tower Neural Architecture** where Tower 1 encodes 384-d semantic embeddings (`all-MiniLM-L6-v2`) and Tower 2 encodes 16-d hand-crafted lexical/intent features (derivation verbs, comparative markers, clause counts). This ensures structural signals cannot be drowned out by sentence length.

#### 3. Python 3.11 C-Extension Conflicts in `rag_env`:
* **Difficulty:** Testing in the user's `rag_env` (Python 3.11) threw `ModuleNotFoundError: No module named 'tokenizers.tokenizers'`.
* **Approach:** A legacy `packages/` directory containing Python 3.8 binary wheels was prepended to `sys.path`. We completely removed `packages/` and purged all `sys.path.insert(0, ...packages)` references from the codebase.

#### 4. Upstream Dependency Breakage in `sentence_transformers`:
* **Difficulty:** Importing `sentence_transformers` in `rag_env` failed with `ImportError: cannot import name 'BucketNotFoundError' from 'huggingface_hub.errors'`.
* **Approach:** Standardized all inference embedding in [`gateway/router/mlp_router/inference.py`](gateway/router/mlp_router/inference.py) on [`LocalEmbedder`](pipeline/embeddings/embedder.py), which uses Hugging Face `transformers` with mean pooling offline without relying on `sentence_transformers`.

#### 5. SLM Generation Duplicate Keyword Collision:
* **Difficulty:** Invoking `model.generate()` in `pipeline/slm_runner.py` failed with:
  `TypeError: transformers.generation.utils.GenerationMixin.generate() got multiple values for keyword argument 'attention_mask'`.
* **Approach:** `inputs = tokenizer(...)` already returned a dict containing `attention_mask`. Passing `**inputs` alongside `gen_kwargs` (which also had `attention_mask`) passed the argument twice. Refactored `gen_kwargs` to supply inputs and masks once cleanly.

#### 6. Real-Time Stream Event Loop Alignment:
* **Difficulty:** The CLI streaming loop consumed `type: route` as `type: chunks`, printing `Retrieved 0 chunks in 0s`.
* **Approach:** Restructured event consumption in [`pipeline/rag_chain.py`](pipeline/rag_chain.py) to cleanly parse all event types (`route`, `chunks`, `token`, `done`), and bypass retrieval entirely when hard queries are detected.

#### 7. Credential Protection in Command Execution:
* **Difficulty:** Passing explicit passwords via `-a "$REDIS_PASSWORD"` in shell commands exposes credentials to process tables and bash history.
* **Approach:** Standardized execution using the user's pre-configured `~/.bashrc` (`source ~/.bashrc && conda activate rag_env`), using `REDISCLI_AUTH` without exposing passwords or hardcoded paths.

---

### 5. Verification on Unseen Knowledge Base Query

To prove that the pipeline produces accurate answers from the indexed Redis thesis corpus without repeating training data, we executed a completely unseen query:
* **Query:** `"What is the primary QoS attribute considered for Internet of Things applications in serverless platforms?"`
* **Check:** Verified 0 occurrences in `data/router_multichunk_training_dataset.jsonl` and `data/diverse_augmentations_2k.jsonl`.
* **Step 0 Routing:** Classified as `EASY` ($P(\text{hard}) = 0.0001$).
* **Step 1 Retrieval:** Retrieved 3 chunks from thesis `dspace_2005_6728` (*"Designing Quality of Service aware Serverless Platforms"*) with top similarity of **77.5%**.
* **Step 2 Generation:** CPU SLM (`Qwen 2.5 3B Instruct`) generated the exact factual answer without hallucination:
  > *"The primary QoS attribute considered for Internet of Things (IoT) applications in serverless platforms is performance characteristic and response latency."*

---

### 6. Runbook for Teammates (Copy-Paste Commands)

Teammates can reproduce and test today's work using these copy-paste commands:

```bash
# 1. Activate Environment (Standard Conda Setup)
cd /data2/home/ritujmanware/onprem-rag-gateway
source ~/.bashrc
conda activate rag_env
set -a && source .env && set +a

# 2. Verify Redis Database Status
redis-cli ping
# Expected output: PONG

# 3. Test Two-Tower Neural Complexity Router
python gateway/router/mlp_router/inference.py

# 4. Test Unseen Easy Query (Redis Vector Retrieval + CPU SLM Generation)
python -m pipeline.rag_chain \
  "What is the primary QoS attribute considered for Internet of Things applications in serverless platforms?" \
  --no-stream

# 5. Test Hard Query (Immediate <20ms Deferred 7B Intercept)
python -m pipeline.rag_chain \
  "Synthesize the trade-offs of PagedAttention vs standard KV caching in continuous batching."

# 6. Start the Gateway Server
uvicorn gateway.main:app --host 0.0.0.0 --port 8080 --reload

# 7. Test Gateway API Endpoints via cURL
# Test 1: Direct Query Endpoint (Hard query -> returns 7B deferred notice)
curl -X POST http://localhost:8080/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Derive the mathematical relationship between learning rate and gradient variance in SGD."}'

# Test 2: OpenAI-Compatible Chat Completion Endpoint (Hard query)
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Compare the methodologies used in the study on polymer membrane with continuous glucose monitoring."}]
  }'
```

---

### 7. Living Roadmap Status Update

- [x] **Milestone 6:** End-to-end Gateway integration test (connecting Gateway port 8080 to CPU SLM tier with hardcoded 7B deferred routing).
- [x] **Milestone 7:** Router classification training & Two-Tower neural architecture on 102k balanced samples.
- [ ] **Milestone 8:** Multi-user load testing (`evaluation/benchmarks/load_test.py`) proving GPU queue avoidance under 30+ concurrent campus users.
- [ ] **Milestone 9:** Dual-tier caching implementation (integrating L0 Exact Match & L1 Semantic Cache into the live gateway pipeline).


