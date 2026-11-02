"""Tests for the Hugging Face CPU SLM integration.

Validates the model file integrity, downloader verification, presets,
and the CPUSLMClient interface.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import pytest

# Add gateway root to sys.path
GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATEWAY_ROOT))

from gateway.clients.cpu_slm_client import CPUSLMClient
from pipeline.download_slm import PRESETS, verify_gguf_file, GGUF_MAGIC


def test_presets_configuration():
    """Verify built-in Hugging Face SLM presets."""
    assert "qwen-0.5b" in PRESETS
    assert "smollm-135m" in PRESETS
    assert "qwen-1.5b" in PRESETS

    for key, p in PRESETS.items():
        assert "repo_id" in p and "/" in p["repo_id"]
        assert "filename" in p and p["filename"].endswith(".gguf")
        assert "description" in p


def test_gguf_verification(tmp_path: Path):
    """Test GGUF magic header verification logic."""
    # Invalid empty file
    f_empty = tmp_path / "empty.gguf"
    f_empty.touch()
    assert verify_gguf_file(f_empty) is False

    # Invalid corrupt file
    f_bad = tmp_path / "bad.gguf"
    f_bad.write_bytes(b"NOT_A_GGUF" * 200)
    assert verify_gguf_file(f_bad) is False

    # Valid mock GGUF file
    f_valid = tmp_path / "mock.gguf"
    f_valid.write_bytes(GGUF_MAGIC + b"\x00" * 2000)
    assert verify_gguf_file(f_valid) is True


def test_downloaded_slm_model_integrity():
    """Verify that the required 1.5B SLM model is provisioned locally."""
    model_path = GATEWAY_ROOT / "models" / "slm_gguf" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    assert model_path.exists(), f"Expected model file at {model_path}"
    assert model_path.stat().st_size > 100 * 1024 * 1024, "Model should be > 100 MB"
    assert verify_gguf_file(model_path) is True, "Model file header must be valid GGUF"


@pytest.mark.asyncio
async def test_cpu_slm_client_refuses_offline_generation():
    """Offline inference must fail rather than poison a grounded answer cache."""
    client = CPUSLMClient(
        base_url="http://127.0.0.1:54321/v1",  # non-existent port
        model_name="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        timeout=1.0,
    )
    is_healthy = await client.check_health()
    assert is_healthy is False

    with pytest.raises(RuntimeError, match="CPU SLM is unavailable"):
        await client.generate_chat([
            {"role": "user", "content": "What is reinforcement learning?"}
        ])

    await client.close()


def main():
    import tempfile

    print("Running CPU SLM unit tests...")
    test_presets_configuration()
    print("  ✓ test_presets_configuration passed")

    with tempfile.TemporaryDirectory() as td:
        test_gguf_verification(Path(td))
    print("  ✓ test_gguf_verification passed")

    test_downloaded_slm_model_integrity()
    print("  ✓ test_downloaded_slm_model_integrity passed")

    asyncio.run(test_cpu_slm_client_refuses_offline_generation())
    print("  ✓ test_cpu_slm_client_refuses_offline_generation passed")

    print("\nAll CPU SLM tests passed successfully!")


if __name__ == "__main__":
    main()

