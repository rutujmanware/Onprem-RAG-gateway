"""Regression tests for scripts/serve_vllm.py (red-team finding: vLLM 0.6.3 ignores --host).

vLLM 0.6.3 pre-binds with ``sock.bind(("", port))``. These tests reproduce that
exact call through the launcher's socket shim and check it lands on loopback.
"""

import socket

import pytest

from scripts.serve_vllm import make_host_bound_socket_module, resolve_bind_host


def test_empty_host_bind_is_forced_to_loopback():
    shim = make_host_bound_socket_module("127.0.0.1")
    sock = shim.socket(shim.AF_INET, shim.SOCK_STREAM)  # same call as vLLM's run_server
    try:
        sock.bind(("", 0))  # the vulnerable vLLM 0.6.3 bind
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_bind_sets_reuseaddr_so_restarts_survive_time_wait():
    shim = make_host_bound_socket_module("127.0.0.1")
    sock = shim.socket(shim.AF_INET, shim.SOCK_STREAM)
    try:
        sock.bind(("", 0))
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1
    finally:
        sock.close()


def test_shim_leaves_real_socket_module_untouched():
    make_host_bound_socket_module("127.0.0.1")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("", 0))
        assert sock.getsockname()[0] == "0.0.0.0"
    finally:
        sock.close()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_loopback_hosts_are_accepted(host):
    assert resolve_bind_host(host) == "127.0.0.1"


@pytest.mark.parametrize("host", ["", None, "0.0.0.0"])
def test_wildcard_hosts_are_refused(host):
    with pytest.raises(ValueError):
        resolve_bind_host(host)


@pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.10", "8.8.8.8", "172.17.0.1"])
def test_non_loopback_hosts_are_always_refused(host):
    with pytest.raises(ValueError, match="not a loopback"):
        resolve_bind_host(host)


def test_resolve_bind_host_has_no_override_parameter():
    import inspect
    assert list(inspect.signature(resolve_bind_host).parameters) == ["host"]


def test_shim_refuses_non_loopback_bind_even_if_called_directly():
    """Defence in depth: a shim built for a non-loopback host must still refuse to serve."""
    shim = make_host_bound_socket_module("0.0.0.0")
    sock = shim.socket(shim.AF_INET, shim.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="refusing to serve"):
            sock.bind(("", 0))
    finally:
        sock.close()


def test_removed_override_env_var_blocks_startup(monkeypatch):
    """Setting the removed VLLM_ALLOW_NON_LOOPBACK must stop the launcher, not be silently ignored."""
    import sys
    from scripts import serve_vllm
    monkeypatch.setenv("VLLM_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("VLLM_API_KEY", "test-key")
    monkeypatch.setattr(serve_vllm, "_load_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["serve_vllm.py", "--model", "m", "--host", "127.0.0.1", "--port", "18999"])
    with pytest.raises(SystemExit, match="no longer supported"):
        serve_vllm.main()


# ------------------------------------------------------------------ no usage statistics (2026-11-28)
def test_usage_statistics_are_switched_off(monkeypatch):
    from scripts.serve_vllm import disable_usage_stats
    for var in ("VLLM_NO_USAGE_STATS", "VLLM_DO_NOT_TRACK", "DO_NOT_TRACK"):
        monkeypatch.setenv(var, "0")   # .env or the shell cannot switch reporting back on
    disable_usage_stats()
    import os
    assert all(os.environ[v] == "1" for v in ("VLLM_NO_USAGE_STATS", "VLLM_DO_NOT_TRACK", "DO_NOT_TRACK"))


def test_switched_off_before_vllm_is_imported():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "scripts" / "serve_vllm.py").read_text()
    main = src[src.index("def main() -> None:"):]
    assert main.index("disable_usage_stats()") < main.index("import vllm")


def test_installed_vllm_honours_the_switch(monkeypatch):
    usage_lib = pytest.importorskip("vllm.usage.usage_lib")
    from scripts.serve_vllm import disable_usage_stats
    monkeypatch.setattr(usage_lib, "_USAGE_STATS_ENABLED", None, raising=False)
    disable_usage_stats()
    assert usage_lib.is_usage_stats_enabled() is False
