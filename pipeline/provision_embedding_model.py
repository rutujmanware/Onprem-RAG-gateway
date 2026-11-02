"""One-time provisioning utility for the local MiniLM embedding model.

This command is intentionally separate from serving. The gateway runs with
local_files_only=True and therefore never downloads a model at request time.
"""

from pathlib import Path


MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
DESTINATION = Path("models/embeddings/all-MiniLM-L6-v2")


def main() -> None:
    from huggingface_hub import snapshot_download

    DESTINATION.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=MODEL_ID,
        local_dir=str(DESTINATION),
        local_dir_use_symlinks=False,
    )
    print(f"Provisioned {MODEL_ID} at {DESTINATION}")


if __name__ == "__main__":
    main()
