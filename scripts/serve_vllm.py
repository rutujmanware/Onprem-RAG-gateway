#!/usr/bin/env python3
"""Hardened launcher for the vLLM OpenAI-compatible GPU server (port 8000).

Why this exists
---------------
vLLM 0.6.3 (pinned; we cannot upgrade) pre-binds its listening socket in
``vllm/entrypoints/openai/api_server.py::run_server`` with::

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("", args.port))          # "" == every interface

and then hands that fd to uvicorn, so ``--host 127.0.0.1`` is silently ignored
and the model API listens on 0.0.0.0:8000 without authentication.

This launcher accepts exactly the same arguments as
``python -m vllm.entrypoints.openai.api_server`` and adds two controls:

1. The pre-bound socket honours ``--host`` and is STRICTLY loopback-only: ``--host``
   must resolve to a loopback address (127.0.0.0/8), there is no override, and the
   address actually bound is re-checked before serving. Exposing the model on the
   network is not supported; all access goes through the :8080 gateway (FR-4).
2. Startup is refused unless an API key is configured (VLLM_API_KEY env var,
   preferred, or --api-key). vLLM then requires
   ``Authorization: Bearer <key>`` on every /v1/* route, so the model stays
   closed even if the bind ever regresses.

The bind patch is a no-op on vLLM versions that already bind to ``args.host``.

Usage:
    python scripts/serve_vllm.py --model <path> --host 127.0.0.1 --port 8000 ...
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import types
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def resolve_bind_host(host: str | None) -> str:
    """Resolve --host to an IPv4 loopback address; anything else is refused (no override)."""
    if not host:
        raise ValueError(
            "--host is empty, which binds every interface. Pass --host 127.0.0.1."
        )
    try:
        resolved = socket.gethostbyname(host)
    except OSError as exc:
        raise ValueError(f"cannot resolve --host {host!r} to an IPv4 address: {exc}") from exc

    addr = ipaddress.ip_address(resolved)
    if not addr.is_loopback:
        raise ValueError(
            f"--host {host!r} ({resolved}) is not a loopback address. The GPU model "
            "server is loopback-only and must stay internal behind the :8080 gateway."
        )
    return resolved


def make_host_bound_socket_module(host: str) -> types.ModuleType:
    """Return a stand-in for the ``socket`` module whose sockets bind "" to ``host``.

    It is installed as ``vllm.entrypoints.openai.api_server.socket`` so only that
    module's pre-bind is affected; the rest of the process uses the real module.
    """

    class HostBoundSocket(socket.socket):
        def bind(self, address):  # type: ignore[override]
            if isinstance(address, tuple) and address and address[0] in ("", "0.0.0.0", None):
                address = (host, *address[1:])
            # vLLM 0.6.3 omits SO_REUSEADDR here, so a restart fails with EADDRINUSE while the
            # previous server's connections sit in TIME_WAIT. Linux still forbids two listeners.
            self.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            super().bind(address)
            bound_host = self.getsockname()[0]
            if bound_host != host or not ipaddress.ip_address(bound_host).is_loopback:
                self.close()
                raise RuntimeError(
                    f"vLLM socket bound to {bound_host!r}, expected loopback {host!r}; refusing to serve."
                )

    shim = types.ModuleType("socket")
    shim.__dict__.update(socket.__dict__)
    shim.socket = HostBoundSocket  # type: ignore[attr-defined]
    return shim


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT_DIR / ".env", override=False)


def main() -> None:
    _load_env()

    import uvloop
    import vllm.entrypoints.openai.api_server as api_server
    from vllm.entrypoints.openai.cli_args import make_arg_parser, validate_parsed_serve_args
    from vllm.utils import FlexibleArgumentParser

    # Mirrors the `if __name__ == "__main__"` block of vllm.entrypoints.openai.api_server.
    parser = make_arg_parser(
        FlexibleArgumentParser(description="vLLM OpenAI-compatible server (hardened launcher).")
    )
    args = parser.parse_args()
    validate_parsed_serve_args(args)

    if "VLLM_ALLOW_NON_LOOPBACK" in os.environ:
        sys.exit("[serve_vllm] refusing to start: VLLM_ALLOW_NON_LOOPBACK is no longer supported; "
                 "the model server is strictly loopback-only. Remove it from the environment/.env.")
    try:
        args.host = resolve_bind_host(args.host)
    except ValueError as exc:
        sys.exit(f"[serve_vllm] refusing to start: {exc}")

    if not (os.getenv("VLLM_API_KEY") or args.api_key):
        sys.exit(
            "[serve_vllm] refusing to start: no API key. Set VLLM_API_KEY in .env "
            "(e.g. `python -c 'import secrets;print(secrets.token_hex(32))'`)."
        )
    if args.api_key:
        print(
            "[serve_vllm] warning: --api-key is visible in the process list; "
            "prefer the VLLM_API_KEY environment variable.",
            file=sys.stderr,
        )

    api_server.socket = make_host_bound_socket_module(args.host)
    print(f"[serve_vllm] binding vLLM to {args.host}:{args.port} with API-key auth on /v1/*",
          file=sys.stderr)
    uvloop.run(api_server.run_server(args))


if __name__ == "__main__":
    main()
