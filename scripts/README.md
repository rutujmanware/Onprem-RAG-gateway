# Utility & Operational Scripts

This directory contains standalone execution, cluster batch, and data engineering scripts:

- **`download_models.py`**:
  Automated model provisioner that downloads offline HuggingFace models directly into `models/` (Dense embeddings, 3B CPU SLM GGUF, and 7B GPU bfloat16 safetensors). Can be triggered via `make download-models`.
- **`dataset_generator.py`**:
  Generates synthetic multi-chunk query datasets by querying Redis for indexed document chunks, assembling intra-doc and cross-doc contexts, and synthesizing queries using vLLM. Outputs are written directly to `data/router_multichunk_training_dataset.csv` and `.jsonl`.
- **`query.py`**:
  Convenient CLI query client for testing the running Gateway. Supports one-off queries, formatted terminal answers with citation metadata, and interactive chat loop mode without raw JSON or vector noise.
- **`eval_router_threshold.py`**:
  Production threshold sweep and complexity evaluation script benchmarks query routing accuracy across the 20,400 query validation set.
- **`load_test.py`**:
  Asynchronous multi-scenario concurrency benchmark measuring RPS, latency percentiles (p50, p95, p99, max), VRAM, and cache hit rates.
- **`run_100_uncached_load_test.py`**:
  Cold-cache stress testing tool forcing 100 consecutive cache misses across the 2.26M chunk vector database.
- **`serve_vllm.py`**:
  Hardened launcher for the vLLM 0.6.3 GPU server (port 8000). Forces the `--host` loopback bind that stock 0.6.3 ignores, refuses non-loopback hosts, and refuses to start without `VLLM_API_KEY`. Used by `make serve-gpu` and `prod_stack.py`; never start vLLM with `python -m vllm.entrypoints.openai.api_server` directly.
- **`check_vllm_security.sh`**:
  Post-restart verification (`make vllm-check`): loopback-only bind, 401 without/with a wrong key, 200 with `VLLM_API_KEY`, unreachable via the host's network IPs.
- **`prod_stack.py`**:
  Production orchestrator (`make prod-up/prod-status/prod-restart/prod-down`). `--with-vllm` also stops/restarts the vLLM engine (process-group stop so GPU memory is released); `VLLM_GPU` in `.env` selects the GPU.
- **`run_slurm.sh`**:
  Slurm batch job submission script designed to run high-throughput dataset generation across multiple GPUs with automatic memory detection, temporary RAM-disk caching, and logging to `logs/llm_dataset.log`.

