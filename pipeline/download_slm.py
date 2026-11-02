"""Hugging Face Model Downloader for Edge CPU SLM.

Downloads lightweight Hugging Face Small Language Models (GGUF format)
for zero-egress, low-latency CPU inference with llama.cpp or local runtime.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

# Supported lightweight Hugging Face SLM presets
PRESETS: Dict[str, Dict[str, str]] = {
    "qwen-0.5b": {
        "repo_id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "filename": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "description": "Qwen 2.5 0.5B Instruct (Q4_K_M quantization, ~468 MB, high quality)",
    },
    "smollm-135m": {
        "repo_id": "bartowski/SmolLM2-135M-Instruct-GGUF",
        "filename": "SmolLM2-135M-Instruct-Q4_K_M.gguf",
        "description": "SmolLM2 135M Instruct (Q4_K_M quantization, ~105 MB, ultra-compact)",
    },
    "qwen-1.5b": {
        "repo_id": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "description": "Qwen 2.5 1.5B Instruct (Q4_K_M quantization, ~1.1 GB, top tier)",
    },
}

DEFAULT_PRESET = "qwen-1.5b"
GGUF_MAGIC = b"GGUF"


def verify_gguf_file(file_path: Path) -> bool:
    """Check if file exists, is non-empty, and starts with GGUF magic bytes."""
    if not file_path.is_file() or file_path.stat().st_size < 1024:
        return False
    try:
        with open(file_path, "rb") as f:
            header = f.read(4)
            return header == GGUF_MAGIC
    except Exception:
        return False


def download_file_with_progress(url: str, dest_path: Path, expected_filename: str) -> None:
    """Download a file with an interactive progress indicator."""
    temp_path = dest_path.with_suffix(".tmp")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n[SLM-DOWNLOAD] Downloading Hugging Face model:")
    print(f"  Source URL : {url}")
    print(f"  Target File: {dest_path}")

    start_time = time.time()

    def report_progress(block_num: int, block_size: int, total_size: int):
        downloaded = block_num * block_size
        elapsed = time.time() - start_time
        speed = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0

        if total_size > 0:
            percent = min(100.0, downloaded * 100.0 / total_size)
            mb_downloaded = downloaded / (1024 * 1024)
            mb_total = total_size / (1024 * 1024)
            sys.stdout.write(
                f"\r  [{percent:5.1f}%] {mb_downloaded:6.1f} MB / {mb_total:6.1f} MB | {speed:5.1f} MB/s"
            )
        else:
            mb_downloaded = downloaded / (1024 * 1024)
            sys.stdout.write(f"\r  Downloaded: {mb_downloaded:6.1f} MB | {speed:5.1f} MB/s")
        sys.stdout.flush()

    try:
        # User-Agent header is required for Hugging Face CDN requests
        opener = urllib.request.build_opener()
        opener.addheaders = [("User-Agent", "Mozilla/5.0 (compatible; OnpremRAG/1.0)")]
        urllib.request.install_opener(opener)

        urllib.request.urlretrieve(url, str(temp_path), reporthook=report_progress)
        print()  # newline after progress

        # Verify downloaded model header
        if not verify_gguf_file(temp_path):
            raise ValueError(
                f"Downloaded file {temp_path} is corrupt or not a valid GGUF binary."
            )

        # Move temp to final destination
        if dest_path.exists():
            dest_path.unlink()
        temp_path.rename(dest_path)
        total_time = time.time() - start_time
        final_size_mb = dest_path.stat().st_size / (1024 * 1024)
        print(
            f"[SLM-DOWNLOAD] Successfully downloaded {final_size_mb:.1f} MB in {total_time:.1f}s."
        )

    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise RuntimeError(f"Download failed: {e}") from e


def setup_slm_model(
    preset_name: Optional[str] = None,
    repo_id: Optional[str] = None,
    filename: Optional[str] = None,
    output_dir: Optional[str] = None,
    link_as_default: bool = True,
    force: bool = False,
) -> Path:
    """Download and configure a small Hugging Face model for CPU SLM inference."""
    preset = PRESETS.get(preset_name or DEFAULT_PRESET)

    final_repo = repo_id or (preset["repo_id"] if preset else "Qwen/Qwen2.5-1.5B-Instruct-GGUF")
    final_filename = filename or (
        preset["filename"] if preset else "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    )

    base_dir = Path(output_dir or Path(__file__).resolve().parents[1] / "models" / "slm_gguf")
    base_dir.mkdir(parents=True, exist_ok=True)

    dest_file = base_dir / final_filename
    default_link = base_dir / "model.gguf"

    # Direct Hugging Face download URL
    hf_url = f"https://huggingface.co/{final_repo}/resolve/main/{final_filename}"

    if dest_file.exists() and verify_gguf_file(dest_file) and not force:
        print(f"[SLM-DOWNLOAD] Model file already exists and is valid: {dest_file}")
    else:
        download_file_with_progress(hf_url, dest_file, final_filename)

    # Link or copy to model.gguf for docker-compose and configs compatibility
    if link_as_default:
        if default_link.exists() or default_link.is_symlink():
            default_link.unlink()
        try:
            # Create relative symlink
            default_link.symlink_to(dest_file.name)
            print(f"[SLM-DOWNLOAD] Created symlink: {default_link} -> {dest_file.name}")
        except OSError:
            # Fallback to copy if filesystem doesn't support symlinks
            import shutil

            shutil.copy2(dest_file, default_link)
            print(f"[SLM-DOWNLOAD] Copied model to default path: {default_link}")

    return dest_file


def main():
    parser = argparse.ArgumentParser(
        description="Download small Hugging Face GGUF models for Onprem RAG CPU SLM"
    )
    parser.add_argument(
        "--preset",
        choices=list(PRESETS.keys()),
        default=DEFAULT_PRESET,
        help=f"Model preset to download (default: {DEFAULT_PRESET})",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Custom Hugging Face repo ID (e.g. Qwen/Qwen2.5-0.5B-Instruct-GGUF)",
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="Custom GGUF filename inside repo (e.g. qwen2.5-0.5b-instruct-q4_k_m.gguf)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Target output directory (default: models/slm_gguf)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force redownload even if file exists",
    )
    parser.add_argument(
        "--list-presets",
        action="store_true",
        help="List available model presets and exit",
    )

    args = parser.parse_args()

    if args.list_presets:
        print("\nAvailable Hugging Face SLM Presets:")
        for name, info in PRESETS.items():
            print(f"  - {name:12}: {info['description']} [{info['repo_id']}]")
        print()
        return

    setup_slm_model(
        preset_name=args.preset,
        repo_id=args.repo,
        filename=args.file,
        output_dir=args.output_dir,
        force=args.force,
    )


if __name__ == "__main__":
    main()

