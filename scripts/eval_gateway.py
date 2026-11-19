"""Isolated measurement gateway for the evaluation scripts (NFR-3/4/5, configurations C0-C2).

Starts a second gateway on a loopback port with its own semantic-cache index and key prefixes, so measurements
never read or write production cache entries; `stop()` terminates it and deletes everything it stored
(SCAN + DEL on the prefixes, FT.DROPINDEX on its index; never FT.INFO or KEYS, see PRD L19).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


class EvalGateway:
    def __init__(self, port: int = 8099, workers: int = 2, env: Optional[Dict[str, str]] = None, tag: str = "eval"):
        self.port, self.workers, self.tag = port, workers, tag
        self.index = f"{tag}cache_idx"
        self.cache_prefix, self.regen_prefix = f"{tag}cache:", f"{tag}regen:"
        self.env = {**os.environ, "REDIS_CACHE_INDEX_NAME": self.index, "REDIS_CACHE_KEY_PREFIX": self.cache_prefix,
                    "REDIS_REGEN_KEY_PREFIX": self.regen_prefix, **(env or {})}
        self.proc: Optional[subprocess.Popen] = None
        self.url = f"http://127.0.0.1:{port}"
        self.log = ROOT / "logs" / f"eval_gateway_{tag}_{port}.log"

    def start(self, timeout: float = 180) -> "EvalGateway":
        cmd = [PYTHON, "-m", "uvicorn", "gateway.main:app", "--host", "127.0.0.1", "--port", str(self.port),
               "--workers", str(self.workers), "--no-access-log", "--no-proxy-headers"]
        self.proc = subprocess.Popen(cmd, cwd=str(ROOT), env=self.env, stdout=open(self.log, "a"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if httpx.get(f"{self.url}/ready", timeout=5).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(f"measurement gateway exited; see {self.log}")
            time.sleep(2)
        self.stop()
        raise RuntimeError(f"measurement gateway not ready after {timeout}s; see {self.log}")

    def stop(self, cleanup: bool = True):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        if cleanup:
            self.cleanup()

    def cleanup(self):
        import redis
        from dotenv import dotenv_values
        url = {**dotenv_values(ROOT / ".env"), **os.environ}.get("REDIS_URL", "redis://localhost:6379/0")
        r = redis.Redis.from_url(url, socket_timeout=30)
        n = 0
        for prefix in (self.cache_prefix, self.regen_prefix):
            batch = []
            for k in r.scan_iter(match=f"{prefix}*", count=1000):
                batch.append(k)
                if len(batch) >= 500:
                    n += r.delete(*batch)
                    batch = []
            if batch:
                n += r.delete(*batch)
        try:
            if self.index.encode() in r.execute_command("FT._LIST"):
                r.execute_command("FT.DROPINDEX", self.index)
        except Exception as exc:
            print(f"warning: could not drop {self.index}: {exc}")
        print(f"[eval gateway] removed {n} keys with prefixes {self.cache_prefix!r}/{self.regen_prefix!r}, index {self.index}")

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
