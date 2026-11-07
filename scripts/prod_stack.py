#!/usr/bin/env python3
"""Production Stack Orchestrator for On-Premise Institutional RAG Gateway.

Manages the lifecycle of all 4 decoupled production services:
1. Redis Stack Server (Port 6379)
2. vLLM GPU Server - Qwen 2.5 7B on NVIDIA A100 (Port 8000)
3. CPU SLM Server - Qwen 2.5 3B GGUF with AVX-512 (Port 8082)
4. Institutional API Gateway - Multi-worker FastAPI (Port 8080)

Usage:
    python scripts/prod_stack.py start
    python scripts/prod_stack.py status
    python scripts/prod_stack.py stop
    python scripts/prod_stack.py restart
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT_DIR / "logs"
PID_FILE = LOGS_DIR / "prod_pids.json"
PYTHON_BIN = sys.executable

# Default configuration
REDIS_SERVER_BIN = ROOT_DIR / "redis-stack-server-7.2.0-v9" / "bin" / "redis-stack-server"
REDIS_PORT = 6379
VLLM_PORT = 8000
SLM_PORT = 8082
ROUTER_PORT = 8090
GATEWAY_PORT = 8080


def ensure_dirs():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def load_pids() -> Dict[str, int]:
    if PID_FILE.exists():
        try:
            with open(PID_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_pids(pids: Dict[str, int]):
    with open(PID_FILE, "w") as f:
        json.dump(pids, f, indent=2)


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def check_port_health(port: int, path: str = "/healthz") -> Tuple[str, str]:
    if port == REDIS_PORT:
        try:
            import redis
            redis_pw = os.getenv("REDIS_PASSWORD") or None
            r = redis.Redis(host="127.0.0.1", port=REDIS_PORT, password=redis_pw, socket_timeout=2.0)
            info = r.info("Persistence")
            is_loading = str(info.get("loading", "0")) == "1"
            if is_loading:
                perc = info.get("loading_loaded_perc", "0.0")
                eta = info.get("loading_eta_seconds", "?")
                return "LOADING", f"Loading dataset into RAM ({perc}% - ETA: {eta}s)"
            r.ping()
            return "READY", "Redis Ready (PONG)"
        except Exception as e:
            err = str(e)
            if "loading the dataset" in err.lower():
                return "LOADING", "Loading dataset into RAM (Wait for completion)"
            return "DOWN", err

    url = f"http://127.0.0.1:{port}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ProdStackHealthCheck"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                return "READY", f"HTTP {resp.status} OK"
    except Exception as e:
        return "DOWN", str(e)
    return "DOWN", "Unreachable"


def start_service(name: str, cmd: list[str], env: Optional[dict] = None) -> int:
    log_file = LOGS_DIR / f"{name}.log"
    out = open(log_file, "a")
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        stdout=out,
        stderr=subprocess.STDOUT,
        env=proc_env,
        start_new_session=True,
    )
    return proc.pid


def start_all():
    ensure_dirs()
    pids = load_pids()
    print("=" * 70)
    print("🚀 STARTING PRODUCTION ON-PREMISE RAG GATEWAY STACK")
    print("=" * 70)

    # 1. Start Redis Stack
    redis_st, _ = check_port_health(REDIS_PORT)
    if redis_st == "DOWN":
        if not is_pid_running(pids.get("redis", 0)):
            print(f"📦 Starting Redis Stack Server on port {REDIS_PORT}...")
            pid = start_service("redis", [str(REDIS_SERVER_BIN)])
            pids["redis"] = pid
            time.sleep(1.0)
    elif redis_st == "LOADING":
        print(f"🟡 Redis Stack is running and actively loading 12GB dataset into RAM...")
    else:
        print(f"✅ Redis Stack is already running on port {REDIS_PORT}.")

    # 2. Start vLLM GPU Server
    vllm_st, _ = check_port_health(VLLM_PORT, "/health")
    if vllm_st != "READY":
        if not is_pid_running(pids.get("vllm", 0)):
            print(f"🔥 Starting vLLM GPU Server on port {VLLM_PORT} (NVIDIA A100)...")
            vllm_cmd = [
                PYTHON_BIN, "-m", "vllm.entrypoints.openai.api_server",
                "--model", "models/llm_7b/qwen2.5-7b-instruct",
                "--served-model-name", "Qwen/Qwen2.5-7B-Instruct",
                "--host", "127.0.0.1",
                "--port", str(VLLM_PORT),
                "--gpu-memory-utilization", "0.45",
                "--max-model-len", "8192",
                "--dtype", "bfloat16",
            ]
            pid = start_service("vllm", vllm_cmd, env={"CUDA_VISIBLE_DEVICES": "0"})
            pids["vllm"] = pid
    else:
        print(f"✅ vLLM GPU Server is already running on port {VLLM_PORT}.")

    # 3. Start CPU SLM Server
    slm_st, _ = check_port_health(SLM_PORT, "/healthz")
    if slm_st != "READY":
        if not is_pid_running(pids.get("slm", 0)):
            print(f"⚡ Starting CPU SLM Server on port {SLM_PORT} (Qwen 2.5 3B GGUF with 6 parallel workers)...")
            slm_cmd = [
                PYTHON_BIN, "-m", "pipeline.serve_cpu_slm",
                "--host", "127.0.0.1",
                "--port", str(SLM_PORT),
                "--threads", "16",
                "--workers", "6",
            ]
            pid = start_service("slm", slm_cmd)
            pids["slm"] = pid
    else:
        print(f"✅ CPU SLM Server is already running on port {SLM_PORT}.")

    # 4. Start Production Gateway (No reload, production uvicorn with 6 workers)
    gw_st, _ = check_port_health(GATEWAY_PORT, "/healthz")
    if gw_st != "READY":
        if not is_pid_running(pids.get("gateway", 0)):
            print(f"🌐 Starting Institutional Gateway on port {GATEWAY_PORT} (0.0.0.0:8080 with 6 workers)...")
            gw_cmd = [
                PYTHON_BIN, "-m", "uvicorn",
                "gateway.main:app",
                "--host", "0.0.0.0",
                "--port", str(GATEWAY_PORT),
                "--workers", "6",
                "--no-access-log",
            ]
            pid = start_service("gateway", gw_cmd)
            pids["gateway"] = pid
    else:
        print(f"✅ RAG Gateway is already running on port {GATEWAY_PORT}.")

    save_pids(pids)
    print("-" * 70)
    print("⏳ Verifying service readiness...")

    # Verification polling (up to 30s)
    for _ in range(15):
        time.sleep(2.0)
        r_st, _ = check_port_health(REDIS_PORT)
        v_st, _ = check_port_health(VLLM_PORT, "/health")
        s_st, _ = check_port_health(SLM_PORT, "/healthz")
        g_st, _ = check_port_health(GATEWAY_PORT, "/healthz")
        if r_st == "READY" and v_st == "READY" and s_st == "READY" and g_st == "READY":
            break

    print_status()


def stop_all(stop_redis: bool = False, stop_vllm: bool = False):
    pids = load_pids()
    print("=" * 70)
    print("🛑 STOPPING PRODUCTION COMPUTE ENGINES")
    print("=" * 70)

    # Stop order: Gateway -> SLM (Keep Redis and vLLM active to avoid costly reload)
    service_names = ["gateway", "slm"]
    if stop_vllm:
        service_names.append("vllm")
    if stop_redis:
        service_names.append("redis")
    else:
        print("ℹ️ Preserving Redis RAM (2.26M document vectors active on port 6379).")

    for name in service_names:
        pid = pids.get(name)
        if pid and is_pid_running(pid):
            print(f"Stopping {name} (PID {pid})...")
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        else:
            # Fallback search if PID file was missing
            try:
                if name == "slm":
                    subprocess.run(f"pkill -f 'serve_cpu_slm' 2>/dev/null || true", shell=True)
                elif name == "gateway":
                    subprocess.run(f"fuser -k {GATEWAY_PORT}/tcp 2>/dev/null || true", shell=True)
                elif name == "redis" and stop_redis:
                    subprocess.run(f"pkill -f 'redis-stack-server' 2>/dev/null || true", shell=True)
            except Exception:
                pass

    time.sleep(1.5)
    # Check if any lingering
    for name in service_names:
        pid = pids.get(name)
        if pid and is_pid_running(pid):
            print(f"Force killing {name} (PID {pid})...")
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

    # Update PID file to keep preserved services
    remaining_pids = {k: v for k, v in pids.items() if k not in service_names}
    save_pids(remaining_pids)
    print("✅ Compute engines stopped.")


def print_status():
    pids = load_pids()
    print("\n" + "=" * 70)
    print("📊 PRODUCTION STACK HEALTH & RUNTIME DASHBOARD")
    print("=" * 70)

    services = [
        ("Redis Vector & Cache", REDIS_PORT, "", True, pids.get("redis"), True),
        ("vLLM GPU Engine (7B)", VLLM_PORT, "/health", False, pids.get("vllm"), True),
        ("CPU SLM Engine (3B)", SLM_PORT, "/healthz", False, pids.get("slm"), True),
        ("Institutional Gateway", GATEWAY_PORT, "/healthz", False, pids.get("gateway"), True),
    ]

    all_ready = True
    has_loading = False

    for name, port, path, is_redis, pid, is_required in services:
        status, msg = check_port_health(port, path)
        if status == "READY":
            status_icon = "🟢 READY"
        elif status == "LOADING":
            status_icon = "🟡 LOADING"
            has_loading = True
            all_ready = False
        else:
            status_icon = "🔴 DOWN"
            all_ready = False

        pid_str = f"PID: {pid}" if pid and is_pid_running(pid) else "PID: N/A"
        print(f"  {status_icon:<11} | {name:<25} | Port {port} | {pid_str:<12} | {msg}")

    # Check Two-Tower MLP Router status
    router_status, router_msg = check_port_health(ROUTER_PORT, "/healthz")
    if router_status == "READY":
        print(f"  🟢 READY   | MLP Router (Microservice)  | Port {ROUTER_PORT} | PID: {pids.get('router', 'N/A'):<8} | {router_msg}")
    else:
        print(f"  ⚡ IN-PROCESS| Two-Tower MLP Router      | Port {GATEWAY_PORT} | In-Process   | Embedded in Gateway (< 3ms)")

    print("=" * 70)
    if all_ready:
        print("🎉 ALL SYSTEMS ONLINE & OPERATIONAL: Ready for campus traffic.")
        print("🌐 Unified AskIISc Frontend UI: http://0.0.0.0:8080/")
    elif has_loading:
        print("⏳ STACK IS INITIALIZING: Redis is actively loading its 12GB dataset into RAM.")
        print("💡 Run 'make redis-watch' in another terminal to track live loading progress.")
    else:
        print("⚠️ SOME SERVICES ARE OFFLINE. Check logs in logs/ directory.")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Production Stack Orchestrator")
    parser.add_argument("action", choices=["start", "stop", "status", "restart"], help="Action to perform")
    args = parser.parse_args()

    if args.action == "start":
        start_all()
    elif args.action == "stop":
        stop_all()
    elif args.action == "status":
        print_status()
    elif args.action == "restart":
        stop_all()
        time.sleep(1.0)
        start_all()


if __name__ == "__main__":
    main()

