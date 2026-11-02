"""Locust workload for the authenticated gateway.

Run, for example:
  locust -f evaluation/benchmarks/load_test.py --host http://localhost:8080
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from locust import HttpUser, between, task


TRACE_PATH = Path(__file__).resolve().parents[1] / "datasets" / "load_traces.json"


class GatewayUser(HttpUser):
    wait_time = between(0.05, 0.25)

    def on_start(self) -> None:
        self.traces = json.loads(TRACE_PATH.read_text(encoding="utf-8"))
        self.trace_position = 0
        token = os.getenv("LOAD_TEST_TOKEN")
        if not token:
            response = self.client.post(
                "/auth/login",
                json={
                    "email": os.getenv("LOAD_TEST_EMAIL", "loadtest@iisc.ac.in"),
                    "password": os.getenv("LOAD_TEST_PASSWORD", ""),
                    "persona": "student",
                },
                name="/auth/login",
            )
            response.raise_for_status()
            token = response.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {token}"}

    @task
    def ask(self) -> None:
        trace = self.traces[self.trace_position % len(self.traces)]
        self.trace_position += 1
        scope = trace.get("scope") or {
            "thesis_id": os.getenv("LOAD_TEST_THESIS_ID", ""),
            "chapter": os.getenv("LOAD_TEST_CHAPTER", "all"),
        }
        if not scope["thesis_id"]:
            raise RuntimeError("Set LOAD_TEST_THESIS_ID or provide scope in load_traces.json")
        with self.client.post(
            "/v1/chat/completions",
            json={"prompt": trace["prompt"], "scope": scope},
            headers=self.headers,
            name="/v1/chat/completions",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"Gateway returned {response.status_code}: {response.text[:200]}")
