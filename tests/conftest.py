"""Shared fixtures: an HTTP client for MockCore and a live MockCore server for browser tests."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from mockcore import MockCoreConfig, create_app

USERNAME = "operator"
PASSWORD = "mockcore-demo"


def logged_in_client(config: MockCoreConfig | None = None) -> TestClient:
    client = TestClient(create_app(config or MockCoreConfig()))
    resp = client.post("/login", data={"uid": USERNAME, "pwd": PASSWORD})
    assert resp.url.path == "/app"
    return client


@pytest.fixture
def client() -> TestClient:
    return logged_in_client()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LiveServer:
    def __init__(self, config: MockCoreConfig) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.app = create_app(config)
        self._server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                httpx.get(self.url + "/login", timeout=0.5)
                return
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("MockCore did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    def set_faults(self, spec: str) -> None:
        httpx.post(self.url + "/__admin/faults", json={"spec": spec}).raise_for_status()

    def reset(self) -> None:
        httpx.post(self.url + "/__admin/reset").raise_for_status()


@pytest.fixture(scope="session")
def live_mockcore() -> Iterator[LiveServer]:
    server = LiveServer(MockCoreConfig())
    server.start()
    yield server
    server.stop()


@pytest.fixture
def mockcore_url(live_mockcore: LiveServer) -> Iterator[str]:
    live_mockcore.reset()
    yield live_mockcore.url
    live_mockcore.reset()
