from __future__ import annotations

import os
from pathlib import Path
import socket
from typing import Optional


def load_env_files() -> None:
    """Load key-value pairs from .env if not already set in os.environ."""
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[1] / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    for env_path in candidates:
        if env_path.exists():
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


def resolve_redis_url(redis_url: Optional[str] = None) -> str:
    """Resolve Redis connection URL with credentials and host fallbacks."""
    load_env_files()

    url = redis_url or os.getenv("REDIS_URL", "redis://:Redis*2703!@localhost:6379/0")
    password = os.getenv("REDIS_PASSWORD", "Redis*2703!")

    # Inject password if missing from URL
    if password and "@" not in url and url.startswith("redis://"):
        url = f"redis://:{password}@" + url[len("redis://"):]

    # Handle docker-internal host 'redis' when running on local host
    if "@redis:6379" in url or url.startswith("redis://redis:6379"):
        try:
            socket.gethostbyname("redis")
        except socket.gaierror:
            url = url.replace("@redis:6379", "@localhost:6379").replace(
                "redis://redis:6379", "redis://localhost:6379"
            )

    return url
