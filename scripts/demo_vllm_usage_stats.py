#!/usr/bin/env python3
"""Demo: vLLM usage statistics were sent by default and are now switched off (fixes.md, 2026-11-28).

Read-only and safe to run on production: it sends nothing, restarts nothing and only asks vLLM's own
is_usage_stats_enabled() check, reads ~/.config/vllm/usage_stats.json (vLLM's local copy of every report it
sent) and reads the running model server's process environment.

    make demo-vllm-stats
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATS_FILE = Path.home() / ".config" / "vllm" / "usage_stats.json"
SWITCHES = ("VLLM_NO_USAGE_STATS", "VLLM_DO_NOT_TRACK", "DO_NOT_TRACK")
CHECK = "import vllm.usage.usage_lib as u; print(u.is_usage_stats_enabled())"


def ask_vllm(with_launcher_fix: bool) -> str:
    """vLLM's own answer to 'will you send usage statistics?' in a fresh process."""
    env = {k: v for k, v in os.environ.items() if k not in SWITCHES}   # a plain vLLM install
    code = CHECK if not with_launcher_fix else (
        "import sys; sys.path.insert(0, %r)\n"
        "from scripts.serve_vllm import disable_usage_stats; disable_usage_stats()\n" % str(ROOT) + CHECK)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    return (out.stdout.strip().splitlines() or ["?"])[-1]


def model_server_pid() -> int | None:
    """The production vLLM process (python running scripts/serve_vllm.py, not the bash wrapper)."""
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            argv = (p / "cmdline").read_bytes().split(b"\0")
            if argv and argv[0].endswith(b"python") and any(a.endswith(b"scripts/serve_vllm.py") for a in argv):
                return int(p.name)
        except OSError:
            continue
    return None


def started_at(pid: int) -> datetime:
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return datetime.strptime(out, "%a %b %d %H:%M:%S %Y")


def env_of(pid: int) -> dict:
    try:
        items = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    except OSError:
        return {}
    return dict(i.decode(errors="replace").split("=", 1) for i in items if b"=" in i)


def main() -> None:
    print("=" * 74)
    print(" vLLM usage statistics: before and after the fix")
    print("=" * 74)

    print("\n1) A plain vLLM, as installed (what we ran before the fix)")
    before = ask_vllm(False)
    print(f"   vLLM: will I send usage statistics to stats.vllm.ai?  -> {before}")

    records = []
    if STATS_FILE.exists():
        for line in STATS_FILE.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                pass
    full = [r for r in records if "model_architecture" in r]
    # The file is shared by every vLLM run of this account. AskIISc's model server is vLLM's OpenAI API server
    # (context OPENAI_API_SERVER); offline jobs (dataset generation, other projects) use the LLM class.
    api = [r for r in full if r.get("context") == "OPENAI_API_SERVER"]
    api_uuids = {r.get("uuid") for r in api}
    if full:
        r = api[-1] if api else full[-1]   # the most recent report of the AskIISc model server
        print(f"   vLLM's local copy of what it sent ({STATS_FILE}):")
        print(f"     AskIISc model server: {len(api)} reports"
              + (f" ({datetime.fromtimestamp(int(api[0]['log_time']) / 1e9):%d %b} to "
                 f"{datetime.fromtimestamp(int(api[-1]['log_time']) / 1e9):%d %b}), "
                 f"{sum(1 for x in records if x.get('uuid') in api_uuids) - len(api)} heartbeats" if api else ""))
        print(f"     other vLLM jobs of this account: {len(full) - len(api)} reports")
        print(f"     latest AskIISc report: GPU={r.get('gpu_type')}, model={r.get('model_architecture')}, "
              f"vLLM {r.get('vllm_version')}, CPUs={r.get('num_cpu')}, RAM={int(r.get('total_memory', 0)) >> 30} GiB")
        print(f"     all {len(r)} fields (hardware and settings only, no questions or answers):")
        names = sorted(r)
        for i in range(0, len(names), 5):
            print("       " + ", ".join(names[i:i + 5]))

    print("\n2) The same vLLM through our launcher (scripts/serve_vllm.py)")
    after = ask_vllm(True)
    print(f"   vLLM: will I send usage statistics?  -> {after}")

    print("\n3) The production model server right now")
    pid = model_server_pid()
    if not pid:
        print("   vLLM is not running (start it with make prod-up).")
    else:
        start = started_at(pid)
        print(f"   vLLM PID {pid}, started {start:%Y-%m-%d %H:%M:%S} by scripts/serve_vllm.py")
        children = subprocess.run(["ps", "-o", "pid=", "--ppid", str(pid)], capture_output=True, text=True).stdout.split()
        for c in children[:2]:
            e = env_of(int(c))
            print(f"   engine process {c}: " + ", ".join(f"{k}={e.get(k, 'unset')}" for k in SWITCHES))
        start_ns = int(start.timestamp() * 1e9)
        # A reporting model server would write a new OPENAI_API_SERVER report at startup, then heartbeats with its uuid.
        new_api = [r for r in records if int(r.get("log_time", 0)) >= start_ns
                   and (r.get("context") == "OPENAI_API_SERVER" or r.get("uuid") in api_uuids)]
        last = max((int(r.get("log_time", 0)) for r in records if r.get("uuid") in api_uuids), default=0)
        if last:
            print(f"   last AskIISc report or heartbeat: {datetime.fromtimestamp(last / 1e9):%Y-%m-%d %H:%M:%S}"
                  + (" (before this vLLM started)" if last < start_ns else ""))
        print(f"   AskIISc reports since this vLLM started: {len(new_api)}")
        new = new_api

    ok = before == "True" and after == "False" and (not pid or not new)
    print("\n" + ("RESULT: FIXED - plain vLLM would report; our launcher switches it off, and the running "
                  "server has sent nothing since it started." if ok else "RESULT: CHECK THE OUTPUT ABOVE"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
