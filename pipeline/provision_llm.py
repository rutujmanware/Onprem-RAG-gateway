"""Provisioning utility for the on-prem Qwen 2.5 7B GPU Large Language Model.

Downloads quantized Qwen2.5 7B weights (AWQ / GPTQ / GGUF) from Hugging Face
into models/llm_7b/ for zero-egress, high-throughput vLLM serving on campus hardware.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, Optional

QUANTIZED_PRESETS: Dict[str, Dict[str, str]] = {
    "awq": {
        "repo_id": "Qwen/Qwen2.5-7B-Instruct-AWQ",
        "description": "Official Qwen 2.5 7B Instruct 4-bit AWQ (~5.5 GB, optimal for vLLM GPU inference)",
        "quant_type": "awq",
    },
    "gptq-4bit": {
        "repo_id": "Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4",
        "description": "Official Qwen 2.5 7B Instruct 4-bit GPTQ (~5.5 GB)",
        "quant_type": "gptq",
    },
    "gptq-8bit": {
        "repo_id": "Qwen/Qwen2.5-7B-Instruct-GPTQ-Int8",
        "description": "Official Qwen 2.5 7B Instruct 8-bit GPTQ (~8.0 GB)",
        "quant_type": "gptq",
    },
}

DEFAULT_PRESET = "awq"
DEFAULT_DESTINATION = Path("models/llm_7b")


def provision_qwen_7b(
    repo_id: str,
    output_dir: Path,
    max_workers: int = 4,
) -> Path:
    """Downloads model weights and configuration files into output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[PROVISION-LLM] Provisioning 7B Quantized Model:")
    print(f"  Hugging Face Repo: {repo_id}")
    print(f"  Target Directory : {output_dir.resolve()}")
    print(f"  Max Workers      : {max_workers}\n")

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("[ERROR] huggingface_hub is not installed in the active environment.")
        print("Install it with: pip install huggingface_hub")
        sys.exit(1)

    downloaded_path = snapshot_download(
        repo_id=repo_id,
        local_dir=str(output_dir),
        max_workers=max_workers,
    )

    print(f"\n[PROVISION-LLM] Successfully provisioned {repo_id} at {downloaded_path}")
    return Path(downloaded_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision Qwen 2.5 7B quantized model for on-prem vLLM GPU tier"
    )
    parser.add_argument(
        "--preset",
        choices=list(QUANTIZED_PRESETS.keys()),
        default=DEFAULT_PRESET,
        help=f"Quantization preset to download (default: {DEFAULT_PRESET})",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Custom Hugging Face repo ID (overrides --preset)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_DESTINATION,
        help=f"Local target directory (default: {DEFAULT_DESTINATION})",
    )
    parser.add_argument(
        "--list-presets",
        action="store_true",
        help="List available quantized presets and exit",
    )

    args = parser.parse_args()

    if args.list_presets:
        print("\nAvailable Quantized Presets for Qwen 2.5 7B:")
        for name, info in QUANTIZED_PRESETS.items():
            print(f"  - {name:10}: {info['description']} [{info['repo_id']}]")
        print()
        return

    repo_id = args.model or QUANTIZED_PRESETS[args.preset]["repo_id"]
    provision_qwen_7b(repo_id=repo_id, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
