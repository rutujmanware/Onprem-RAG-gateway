# Technical Documentation

This directory contains technical guides, hardware tuning notes, and architectural specifications:

- **`OPTIMIZATION_GUIDE.md`**:
  Comprehensive architecture and CPU SLM optimization guide covering:
  - System architecture and hybrid CPU/GPU routing topology.
  - CPU SLM performance engineering on AMD EPYC (Zen 3) architectures.
  - Precision selection (AVX2 `float32` vs emulated `bfloat16`).
  - Thread pooling, NUMA pinning, and continuous batching parameters.
  - End-to-end benchmarking methodologies and results.

