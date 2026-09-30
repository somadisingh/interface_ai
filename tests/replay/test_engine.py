"""Replay engine against a live MockCore: every outcome bucket, recoveries, drift, approvals
and escalation."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from cua.evidence.log import EventLog
from cua.handoff.channel import (
    ApprovalResponse,
    AssistRequest,
    AssistResponse,
    HumanChannel,
    ScriptedChannel,
)
from cua.policy import Policy, PolicyEngine, Redactor, SecretStore
from cua.replay.engine import ReplayEngine
from cua.replay.support import CapabilityRegistry
from cua.schema import AppProfile, Capability, RunResult, load_app_profile, load_capability
from cua.surface.web import WebSurface
from mockcore import MockCoreConfig
from tests.conftest import PASSWORD, USERNAME, LiveServer

ROOT = Path(__file__).parents[2]
PROFILE = load_app_profile(ROOT / "apps/mockcore/profile.yaml")
POLICY = PolicyEngine(Policy.load(ROOT / "policy.yaml"))
READ = load_capability(ROOT / "tests/fixtures/read_savings_balance.yaml")
OPEN = load_capability(ROOT / "tests/fixtures/open_share_account.yaml")
SECRETS = {"MOCKCORE_USERNAME": USERNAME, "MOCKCORE_PASSWORD": PASSWORD}
OPEN_INPUTS = {
    "member_id": "12345",
    "account_type": "S01",
    "nickname": "Rainy day",
    "deposit": "100.00",
    "funding_suffix": "S00",
}


def replay(
    base_url: str,
    run_dir: Path,
    cap: Capability,
    inputs: dict[str, str],
    *,
    human: HumanChannel | None = None,
    profile: AppProfile = PROFILE,
    escalate: bool = False,
    during: Callable[[WebSurface], None] | None = None,
) -> RunResult:
    redactor = Redactor()
    log = EventLog(run_dir, "run-test", redactor)
    with WebSurface.launch(base_url, request_guard=POLICY.request_guard) as surface:
        engine = ReplayEngine(
            surface,
            profile=profile,
            policy=POLICY,
            registry=CapabilityRegistry(ROOT / "capabilities"),
            human=human or ScriptedChannel(),
            log=log,
            redactor=redactor,
            secrets=SecretStore(redactor, SECRETS),
            escalate=escalate,
            poll_ms=100,
        )
        if during is not None:
            engine.human = _wrap_during(engine.human, surface, during)
        return engine.run(cap, inputs)


def _wrap_during(
    inner: HumanChannel, surface: WebSurface, fn: Callable[[WebSurface], None]
) -> HumanChannel:
    class Operator:
        def approve(self, request: Any) -> ApprovalResponse:
            return inner.approve(request)

        def assist(self, request: AssistRequest) -> AssistResponse:
            fn(surface)  # the "human" operates the very same live page
            return AssistResponse(
                "resume",
                "test-operator",
                "dismissed the dialog",
                [{"action": "click", "target": "OK"}],
            )

    return Operator()


def events(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


@pytest.fixture
def base(live_mockcore: LiveServer, mockcore_url: str) -> Iterator[str]:
    yield mockcore_url


# ------------------------------------------------------------------ success & contract


def test_success_returns_typed_outputs_and_evidence(base: str, tmp_path: Path) -> None:
    result = replay(base, tmp_path, READ, {"member_id": "12345"})
    assert result.status == "success", result.error
    assert result.outputs == {"savings_balance": {"amount": "1520.33", "currency": "USD"}}
    assert result.drift_warnings == [] and result.recoveries == []
    types = [e["type"] for e in events(tmp_path)]
    assert types[0] == "run_started" and types[-1] == "run_finished"
    assert "subrun_started" in types  # sign-on ran as a required capability
    assert (tmp_path / "result.json").exists()
    assert list((tmp_path / "screenshots").glob("*.png"))


def test_logs_never_contain_secrets_or_raw_pii(base: str, tmp_path: Path) -> None:
    replay(base, tmp_path, READ, {"member_id": "12345"})
    text = (tmp_path / "events.jsonl").read_text() + (tmp_path / "result.json").read_text()
    assert PASSWORD not in text
    assert "1520.33" not in text and "1,520.33" not in text  # pii output masked in logs


# ------------------------------------------------------------------ business outcomes


@pytest.mark.parametrize(
    ("member_id", "code", "source"),
    [
        ("99999", "MEMBER_NOT_FOUND", "capability"),
        ("1234", "INVALID_MEMBER_ID", "capability"),
        ("55555", "PERMISSION_DENIED", "app_profile"),
    ],
)
def test_business_outcomes_are_results_not_errors(
    base: str, tmp_path: Path, member_id: str, code: str, source: str
) -> None:
    result = replay(base, tmp_path, READ, {"member_id": member_id})
    assert result.status == "business_outcome"
    assert result.outcome is not None
    assert (result.outcome.code, result.outcome.source) == (code, source)
    assert result.error is None and result.outputs == {}


def test_contract_violation_fails_before_touching_the_ui(base: str, tmp_path: Path) -> None:
    result = replay(base, tmp_path, READ, {"member_id": "abc"})
    assert result.status == "failed" and result.error is not None
    assert result.error.category == "INVALID_INPUT"
    assert "step_started" not in [e["type"] for e in events(tmp_path)]


# ------------------------------------------------------------------ recoverable conditions


@pytest.mark.parametrize(
    ("faults", "state"),
    [
        ("maintenance", "maintenance_notice"),
        ("modal", "password_expiry_notice"),
        ("session_timeout=3", "session_expired"),
    ],
)
def test_known_interruptions_are_recovered(
    live_mockcore: LiveServer, base: str, tmp_path: Path, faults: str, state: str
) -> None:
    live_mockcore.set_faults(faults)
    result = replay(base, tmp_path, READ, {"member_id": "12345"})
    assert result.status == "success", result.error
    assert state in [r.state for r in result.recoveries]
    assert result.outputs["savings_balance"] == {"amount": "1520.33", "currency": "USD"}


def test_slow_pages_are_waited_for(live_mockcore: LiveServer, base: str, tmp_path: Path) -> None:
    live_mockcore.set_faults("slow=1500")
    assert replay(base, tmp_path, READ, {"member_id": "12345"}).status == "success"


# ------------------------------------------------------------------ hard failures


def test_application_error_is_a_hard_failure_with_evidence(
    live_mockcore: LiveServer, base: str, tmp_path: Path
) -> None:
    live_mockcore.set_faults("error500=/members/1")
    result = replay(base, tmp_path, READ, {"member_id": "12345"})
    assert result.status == "failed" and result.error is not None
    assert result.error.category == "APP_ERROR"
    assert result.error.step_id == "open_member"
    assert result.error.evidence and (tmp_path / result.error.evidence[0]).exists()


def _profile_without(state: str) -> AppProfile:
    data = copy.deepcopy(PROFILE.model_dump(mode="json"))
    del data["states"][state]
    return AppProfile.model_validate(data)


def test_unrecognised_blocking_dialog_is_unknown_state(
    live_mockcore: LiveServer, base: str, tmp_path: Path
) -> None:
    live_mockcore.set_faults("modal")
    result = replay(
        base,
        tmp_path,
        READ,
        {"member_id": "12345"},
        profile=_profile_without("password_expiry_notice"),
    )
    assert result.status == "failed" and result.error is not None
    assert result.error.category == "UNKNOWN_STATE"
    assert result.error.expected and result.error.observed


def test_escalation_hands_the_live_session_to_a_human_and_resumes(
    live_mockcore: LiveServer, base: str, tmp_path: Path
) -> None:
    live_mockcore.set_faults("modal")

    def dismiss(surface: WebSurface) -> None:
        main = surface.page.frame(name="main")
        assert main is not None
        main.get_by_text("OK", exact=True).click()

    result = replay(
        base,
        tmp_path,
        READ,
        {"member_id": "12345"},
        escalate=True,
        profile=_profile_without("password_expiry_notice"),
        during=dismiss,
    )
    assert result.status == "success", result.error
    assert len(result.interventions) == 1
    kinds = [e["type"] for e in events(tmp_path)]
    assert "assist_requested" in kinds and "assist_resolved" in kinds


# ------------------------------------------------------------------ drift


def test_second_tenant_variant_succeeds_with_drift_warnings(tmp_path: Path) -> None:
    server = LiveServer(MockCoreConfig(variant="b"))
    server.start()
    try:
        result = replay(server.url, tmp_path, READ, {"member_id": "12345"})
    finally:
        server.stop()
    assert result.status == "success", result.error
    drifted = {w.target: w.used_strategy for w in result.drift_warnings}
    assert drifted["member_id_field"] == "attribute"
    assert drifted["savings_balance_cell"] == "css"


# ------------------------------------------------------------------ irreversible steps


def test_irreversible_step_waits_for_human_approval(
    live_mockcore: LiveServer, base: str, tmp_path: Path
) -> None:
    human = ScriptedChannel(approve=lambda r: ApprovalResponse(True, "supervisor"))
    result = replay(base, tmp_path, OPEN, OPEN_INPUTS, human=human)
    assert result.status == "success", result.error
    assert result.outputs["confirmation_number"] == "CNF-100001"
    assert [a.step for a in human.approvals] == ["confirm"]


def test_rejected_approval_stops_before_the_irreversible_action(
    live_mockcore: LiveServer, base: str, tmp_path: Path
) -> None:
    human = ScriptedChannel(approve=lambda r: ApprovalResponse(False, "supervisor", "wrong amount"))
    result = replay(base, tmp_path, OPEN, OPEN_INPUTS, human=human)
    assert result.status == "escalated" and result.error is not None
    assert result.error.category == "APPROVAL_DENIED"
    import httpx

    assert httpx.get(base + "/__admin/state").json()["opened"] == []  # nothing happened


def test_approved_capability_with_preapproved_step_runs_unattended(
    live_mockcore: LiveServer, base: str, tmp_path: Path
) -> None:
    data = OPEN.model_dump(mode="json")
    data["review"] = {"status": "approved", "reviewed_by": "reviewer"}
    next(s for s in data["steps"] if s["id"] == "confirm")["auto_approve_on_replay"] = True
    human = ScriptedChannel()
    result = replay(base, tmp_path, Capability.model_validate(data), OPEN_INPUTS, human=human)
    assert result.status == "success", result.error
    assert human.approvals == []


def test_validation_rejection_is_a_business_outcome(
    live_mockcore: LiveServer, base: str, tmp_path: Path
) -> None:
    result = replay(base, tmp_path, OPEN, OPEN_INPUTS | {"deposit": "1.00"})
    assert result.status == "business_outcome" and result.outcome is not None
    assert result.outcome.code == "VALIDATION_REJECTED"


def test_unknown_dialog_fails_without_escalation(
    live_mockcore: LiveServer, base: str, tmp_path: Path
) -> None:
    live_mockcore.set_faults("announcement")
    result = replay(base, tmp_path, READ, {"member_id": "12345"})
    assert result.status == "failed" and result.error is not None
    assert result.error.category == "UNKNOWN_STATE"
