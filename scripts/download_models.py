#!/usr/bin/env python3
"""Model Provisioning & Download Utility for On-Premise Institutional RAG Gateway.

Automatically downloads and provisions local offline AI models into their standard locations:
1. Dense Vector Embedder:
   - Model: sentence-transformers/all-MiniLM-L6-v2
   - Destination: models/embeddings/all-MiniLM-L6-v2
2. Tier-1 CPU Small Language Model (SLM):
   - Model: Qwen/Qwen2.5-3B-Instruct-GGUF (qwen2.5-3b-instruct-q4_k_m.gguf, ~2.0 GB)
   - Destination: models/slm_gguf/qwen2.5-3b-instruct-q4_k_m.gguf
3. Tier-2 GPU Large Language Model (vLLM / PyTorch):
   - Model: Qwen/Qwen2.5-7B-Instruct (bfloat16 safetensors, ~15.2 GB)
   - Destination: models/llm_7b/qwen2.5-7b-instruct

Usage:
    python scripts/download_models.py                 # Download all required models
    python scripts/download_models.py --embeddings    # Only download embedding model
    python scripts/download_models.py --slm           # Only download 3B GGUF SLM model
    python scripts/download_models.py --gpu-llm       # Only download 7B GPU model
    python scripts/download_models.py --force         # Re-download even if already present
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from huggingface_hub import hf_hub_download, snapshot_download
except ImportError:
    print("❌ Error: 'huggingface_hub' is required. Please install it:")
    print("   pip install huggingface_hub")
    sys.exit(1)


# Target model repositories & directories
EMBEDDING_REPO = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIR = ROOT_DIR / "models" / "embeddings" / "all-MiniLM-L6-v2"

SLM_REPO = "Qwen/Qwen2.5-3B-Instruct-GGUF"
SLM_FILENAME = "qwen2.5-3b-instruct-q4_k_m.gguf"
SLM_DIR = ROOT_DIR / "models" / "slm_gguf"
SLM_TARGET_PATH = SLM_DIR / SLM_FILENAME

GPU_REPO = "Qwen/Qwen2.5-7B-Instruct"
GPU_DIR = ROOT_DIR / "models" / "llm_7b" / "qwen2.5-7b-instruct"


def format_bytes(num_bytes: int) -> str:
    """Format bytes into human-readable string (KB, MB, GB)."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"


def get_path_size(path: Path) -> int:
    """Calculate total byte size of a file or directory tree, resolving symlinks."""
    try:
        resolved = path.resolve()
        if not resolved.exists():
            return 0
        if resolved.is_file():
            return resolved.stat().st_size
        total = 0
        for root, _, files in os.walk(resolved):
            for f in files:
                fp = Path(root) / f
                try:
                    total += fp.stat().st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def download_embedding_model(force: bool = False) -> bool:
    """Download the dense SentenceTransformer embedding model."""
    print("\n" + "=" * 70)
    print(f"📦 [1/3] Provisioning Embedding Model: {EMBEDDING_REPO}")
    print("=" * 70)
    print(f"Target Directory: {EMBEDDING_DIR}")

    if EMBEDDING_DIR.exists() and not force:
        size = get_path_size(EMBEDDING_DIR)
        config_file = EMBEDDING_DIR / "config.json"
        if (config_file.exists() or (EMBEDDING_DIR / "pytorch_model.bin").exists()) and size > 50 * 1024 * 1024:
            print(f"✅ Embedding model already present ({format_bytes(size)}). Skipping download.")
            return True

    print(f"⬇️ Downloading snapshot from HuggingFace Hub: {EMBEDDING_REPO}...")
    t0 = time.time()
    try:
        EMBEDDING_DIR.parent.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=EMBEDDING_REPO,
            local_dir=str(EMBEDDING_DIR),
        )
        elapsed = time.time() - t0
        size = get_path_size(EMBEDDING_DIR)
        print(f"✅ Successfully provisioned Embeddings in {elapsed:.1f}s ({format_bytes(size)})")
        return True
    except Exception as e:
        print(f"❌ Failed to download embedding model: {e}")
        return False


def download_slm_model(force: bool = False) -> bool:
    """Download the quantized CPU SLM GGUF model."""
    print("\n" + "=" * 70)
    print(f"⚡ [2/3] Provisioning Tier-1 CPU SLM: {SLM_REPO}/{SLM_FILENAME}")
    print("=" * 70)
    print(f"Target File: {SLM_TARGET_PATH}")

    if SLM_TARGET_PATH.exists() and not force:
        size = get_path_size(SLM_TARGET_PATH)
        # Qwen 2.5 3B Q4_K_M is approx 1.9 - 2.1 GB
        if size > 1.5 * 1024 * 1024 * 1024:
            print(f"✅ CPU SLM GGUF model already present ({format_bytes(size)}). Skipping download.")
            return True

    SLM_DIR.mkdir(parents=True, exist_ok=True)
    if SLM_TARGET_PATH.is_symlink() and (force or not SLM_TARGET_PATH.exists()):
        SLM_TARGET_PATH.unlink()
    print(f"⬇️ Downloading GGUF weights ({SLM_FILENAME}) from HuggingFace Hub...")
    t0 = time.time()
    try:
        downloaded_path = hf_hub_download(
            repo_id=SLM_REPO,
            filename=SLM_FILENAME,
            local_dir=str(SLM_DIR),
        )
        elapsed = time.time() - t0
        size = get_path_size(Path(downloaded_path))
        print(f"✅ Successfully provisioned CPU SLM GGUF in {elapsed:.1f}s ({format_bytes(size)})")
        return True
    except Exception as e:
        print(f"❌ Failed to download CPU SLM GGUF: {e}")
        return False


def download_gpu_llm_model(force: bool = False) -> bool:
    """Download the Tier-2 7B GPU model in bfloat16 safetensors format."""
    print("\n" + "=" * 70)
    print(f"🔥 [3/3] Provisioning Tier-2 GPU LLM: {GPU_REPO}")
    print("=" * 70)
    print(f"Target Directory: {GPU_DIR}")

    if GPU_DIR.exists() and not force:
        size = get_path_size(GPU_DIR)
        config_file = GPU_DIR / "config.json"
        safetensors = list(GPU_DIR.glob("*.safetensors"))
        # 7B model in bfloat16 is ~14 - 16 GB with 4 safetensors shards
        if config_file.exists() and (len(safetensors) >= 4 or size > 10 * 1024 * 1024 * 1024):
            print(f"✅ GPU LLM (7B) already present ({format_bytes(size)}, {len(safetensors)} shards). Skipping download.")
            return True

    print(f"⬇️ Downloading 7B bfloat16 weights from HuggingFace Hub: {GPU_REPO}...")
    print("   (Note: Total size is ~15.2 GB. Download time depends on network bandwidth)")
    t0 = time.time()
    try:
        GPU_DIR.parent.mkdir(parents=True, exist_ok=True)
        # If it's a symlink that needs re-downloading under force, remove link first
        if GPU_DIR.is_symlink():
            GPU_DIR.unlink()
            GPU_DIR.mkdir(parents=True, exist_ok=True)

        snapshot_download(
            repo_id=GPU_REPO,
            local_dir=str(GPU_DIR),
            ignore_patterns=["*.pt", "*.bin", "*.onnx"],  # Prefer safetensors
        )
        elapsed = time.time() - t0
        size = get_path_size(GPU_DIR)
        print(f"✅ Successfully provisioned GPU LLM in {elapsed:.1f}s ({format_bytes(size)})")
        return True
    except Exception as e:
        print(f"❌ Failed to download GPU LLM: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Download and provision offline local models for AskIISc RAG Gateway",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--embeddings",
        action="store_true",
        help="Download only dense embedding model (all-MiniLM-L6-v2)",
    )
    parser.add_argument(
        "--slm",
        action="store_true",
        help="Download only Tier-1 CPU SLM GGUF model (Qwen 2.5 3B Q4_K_M)",
    )
    parser.add_argument(
        "--gpu-llm",
        action="store_true",
        help="Download only Tier-2 GPU LLM model (Qwen 2.5 7B Instruct)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download even if model files already exist",
    )

    args = parser.parse_args()

    # If no specific model flag is given, download all models
    download_all = not (args.embeddings or args.slm or args.gpu_llm)

    print("\n" + "=" * 70)
    print("🚀 ASKIISC ON-PREMISE RAG GATEWAY - MODEL PROVISIONER")
    print("=" * 70)
    print(f"Models Directory Base: {ROOT_DIR / 'models'}")

    results = []

    if download_all or args.embeddings:
        ok = download_embedding_model(force=args.force)
        results.append(("Embeddings (all-MiniLM-L6-v2)", ok))

    if download_all or args.slm:
        ok = download_slm_model(force=args.force)
        results.append(("CPU SLM (Qwen 2.5 3B GGUF)", ok))

    if download_all or args.gpu_llm:
        ok = download_gpu_llm_model(force=args.force)
        results.append(("GPU LLM (Qwen 2.5 7B Instruct)", ok))

    print("\n" + "=" * 70)
    print("📋 PROVISIONING SUMMARY")
    print("=" * 70)
    all_ok = True
    for name, success in results:
        status_tag = "✅ READY" if success else "❌ FAILED"
        print(f"  {status_tag:<10} | {name}")
        if not success:
            all_ok = False

    print("=" * 70)
    if all_ok:
        print("🎉 All requested models are verified and ready for offline inference!")
        sys.exit(0)
    else:
        print("⚠️ Some models failed to download. Please check network connectivity and retry.")
        sys.exit(1)


if __name__ == "__main__":
    main()
