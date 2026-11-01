# Institutional On-Premise RAG Gateway: Architecture & CPU SLM Optimization Guide

> **Author / Maintainers:** Rutuj Manware & Team  
> **Last Updated:** November 2026  
> **Repository:** [rutujmanware/Onprem-RAG-gateway](https://github.com/rutujmanware/Onprem-RAG-gateway)  
> **Hardware Environment:** Dual AMD EPYC 7713 (128 Physical Cores / 256 vCPUs), 8x NVIDIA A100-40GB, 504 GB RAM Disk (`/dev/shm`)

---

## 📌 Executive Summary

This guide documents the architecture, performance engineering, and CPU SLM optimizations developed for the **On-Premise Institutional RAG Gateway**. 

In academic and enterprise compute clusters, GPUs are scarce, expensive, and constantly booked for training jobs. To solve GPU starvation and head-of-line request blocking, our gateway implements a **hybrid CPU/GPU routing architecture**:
1. **CPU SLM Tier (Qwen 2.5 3B Instruct):** Answers single-document, factual, and direct research queries (70–80% of campus traffic) strictly on multi-core CPU, with **zero GPU egress**.
2. **GPU LLM Tier (vLLM / 70B Tier):** Reserved strictly for multi-document synthesis, cross-paper comparisons, and complex reasoning.
3. **Redis Stack Vector Database:** 2.26 Million indexed chunks from 12,900+ institutional theses and research papers.

---

## 1. System Architecture Overview

```
                                      +---------------------------------------------+
                                      |                User Query                   |
                                      +----------------------+----------------------+
                                                             |
                                                             v
                                      +---------------------------------------------+
                                      |         Gateway & Complexity Router         |
                                      |     (gateway/router/complexity_scorer.py)   |
                                      +----------------------+----------------------+
                                                             |
                                      +----------------------+----------------------+
                                      |                                             |
                   [Complexity Score <= 0.65 (Easy)]             [Complexity Score > 0.65 (Hard)]
                                      |                                             |
                                      v                                             v
                    +------------------------------------+        +-----------------------------------+
                    |        CPU SLM Tier (Qwen 3B)      |        |        GPU LLM Tier (vLLM 70B)    |
                    |   - Runs strictly on AMD EPYC CPU  |        |   - Runs on NVIDIA A100 GPUs      |
                    |   - High-throughput multi-worker   |        |   - High-context reasoning        |
                    |   - Real-time token streaming      |        |   - Cross-paper comparisons       |
                    +-----------------+------------------+        +-----------------+-----------------+
                                      |                                             |
                                      +----------------------+----------------------+
                                                             |
                                                             v
                                      +---------------------------------------------+
                                      |          Redis Vector Database              |
                                      |   - 2.26 Million dense 384-dim chunks       |
                                      |   - Sub-50ms KNN vector retrieval           |
                                      +---------------------------------------------+
```

---

## 2. The Performance Bottleneck Analysis

When initially testing Qwen 2.5 3B Instruct on CPU on a live institutional query from [`router_multichunk_training_dataset.jsonl`](router_multichunk_training_dataset.jsonl):
* **Initial Response Time:** **63.14 seconds**
* **Generation Throughput:** **~4.49 tokens/second**
* **User Experience:** Complete blank screen / freeze for over a minute.

### Root Cause Diagnostics:

1. **Context Prefill Overhead on CPU:**
   - Evaluating a RAG prompt containing 3 document chunks (~1,000–1,200 tokens) required $O(N^2)$ attention computation in PyTorch `bfloat16`. 
   - On CPU, computing attention across 1,000 tokens took **23 to 32 seconds** before a single word could be generated!

2. **Unconstrained Output Verbosity:**
   - The original SLM prompt produced a long 5-paragraph academic essay (**283 tokens**).
   - At ~4.5 tok/s, 283 tokens naturally required `283 / 4.5 = 63 seconds`.
   - **Insight:** An "easy" RAG query is a direct factual lookup (e.g. *"What is the safety factor at 3m?"*). It requires a **2 to 3 sentence answer (40–80 tokens)**, not an essay.

3. **Autoregressive Memory Bandwidth Limits:**
   - For every single generated token, the CPU must read the entire 3B model matrix from system DDR4 memory.
   - On server architectures, single-stream DDR4 memory bandwidth physically bounds single-thread CPU token generation to **~3.5 to 5.0 tok/s**.

---

## 3. Engineering Optimizations Implemented

### Optimization 1: Real-Time Token Streaming (Server-Sent Events)
Instead of waiting 30–60 seconds for the complete response to generate in batch, we implemented live token streaming:
* **SLM Runner ([`pipeline/slm_runner.py`](pipeline/slm_runner.py)):** Integrated `transformers.TextIteratorStreamer` with dedicated generation threading. Added `generate_chat_stream()` and `answer_rag_stream()`.
* **FastAPI Server ([`pipeline/serve_cpu_slm.py`](pipeline/serve_cpu_slm.py)):** Added `StreamingResponse` on `/v1/chat/completions` emitting OpenAI-compliant chunk events (`text/event-stream`).
* **Gateway Client ([`gateway/clients/cpu_slm_client.py`](gateway/clients/cpu_slm_client.py)):** Added asynchronous SSE generator `generate_chat_stream()`.
* **CLI RAG Chain ([`pipeline/rag_chain.py`](pipeline/rag_chain.py)):** Defaulted to live streaming into stdout with real-time **Time-to-First-Token (TTFT)** measurement.

### Optimization 2: Concise Grounding Prompt & Token Budgeting
In [`pipeline/slm_runner.py`](pipeline/slm_runner.py), we updated the grounded system prompt to instruct:
> *"1. Answer concisely and directly in 2 to 4 factual sentences based strictly on the provided context."*
> *"2. Avoid unnecessary background introductions, preamble, or repetition."*
> *"3. If insufficient information exists, state: 'Based on the provided documents, there is insufficient information to answer this question.' Do not speculate."*

* Set default `max_new_tokens = 160` (easy answers finish in ~40–80 tokens = **8 to 15 seconds**).

### Optimization 3: CPU Thread Scaling on AMD EPYC
We benchmarked prompt evaluation scaling across different CPU thread counts on the AMD EPYC 7713 processor:

| Thread Count | Prefill Time (801 tokens) | Speedup vs Baseline |
| :--- | :--- | :--- |
| 16 Threads | 32.16s | 1.0x (baseline) |
| 24 Threads | 23.02s | 1.4x |
| 32 Threads | 16.77s | 1.9x |
| **48 Threads** | **12.44s** | **2.6x** |
| 64 Threads | 10.64s | 3.0x |

* **Decision:** Configured `CPU_NUM_THREADS=48` in [`.env`](.env) and [`pipeline/slm_runner.py`](pipeline/slm_runner.py). This maximizes compute efficiency per socket without causing cross-socket NUMA bus saturation.

### Optimization 4: GGUF & RAM Disk Acceleration (`/dev/shm`)
* **Disk Constraint:** The server disk partition `/data2` was at 100% capacity (~1.4 GB available).
* **RAM Disk Solution:** Leveraged the server's **504 GB in-memory RAM disk (`/dev/shm`)**.
* Downloaded and quantized `Qwen2.5-3B-Instruct-Q4_K_M.gguf` (2.1 GB) directly into `/dev/shm/models/` and symlinked to [`models/slm_gguf/`](models/slm_gguf/).
* **Memory Win:** Reduced model memory footprint from **6.5 GB down to 2.1 GB (a 67% reduction)**. This allows the server to run **up to 8 to 16 concurrent CPU workers** in parallel memory without exhausting system RAM.

---

## 4. Empirical Benchmark Comparison

Evaluated on the same question from the institutional dataset:
> *"How do the factor of safety values against liquefaction at 3 m and 6 m depth compare for a return period of 475 years, and what might explain the differences in these values?"*

| Configuration | Retrieval Time | Prefill / TTFT | Generation Time | Total Duration | Tokens Generated | Effective UX |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Original Baseline (24 threads, no streaming, unconstrained)** | 0.080s | ~28.0s | ~35.0s | **63.14s** | 283 tokens | Frozen blank screen for 63s |
| **Optimized Pipeline (48 threads, streaming, concise prompt)** | **0.042s** | **~11.1s** | **~10.0s** | **21.15s** | 88 tokens | **Live streaming words, 3x faster** |

### Quality Verification:
The SLM generated a high-precision, factual answer directly citing the document:
> *"The factor of safety against liquefaction for a return period of 475 years is higher at 6 meters depth compared to 3 meters depth. Specifically, Fig. 8.8 shows a factor of safety range of approximately 0 – 1 at 6 meters depth, while Fig. 8.6 indicates a similar range but slightly lower at 3 meters depth. This difference can be attributed to the increased overburden pressure and decreased relative density (rd) with depth."*

---

## 5. Guide for Teammates: How to Run & Verify

### Step 1: Environment Activation
```bash
# Navigate to the workspace
cd /data2/home/ritujmanware/onprem-rag-gateway

# Activate Conda environment and load environment variables
source /data2/home/ritujmanware/miniconda3/etc/profile.d/conda.sh
conda activate rag_env
set -a && source .env && set +a
```

### Step 2: Verify Redis Vector Database
```bash
# Verify Redis is running and finished loading (loading:0 means ready)
./redis-stack-server-7.2.0-v9/bin/redis-cli -a "$REDIS_PASSWORD" info persistence | grep -E "loading:|rdb_last_load_keys_loaded:"
```
*(Should output `loading:0` and `rdb_last_load_keys_loaded: 2261664`)*.

### Step 3: Run Live Streaming RAG Query
```bash
# Run factual query with top-2 chunks and real-time streaming output
python -m pipeline.rag_chain \
  "How do the factor of safety values against liquefaction at 3 m and 6 m depth compare for a return period of 475 years, and what might explain the differences in these values?" \
  --top-k 2
```

### Step 4: Run the CPU SLM API Server
```bash
# Starts the OpenAI-compatible server on port 8082
python -m pipeline.serve_cpu_slm --port 8082 --threads 48
```

Test streaming via `curl`:
```bash
curl -N http://localhost:8082/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "What is concrete fracture?"}],
    "max_tokens": 80,
    "stream": true
  }'
```

---

## 6. Git Hygiene & Best Practices

To prevent accidental repository bloat and GitHub size rejection errors:
1. **Never commit the `.rdb` files or database dumps:** The 12GB vector backups (`dump.rdb`, `dump_final.rdb`, `redis_server_data/`) are strictly listed in [`.gitignore`](.gitignore).
2. **Never commit virtual environments or logs:** `myenv/` and `*.log` are ignored.
3. **Never commit large datasets:** `router_*_training_dataset.*` (190MB) are kept locally.
4. **Always verify before committing:**
   ```bash
   git ls-files --cached
   ```
   *(Ensure only `.py`, `.yaml`, configs, and documentation appear in the list).*
