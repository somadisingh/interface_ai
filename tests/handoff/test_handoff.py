"""Human-in-the-loop handoff: control lease, operator API, and a full take-over of the live
session during a replay, driven through the real operator HTTP API from another thread."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cua.handoff.channel import ApprovalRequest, AssistRequest
from cua.handoff.control import InvalidTransition, LeaseViolation, SessionControl
from cua.handoff.queue import InterventionQueue
from cua.handoff.server import create_operator_app
from cua.runtime import open_runtime
from cua.schema import AppProfile, RunResult, load_capability
from cua.surface.web import WebSurface
from tests.conftest import PASSWORD, USERNAME, LiveServer

ROOT = Path(__file__).parents[2]
SECRETS = {"MOCKCORE_USERNAME": USERNAME, "MOCKCORE_PASSWORD": PASSWORD}
READ = load_capability(ROOT / "tests/fixtures/read_savings_balance.yaml")
OPEN = load_capability(ROOT / "tests/fixtures/open_share_account.yaml")


# ------------------------------------------------------------------ control lease


def test_lease_transitions_and_history() -> None:
    seen: list[str] = []
    control = SessionControl(on_change=lambda t: seen.append(f"{t.frm}->{t.to}:{t.by}"))
    control.assert_automation()
    control.transfer("paused", by="automation", reason="stuck")
    with pytest.raises(LeaseViolation):
        control.assert_automation()
    control.transfer("human", by="alice", reason="took control")
    assert control.holder == "alice"
    with pytest.raises(InvalidTransition):
        control.transfer("paused", by="bob", reason="nope")
    control.transfer("automation", by="alice", reason="handed back")
    control.assert_automation()
    assert seen == [
        "automation->paused:automation",
        "paused->human:alice",
        "human->automation:alice",
    ]


# ------------------------------------------------------------------ operator API


def _assist(rid: str = "hlp-1") -> AssistRequest:
    return AssistRequest(rid, "run-1", "cap", "step", "unknown_state", "blocked", "http://x")


def test_operator_api_enforces_request_kinds_and_holder(tmp_path: Path) -> None:
    control, queue = SessionControl(), InterventionQueue()
    api = TestClient(create_operator_app(control, queue, tmp_path))
    queue.submit(
        "approval",
        ApprovalRequest(
            "apr-1", "run-1", "cap", "confirm", "click", "irreversible", "why", "http://x"
        ),
    )
    control.transfer("paused", by="automation", reason="approval")
    assert api.post("/api/requests/apr-1/take", json={"by": "a"}).status_code == 409
    assert api.post("/api/requests/apr-1/approve", json={"by": "a"}).status_code == 200
    assert api.post("/api/requests/apr-1/reject", json={"by": "a"}).status_code == 409
    control.transfer("automation", by="a", reason="approved")

    queue.submit("assist", _assist())
    control.transfer("paused", by="automation", reason="blocked")
    assert api.post("/api/requests/hlp-1/take", json={"by": "alice"}).status_code == 200
    assert api.get("/api/state").json()["controller"] == "human"
    other = api.post("/api/requests/hlp-1/handback", json={"by": "bob", "resolution": "resume"})
    assert other.status_code == 409  # only the person holding the session hands it back
    ok = api.post("/api/requests/hlp-1/handback", json={"by": "alice", "resolution": "resume"})
    assert ok.status_code == 200
    assert api.get("/evidence/../../etc/passwd").status_code == 404
    assert "cua operator" in api.get("/").text


# ------------------------------------------------------------------ full handoff (browser)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _operator(port: int, act: Callable[[httpx.Client, dict[str, Any]], bool]) -> threading.Thread:
    """Plays the operator via the HTTP API: calls ``act`` on each open request until done."""

    def loop() -> None:
        deadline = time.monotonic() + 90
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as http:
            while time.monotonic() < deadline:
                try:
                    state = http.get("/api/state").json()
                except httpx.HTTPError:
                    time.sleep(0.2)
                    continue
                for ticket in state["requests"]:
                    if ticket["open"] and act(http, ticket):
                        return
                time.sleep(0.2)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def _profile_without(state: str) -> AppProfile:
    from cua.schema import load_app_profile

    data = load_app_profile(ROOT / "apps/mockcore/profile.yaml").model_dump(mode="json")
    del data["states"][state]
    return AppProfile.model_validate(data)


def _events(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines()]


def test_operator_takes_over_the_live_session_and_hands_back(
    live_mockcore: LiveServer, mockcore_url: str, tmp_path: Path
) -> None:
    live_mockcore.set_faults("modal")  # an overlay the profile will NOT know about
    port = _free_port()
    human_done = threading.Event()

    def person_clicks_ok(surface: WebSurface) -> None:
        # Stand-in for the person: raw mouse input on the very same page.
        main = surface.page.frame(name="main")
        assert main is not None
        box = main.get_by_text("OK", exact=True).bounding_box()
        assert box is not None
        surface.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        surface.page.wait_for_timeout(300)
        # The lease holds: automation cannot act while the person is in control.
        with pytest.raises(LeaseViolation):
            surface.navigate("/home")
        human_done.set()

    def operator(http: httpx.Client, ticket: dict[str, Any]) -> bool:
        if ticket["kind"] != "assist":
            return False
        http.post(f"/api/requests/{ticket['id']}/take", json={"by": "alice"})
        assert human_done.wait(30)
        http.post(
            f"/api/requests/{ticket['id']}/handback",
            json={"by": "alice", "resolution": "resume", "note": "dismissed a notice"},
        )
        return True

    thread = _operator(port, operator)
    with open_runtime(
        mockcore_url,
        human="operator",
        root=ROOT,
        runs_dir=tmp_path,
        escalate=True,
        secrets_source=SECRETS,
        operator_port=port,
        simulate_operator=person_clicks_ok,
    ) as rt:
        rt.engine.profile = _profile_without("password_expiry_notice")
        result: RunResult = rt.engine.run(READ, {"member_id": "12345"})
        run_dir = rt.log.run_dir
    thread.join(5)

    assert result.status == "success", result.error
    assert len(result.interventions) == 1
    events = _events(run_dir)
    transfers = [(e["frm"], e["to"], e["by"]) for e in events if e["type"] == "control_transferred"]
    assert transfers == [
        ("automation", "paused", "automation"),
        ("paused", "human", "alice"),
        ("human", "automation", "alice"),
    ]
    human_actions = [e for e in events if e["type"] == "human_action"]
    assert any(
        a["action"] == "click" and a["text"] == "OK" and a["by"] == "alice" for a in human_actions
    )
    # Only the person's input is attributed to them, never automation's earlier typing.
    assert all(a.get("name") != "mid" for a in human_actions)
    resolved = next(e for e in events if e["type"] == "assist_resolved")
    assert resolved["resolution"] == "resume" and resolved["human_actions"]


def test_operator_approves_an_irreversible_step(
    live_mockcore: LiveServer, mockcore_url: str, tmp_path: Path
) -> None:
    port = _free_port()

    def operator(http: httpx.Client, ticket: dict[str, Any]) -> bool:
        if ticket["kind"] != "approval":
            return False
        assert ticket["request"]["risk"] == "irreversible"
        http.post(f"/api/requests/{ticket['id']}/approve", json={"by": "supervisor"})
        return True

    thread = _operator(port, operator)
    inputs = {
        "member_id": "12345",
        "account_type": "S01",
        "nickname": "Rainy day",
        "deposit": "100.00",
        "funding_suffix": "S00",
    }
    with open_runtime(
        mockcore_url,
        human="operator",
        root=ROOT,
        runs_dir=tmp_path,
        secrets_source=SECRETS,
        operator_port=port,
    ) as rt:
        result = rt.engine.run(OPEN, inputs)
        run_dir = rt.log.run_dir
    thread.join(5)
    assert result.status == "success", result.error
    events = _events(run_dir)
    approval = next(e for e in events if e["type"] == "approval_resolved")
    assert (approval["approved"], approval["by"]) == (True, "supervisor")
    assert [e["to"] for e in events if e["type"] == "control_transferred"] == [
        "paused",
        "automation",
    ]


def test_input_while_paused_is_recorded_as_unleased(tmp_path: Path) -> None:
    """A person clicking in the browser before pressing "Take control" is still audited."""
    from cua.evidence.log import EventLog
    from cua.handoff.control import SessionControl
    from cua.handoff.operator import OperatorChannel
    from cua.handoff.queue import InterventionQueue
    from cua.policy import Redactor

    class Surface:
        def install_human_recorder(self, cb: Callable[[dict[str, Any]], None]) -> None:
            self.record = cb

    surface, control = Surface(), SessionControl()
    log = EventLog(tmp_path, "run-x", Redactor())
    channel = OperatorChannel(surface, control, InterventionQueue(), log)  # type: ignore[arg-type]
    click = {"action": "click", "tag": "span", "text": "Close", "frame": "main"}

    surface.record(dict(click))  # automation acting: its own DOM events are not a person
    control.transfer("paused", by="automation", reason="unknown dialog")
    surface.record(dict(click))  # nobody holds the lease, yet someone clicked
    control.transfer("human", by="alice", reason="took control")
    surface.record(dict(click))

    assert [(a["by"], a.get("without_lease", False)) for a in channel.human_actions] == [
        ("unknown", True),
        ("alice", False),
    ]
    assert len([e for e in _events(tmp_path) if e["type"] == "human_action"]) == 2
