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
- **`run_slurm.sh`**:
  Slurm batch job submission script designed to run high-throughput dataset generation across multiple GPUs with automatic memory detection, temporary RAM-disk caching, and logging to `logs/llm_dataset.log`.

