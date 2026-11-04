#!/usr/bin/env python3
"""Live interactive tracker for Redis Stack database loading & persistence.

Continuously polls Redis Persistence stats and displays a live terminal
dashboard with progress bar, loaded bytes, key counts, and ETA.
Auto-exits when loading reaches 100%.

Usage:
    python scripts/track_redis.py
    make redis-watch
"""

import os
import sys
import time
import subprocess
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT_DIR = Path(__file__).resolve().parent.parent
REDIS_CLI = ROOT_DIR / "redis-stack-server-7.2.0-v9" / "bin" / "redis-cli"
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")


def get_persistence_info() -> dict:
    cmd = [str(REDIS_CLI), "info", "Persistence"]
    env = os.environ.copy()
    env["REDISCLI_AUTH"] = REDIS_PASSWORD
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=3.0,
        )
        if proc.returncode != 0:
            return {"error": proc.stderr.strip() or "Connection error"}
        
        info = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ":" in line:
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip()
        return info
    except Exception as e:
        return {"error": str(e)}


def format_bytes(b: int) -> str:
    gb = b / (1024 ** 3)
    if gb >= 1.0:
        return f"{gb:.2f} GB"
    mb = b / (1024 ** 2)
    return f"{mb:.1f} MB"


def format_seconds(s: int) -> str:
    if s < 60:
        return f"{s}s"
    m = s // 60
    sec = s % 60
    if m < 60:
        return f"{m}m {sec}s"
    h = m // 60
    rem_m = m % 60
    return f"{h}h {rem_m}m"


def render_progress_bar(perc: float, width: int = 35) -> str:
    filled = int(width * (perc / 100.0))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {perc:.2f}%"


def main():
    print("\033[?25l", end="", flush=True)  # Hide cursor
    try:
        while True:
            info = get_persistence_info()
            if "error" in info:
                sys.stdout.write(f"\r\033[K🔴 Redis Offline or Unreachable: {info['error']} (Retrying...)")
                sys.stdout.flush()
                time.sleep(1.5)
                continue

            is_loading = info.get("loading") == "1"
            if not is_loading:
                sys.stdout.write("\n" + "=" * 65 + "\n")
                sys.stdout.write("🎉 REDIS DATASET IS FULLY LOADED & READY FOR QUERIES!\n")
                sys.stdout.write("=" * 65 + "\n")
                sys.stdout.flush()
                break

            perc = float(info.get("loading_loaded_perc", 0.0))
            loaded_b = int(info.get("loading_loaded_bytes", 0))
            total_b = int(info.get("loading_total_bytes", 1))
            eta_s = int(info.get("loading_eta_seconds", 0))
            keys_loaded = int(info.get("rdb_last_load_keys_loaded", 0))

            bar_str = render_progress_bar(perc)
            loaded_str = f"{format_bytes(loaded_b)} / {format_bytes(total_b)}"
            eta_str = format_seconds(eta_s)
            keys_str = f"{keys_loaded:,} keys"

            # Clean ANSI screen redraw
            sys.stdout.write("\033[H\033[J")
            sys.stdout.write("=" * 65 + "\n")
            sys.stdout.write("📊 REDIS STACK 12GB DATASET LOADING MONITOR (LIVE)\n")
            sys.stdout.write("=" * 65 + "\n\n")
            sys.stdout.write(f"  Progress:   {bar_str}\n")
            sys.stdout.write(f"  Data:       {loaded_str}\n")
            sys.stdout.write(f"  Records:    {keys_str} processed\n")
            sys.stdout.write(f"  Time Left:  ~{eta_str} remaining\n\n")
            sys.stdout.write("  Status:     Loading RDB vectors & thesis chunk graph into RAM\n")
            sys.stdout.write("=" * 65 + "\n")
            sys.stdout.write("💡 Press Ctrl+C anytime to exit monitor (Redis will keep loading)\n")
            sys.stdout.flush()

            time.sleep(1.0)
    except KeyboardInterrupt:
        sys.stdout.write("\n\n👋 Stopped live monitor. Redis daemon is continuing to load.\n")
    finally:
        print("\033[?25h", end="", flush=True)  # Restore cursor


if __name__ == "__main__":
    main()

