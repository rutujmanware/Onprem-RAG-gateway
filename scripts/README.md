# Utility & Operational Scripts

This directory contains standalone execution, cluster batch, and data engineering scripts:

- **`dataset_generator.py`**:
  Generates synthetic multi-chunk query datasets by querying Redis for indexed document chunks, assembling intra-doc and cross-doc contexts, and synthesizing queries using vLLM. Outputs are written directly to `data/router_multichunk_training_dataset.csv` and `.jsonl`.
- **`query.py`**:
  Convenient CLI query client for testing the running Gateway. Supports one-off queries, formatted terminal answers with citation metadata, and interactive chat loop mode without raw JSON or vector noise.
- **`run_slurm.sh`**:
  Slurm batch job submission script designed to run high-throughput dataset generation across multiple GPUs with automatic memory detection, temporary RAM-disk caching, and logging to `logs/llm_dataset.log`.

