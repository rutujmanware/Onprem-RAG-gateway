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
    python scripts/prod_stack.py redis-start   # only Redis (make redis-up)

All paths are relative to the repository root, so the stack runs from any checkout location.
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
    # .env wins over variables exported in the shell, as with make (-include .env); a stale export in ~/.bashrc
    # must not start Redis with a password the gateway's .env does not have
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
except ImportError:
    pass

ROOT_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT_DIR / "logs"
PID_FILE = LOGS_DIR / "prod_pids.json"
PYTHON_BIN = sys.executable

# Default configuration
REDIS_SERVER_BIN = ROOT_DIR / "redis-stack-server-7.2.0-v9" / "bin" / "redis-stack-server"
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
# The thesis snapshot: <REDIS_DATA_DIR>/<REDIS_DBFILENAME> when set (relative to the repository root unless
# absolute); otherwise the first that exists of SNAPSHOT_DIRS x SNAPSHOT_NAMES (find_snapshot).
REDIS_DATA_DIR = ROOT_DIR / os.getenv("REDIS_DATA_DIR", "redis_server_data")
REDIS_DBFILENAME = os.getenv("REDIS_DBFILENAME", "dump_final.rdb")
SNAPSHOT_DIRS = (ROOT_DIR / "redis_server_data", ROOT_DIR / "redis-stack-server-7.2.0-v9" / "var" / "db" / "redis-stack")
SNAPSHOT_NAMES = ("dump_final.rdb", "dump.rdb")
RUN_DIR = ROOT_DIR / "run"
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


_PID_MARKERS = {"gateway": "gateway.main:app", "slm": "serve_cpu_slm", "vllm": "serve_vllm", "redis": "redis"}


def is_pid_running(pid: int, name: Optional[str] = None) -> bool:
    """True if the PID exists and, when `name` is given, is still that service (DEFECT-44: a reused PID of an
    unrelated process must never be treated as ours, or signalled)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    marker = _PID_MARKERS.get(name or "")
    if marker:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                return marker in fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return False
    return True


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


def find_snapshot() -> Optional[Path]:
    """The thesis snapshot to load: the configured REDIS_DATA_DIR / REDIS_DBFILENAME first, then dump_final.rdb or
    dump.rdb in redis_server_data/ or in the Redis Stack data folder (where Redis Stack keeps its own dump.rdb)."""
    seen, candidates = set(), []
    for d in (REDIS_DATA_DIR, *SNAPSHOT_DIRS):
        for n in (REDIS_DBFILENAME, *SNAPSHOT_NAMES):
            c = d / n
            if c not in seen:
                seen.add(c)
                candidates.append(c)
    return next((c for c in candidates if c.is_file()), None)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def redis_start_cmd() -> tuple[list[str], Optional[Path]]:
    """Redis Stack with this repository's settings, and the snapshot it will load (None: empty on purpose).
    Nothing depends on the config file shipped inside the Redis download: settings go into run/redis.conf (mode 600,
    so the password never shows in the process list), and the data directory is passed last because the
    redis-stack-server wrapper sets its own --dir after the config file."""
    password = os.getenv("REDIS_PASSWORD", "")
    if not password:
        sys.exit("REDIS_PASSWORD is empty: set it in .env before starting Redis (README, step 3).")
    if not REDIS_SERVER_BIN.exists():
        sys.exit(f"{_rel(REDIS_SERVER_BIN)} not found: download Redis Stack first (README, step 5).")
    snapshot = find_snapshot()
    if snapshot is None:
        if os.getenv("REDIS_ALLOW_EMPTY", "0") != "1":
            # Without a snapshot Redis starts empty: the gateway would run but find no theses (/ready not_ready).
            looked = ", ".join(_rel(d) + "/" for d in dict.fromkeys((REDIS_DATA_DIR, *SNAPSHOT_DIRS)))
            sys.exit(f"No thesis snapshot found (looked for {', '.join(dict.fromkeys((REDIS_DBFILENAME, *SNAPSHOT_NAMES)))} "
                     f"in {looked}): Redis would start with an empty database.\n"
                     f"Put the snapshot in one of those folders (README, step 6), or set REDIS_DATA_DIR / "
                     f"REDIS_DBFILENAME to where it is.\n"
                     f"To start empty on purpose (building the index, README §6): REDIS_ALLOW_EMPTY=1 make redis-up")
        data_dir, dbfilename = REDIS_DATA_DIR, REDIS_DBFILENAME
    else:
        data_dir, dbfilename = snapshot.parent, snapshot.name
    if " " in str(data_dir):
        sys.exit("The Redis data directory path contains a space, which the redis-stack-server wrapper cannot handle.")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    quoted = password.replace("\\", "\\\\").replace('"', '\\"')
    conf = RUN_DIR / "redis.conf"
    fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(
            f'requirepass "{quoted}"\n'
            "bind 127.0.0.1 -::1\n"          # loopback only
            "protected-mode yes\n"
            f"port {REDIS_PORT}\n"
            f"dbfilename {dbfilename}\n"
            'save ""\n'                      # read-only snapshot use: never rewrite the 12.67 GB file
            "daemonize no\n"                 # stays under this script's PID tracking; output -> logs/redis.log
        )
    os.chmod(conf, 0o600)
    return [str(REDIS_SERVER_BIN), str(conf), "--dir", str(data_dir)], snapshot


def start_redis(pids: Dict[str, int]) -> None:
    redis_st, _ = check_port_health(REDIS_PORT)
    if redis_st == "DOWN":
        if not is_pid_running(pids.get("redis", 0), "redis"):
            cmd, snapshot = redis_start_cmd()
            what = f"loading {_rel(snapshot)}, 35-50 min" if snapshot else "EMPTY database (REDIS_ALLOW_EMPTY=1)"
            print(f"📦 Starting Redis Stack Server on port {REDIS_PORT} ({what})...")
            pids["redis"] = start_service("redis", cmd)
            time.sleep(1.0)
    elif redis_st == "LOADING":
        print(f"🟡 Redis Stack is running and actively loading 12GB dataset into RAM...")
    else:
        print(f"✅ Redis Stack is already running on port {REDIS_PORT}.")


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
    start_redis(pids)

    # 2. Start vLLM GPU Server
    vllm_st, _ = check_port_health(VLLM_PORT, "/health")
    if vllm_st != "READY":
        if not is_pid_running(pids.get("vllm", 0), "vllm"):
            print(f"🔥 Starting vLLM GPU Server on port {VLLM_PORT} (NVIDIA A100)...")
            vllm_cmd = [
                # Hardened launcher: vLLM 0.6.3 ignores --host on its pre-bound
                # socket (binds 0.0.0.0); serve_vllm.py enforces loopback + API key.
                PYTHON_BIN, str(ROOT_DIR / "scripts" / "serve_vllm.py"),
                "--model", "models/llm_7b/qwen2.5-7b-instruct",
                "--served-model-name", "Qwen/Qwen2.5-7B-Instruct",
                "--host", "127.0.0.1",
                "--port", str(VLLM_PORT),
                "--gpu-memory-utilization", "0.45",
                "--max-model-len", "8192",
                "--dtype", "bfloat16",
            ]
            # GPU index for vLLM on this shared machine; set VLLM_GPU in .env (default 0).
            vllm_gpu = os.getenv("VLLM_GPU", "0").strip() or "0"
            print(f"   (vLLM on GPU {vllm_gpu}; set VLLM_GPU in .env to change)")
            pid = start_service("vllm", vllm_cmd, env={"CUDA_VISIBLE_DEVICES": vllm_gpu})
            pids["vllm"] = pid
    else:
        print(f"✅ vLLM GPU Server is already running on port {VLLM_PORT}.")
        warn_if_vllm_insecure()

    # 3. Start CPU SLM Server
    slm_st, _ = check_port_health(SLM_PORT, "/healthz")
    if slm_st != "READY":
        if not is_pid_running(pids.get("slm", 0), "slm"):
            print(f"⚡ Starting CPU SLM Server on port {SLM_PORT} (Qwen 2.5 3B GGUF, 1 process with 6 model slots)...")
            slm_cmd = [
                PYTHON_BIN, "-m", "pipeline.serve_cpu_slm",
                "--host", "127.0.0.1",
                "--port", str(SLM_PORT),
                "--threads", "16",
                "--slots", os.getenv("SLM_SLOTS", "6"),
            ]
            pid = start_service("slm", slm_cmd)
            pids["slm"] = pid
    else:
        print(f"✅ CPU SLM Server is already running on port {SLM_PORT}.")

    # 4. Start Production Gateway (No reload, production uvicorn with 6 workers)
    gw_st, _ = check_port_health(GATEWAY_PORT, "/healthz")
    if gw_st != "READY":
        if not is_pid_running(pids.get("gateway", 0), "gateway"):
            print(f"🌐 Starting Institutional Gateway on port {GATEWAY_PORT} (0.0.0.0:8080 with 6 workers)...")
            gw_cmd = [
                PYTHON_BIN, "-m", "uvicorn",
                "gateway.main:app",
                "--host", "0.0.0.0",
                "--port", str(GATEWAY_PORT),
                "--workers", "6",
                "--no-access-log",
                # DEFECT-44: never trust X-Forwarded-For (uvicorn trusts it from 127.0.0.1 by default, so any
                # local user of this shared host could choose their own client IP)
                "--no-proxy-headers",
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


def vllm_is_unauthenticated() -> bool:
    """True if the running vLLM answers /v1/models WITHOUT a Bearer key (pre-hardening launch)."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{VLLM_PORT}/v1/models")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return resp.status == 200
    except Exception:
        return False  # 401 (secured) or not running


def warn_if_vllm_insecure() -> None:
    if vllm_is_unauthenticated():
        print("🚨 SECURITY: the running vLLM on port 8000 accepts unauthenticated /v1 requests")
        print("   (started without scripts/serve_vllm.py, so it is also bound to 0.0.0.0).")
        print("   Fix: python scripts/prod_stack.py restart --with-vllm   (or: make prod-restart-vllm)")


def stop_process_group(name: str, pid: int, timeout: float = 90.0) -> None:
    """SIGTERM a service's whole process group and wait, escalating to SIGKILL.

    vLLM runs its engine in a child process that owns the GPU memory; killing only
    the parent can orphan it and leave the GPU allocated, so the new vLLM fails to start.
    Services are launched with start_new_session=True, so the PID is its group leader.
    """
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    if pgid != pid or pgid == os.getpgid(0):
        print(f"⚠️ {name} (PID {pid}) is not its own process group; signalling the PID only.")
        send = lambda sig: os.kill(pid, sig)
        alive = lambda: is_pid_running(pid)
    else:
        send = lambda sig: os.killpg(pgid, sig)

        def alive() -> bool:
            try:
                os.killpg(pgid, 0)
                return True
            except ProcessLookupError:
                return False

    print(f"Stopping {name} (process group {pgid}), waiting up to {int(timeout)}s for GPU release...")
    try:
        send(signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + timeout
    while time.time() < deadline and alive():
        time.sleep(1.0)
    if alive():
        print(f"Force killing {name} (process group {pgid})...")
        try:
            send(signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(2.0)


def stop_all(stop_redis: bool = False, stop_vllm: bool = False):
    pids = load_pids()
    print("=" * 70)
    print("🛑 STOPPING PRODUCTION COMPUTE ENGINES")
    print("=" * 70)

    # Stop order: Gateway -> SLM (Keep Redis and vLLM active to avoid costly reload)
    service_names = ["gateway", "slm"]
    if stop_vllm:
        vllm_pid = pids.get("vllm")
        if vllm_pid and is_pid_running(vllm_pid, "vllm"):
            stop_process_group("vllm", vllm_pid)
        else:
            print("ℹ️ No vLLM PID recorded; if port 8000 is still busy, stop that process manually.")
        service_names.append("vllm")  # drop it from the PID file below
    else:
        print("ℹ️ Preserving vLLM GPU engine (use --with-vllm to restart it).")
    if stop_redis:
        service_names.append("redis")
    else:
        print("ℹ️ Preserving Redis RAM (2.26M document vectors active on port 6379).")

    for name in service_names:
        pid = pids.get(name)
        if name in ("gateway", "slm") and pid and is_pid_running(pid, name):
            # whole process group: uvicorn's worker processes must not be left behind (DEFECT-44)
            stop_process_group(name, pid, timeout=30.0)
            continue
        if pid and is_pid_running(pid, name):
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
        if pid and is_pid_running(pid, name):
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
    warn_if_vllm_insecure()

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
    parser.add_argument("action", choices=["start", "stop", "status", "restart", "redis-start"], help="Action to perform")
    parser.add_argument("--with-vllm", action="store_true",
                        help="Also stop/restart the vLLM GPU engine (slow model reload; needed to apply vLLM launch changes)")
    args = parser.parse_args()

    if args.action == "start":
        start_all()
    elif args.action == "redis-start":
        ensure_dirs()
        pids = load_pids()
        start_redis(pids)
        save_pids(pids)
        print("Redis is starting. Loading the thesis snapshot takes 35-50 minutes: watch it with `make redis-watch`.")
    elif args.action == "stop":
        stop_all(stop_vllm=args.with_vllm)
    elif args.action == "status":
        print_status()
    elif args.action == "restart":
        stop_all(stop_vllm=args.with_vllm)
        time.sleep(1.0)
        start_all()


if __name__ == "__main__":
    main()

