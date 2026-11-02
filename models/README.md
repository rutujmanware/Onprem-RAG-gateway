# Models Directory

Stores local weights and configurations for zero-egress inference on campus hardware.

---

## 1. GPU Large Language Model (7B Tier)

- **Attached Model:** `Qwen/Qwen2.5-7B-Instruct-AWQ`
- **Location:** `models/llm_7b/`
- **Architecture:** `Qwen2ForCausalLM` (7 Billion parameters)
- **Quantization:** `AWQ` (4-bit activation-aware weight quantization, ~5.5 GB total)
- **Serving Engine:** `vLLM` container (`onprem-rag-vllm` on port 8000) with PagedAttention and continuous batching
- **Configuration:** `configs/vllm_config.yaml`
- **Routing Target:** Queries classified as high-complexity ($\text{score} > \alpha$) by the complexity router.

### Provisioning the 7B Model

```bash
# Provision Qwen 2.5 7B Instruct AWQ (default 4-bit, ~5.5 GB)
make provision-llm

# Or use specific presets:
python3 -m pipeline.provision_llm --preset awq         # Official 4-bit AWQ (~5.5 GB, optimal for vLLM)
python3 -m pipeline.provision_llm --preset gptq-4bit   # Official 4-bit GPTQ (~5.5 GB)
python3 -m pipeline.provision_llm --preset gptq-8bit   # Official 8-bit GPTQ (~8.0 GB)
```

---

## 2. CPU Small Language Model (SLM Tier)

- **Default Model:** `Qwen/Qwen2.5-1.5B-Instruct-GGUF` (or `Qwen/Qwen2.5-0.5B-Instruct-GGUF`)
- **File:** `models/slm_gguf/qwen2.5-1.5b-instruct-q4_k_m.gguf` (symlinked as `model.gguf`)
- **Architecture:** `qwen2`
- **Quantization:** `Q4_K_M` (~1.1 GB)
- **Engine:** `llama.cpp` server (Docker container `cpu-slm` on port 8082) or local Python runtime
- **Configuration:** `configs/slm_config.yaml`
- **Routing Target:** Low-complexity queries ($\text{score} \le \alpha$) for rapid CPU inference.

### Download / Switch Presets

```bash
make download-slm
# Or specific presets:
python3 -m pipeline.download_slm --preset qwen-0.5b    # 468 MB
python3 -m pipeline.download_slm --preset smollm-135m  # 105 MB
python3 -m pipeline.download_slm --preset qwen-1.5b    # 1.1 GB
```
