"""Regression tests for issues found in a pre-submission review: double submission on an
uncertain click, approval bypasses, data leaking into screenshots, positional reads, runs
ending without a result, and handoff edge cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cua.agent.llm import make_llm
from cua.cli import app
from cua.compiler.compile import sanitize_target
from cua.evidence.log import EventLog
from cua.handoff.channel import ApprovalRequest, ApprovalResponse, ScriptedChannel
from cua.handoff.control import SessionControl
from cua.handoff.operator import OperatorChannel
from cua.handoff.queue import InterventionQueue
from cua.policy import Policy, PolicyEngine, Redactor
from cua.recorder.trace import Templater
from cua.schema import Capability, load_capability
from cua.schema.locators import TargetSpec
from cua.schema.tenancy import CapabilityOverlay, Tenant, apply_overlay
from cua.surface import dom_scripts
from cua.surface.base import ActionResult, Resolved
from cua.surface.web import WebSurface
from tests.conftest import LiveServer
from tests.replay.test_engine import OPEN, OPEN_INPUTS, READ, ROOT, events, replay
from tests.surface.test_web import sign_on

POLICY = PolicyEngine(Policy.load(ROOT / "policy.yaml"))


# ------------------------------------------------------------------ double submission


def test_uncertain_click_is_verified_not_repeated(
    live_mockcore: LiveServer, mockcore_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A click that reached the app but did not finish in time must never be re-sent: the
    checkpoint decides whether it worked. Here Confirm goes through but reports uncertain."""
    original = WebSurface.click

    def click(self: WebSurface, element: Resolved) -> ActionResult:
        result = original(self, element)
        if "Confirm" in self.label_of(element) and result.completed:
            return ActionResult(False, "uncertain", "simulated: response still in flight")
        return result

    monkeypatch.setattr(WebSurface, "click", click)
    human = ScriptedChannel(approve=lambda r: ApprovalResponse(True, "supervisor"))
    result = replay(mockcore_url, tmp_path, OPEN, OPEN_INPUTS, human=human)
    assert result.status == "success", result.error
    assert len(httpx.get(mockcore_url + "/__admin/state").json()["opened"]) == 1
    assert "action_uncertain" in [e["type"] for e in events(tmp_path)]


# ------------------------------------------------------------------ approval bypasses


def test_enter_is_classified_by_the_form_it_would_submit(
    live_mockcore: LiveServer, mockcore_url: str
) -> None:
    with WebSurface.launch(mockcore_url) as surface:
        assert surface.navigate("/login").completed
        field = surface.page.locator("input[name=uid]")
        field.focus()
        label = surface.press_label("Enter")  # no target: whatever has focus
        assert "Sign On" in label
        assert "Sign On" not in surface.press_label("Tab")  # Tab submits nothing
    assert POLICY.classify("press", "Member ID: | Confirm & Open Account") == "irreversible"


def test_overlay_does_not_inherit_the_base_approval() -> None:
    data = OPEN.model_dump(mode="json")
    data["review"] = {"status": "approved", "reviewed_by": "reviewer"}
    approved = Capability.model_validate(data)
    overlay = CapabilityOverlay.model_validate(
        {
            "tenant": "harbor_valley_fcu",
            "capability": OPEN.id,
            "applies_to": "*",
            "reason": "test",
            "targets": {"confirm_button": {"locators": [{"strategy": "text", "text": "Go"}]}},
        }
    )
    effective = apply_overlay(approved, overlay)
    assert effective.review.status == "draft"


# ------------------------------------------------------------------ data in screenshots


def test_typed_values_and_classified_fields_are_masked(
    live_mockcore: LiveServer, mockcore_url: str
) -> None:
    with WebSurface.launch(mockcore_url) as surface:
        sign_on(surface)
        main = surface.page.frame(name="main")
        assert main is not None
        main.goto(mockcore_url + "/members/search")
        main.locator("input[name=mid]").fill("12345")
        spec = {"patterns": [r"(?<![A-Za-z0-9])12345(?![A-Za-z0-9])"], "labels": []}
        assert main.evaluate(dom_scripts.MARK_TEXT_MATCHES, spec) >= 1
        assert main.locator("input[name=mid][data-cua-mask]").count() == 1

        main.goto(mockcore_url + "/members/12345")
        spec = {"patterns": [], "labels": ["Name", "Date of Birth", "Current Balance"]}
        main.evaluate(dom_scripts.MARK_TEXT_MATCHES, spec)
        masked = main.locator("[data-cua-mask]").all_inner_texts()
        assert "SAMPLE, JANE Q" in masked and "1984-03-02" in masked
        assert "$1,520.33" in masked and "$845.10" in masked  # the whole balance column
        assert "Name" not in masked and "Suffix" not in masked  # labels stay readable

        # ...and the same values never reach the log as "observed" text either
        surface.sensitive_labels = ["Name", "Date of Birth", "Current Balance"]
        observed = surface.text_excerpt(5000, masked=True)
        assert "Date of Birth" in observed and "[REDACTED]" in observed
        assert "JANE" not in observed and "1,520.33" not in observed
        assert "JANE" in surface.text_excerpt(5000)  # conditions still see the real page


def test_secrets_are_redacted_whatever_their_case() -> None:
    r = Redactor()
    r.add_secret("svc-cua")
    assert r.text("User: SVC-CUA signed on") == "User: [SECRET] signed on"


# ------------------------------------------------------------------ positional fallbacks


def _spec(*locators: dict[str, Any]) -> TargetSpec:
    return TargetSpec.model_validate({"description": "t", "locators": list(locators)})


def test_values_and_input_keyed_targets_never_fall_back_to_position() -> None:
    cell = {"strategy": "table_cell", "row_text": "S00", "column_header": "Current Balance"}
    css = {"strategy": "css", "selector": "table:nth-of-type(2) td:nth-of-type(4)"}
    assert [loc.strategy for loc in sanitize_target(_spec(cell, css), read=True).locators] == [
        "table_cell"
    ]
    link = {"strategy": "role", "role": "link", "name": "{{inputs.member_id}}"}
    anchor = {"strategy": "anchor", "anchor_text": "SAMPLE, JANE Q", "relation": "same_row"}
    kept = sanitize_target(_spec(link, anchor, css), read=False).locators
    assert [loc.strategy for loc in kept] == ["role"]  # no record data, no row position
    button = {"strategy": "text", "text": "Search"}
    assert len(sanitize_target(_spec(button, css), read=False).locators) == 2  # a button may


def test_selectors_are_never_templated() -> None:
    t = Templater({"member_id": "1"}).target(
        _spec(
            {"strategy": "text", "text": "1"},
            {"strategy": "css", "selector": "tr:nth-of-type(1) > a"},
        )
    )
    assert t.locators[0].model_dump()["text"] == "{{inputs.member_id}}"
    assert t.locators[1].model_dump()["selector"] == "tr:nth-of-type(1) > a"


# ------------------------------------------------------------------ always a result


def test_a_broken_overlay_still_ends_with_a_structured_result(
    live_mockcore: LiveServer, mockcore_url: str, tmp_path: Path
) -> None:
    folder = tmp_path / "tenant"
    (folder / "overrides").mkdir(parents=True)
    (folder / "tenant.yaml").write_text(
        "schema_version: '1.0'\ntenant: broken_cu\nname: Broken\nproduct: mockcore\n"
        f"base_url: {mockcore_url}\n"
    )
    (folder / "overrides" / "x.yaml").write_text(
        "schema_version: '1.0'\ntenant: broken_cu\ncapability: member.read_savings_balance\n"
        "applies_to: '*'\nreason: test\ntargets:\n  no_such_target:\n"
        "    locators: [{strategy: text, text: X}]\n"
    )
    from cua.policy import SecretStore
    from cua.replay.engine import ReplayEngine
    from cua.replay.support import CapabilityRegistry
    from cua.schema import load_app_profile

    redactor = Redactor()
    log = EventLog(tmp_path / "run", "run-x", redactor)
    with WebSurface.launch(mockcore_url, request_guard=POLICY.request_guard) as surface:
        engine = ReplayEngine(
            surface,
            profile=load_app_profile(ROOT / "apps/mockcore/profile.yaml"),
            policy=POLICY,
            registry=CapabilityRegistry(ROOT / "capabilities"),
            human=ScriptedChannel(),
            log=log,
            redactor=redactor,
            secrets=SecretStore(redactor, {}),
            tenant=Tenant(folder),
        )
        result = engine.run(READ, {"member_id": "12345"})
    assert result.status == "failed" and result.error is not None
    assert result.error.category == "CONFIGURATION_ERROR"
    assert json.loads((tmp_path / "run" / "result.json").read_text())["status"] == "failed"


def test_sign_on_failure_keeps_its_real_cause(tmp_path: Path) -> None:
    result = replay("http://example.com/", tmp_path, READ, {"member_id": "12345"})
    assert result.status == "failed" and result.error is not None
    assert result.error.category == "POLICY_VIOLATION"


# ------------------------------------------------------------------ handoff


def test_resume_does_not_repeat_a_step_the_operator_completed(
    live_mockcore: LiveServer, mockcore_url: str, tmp_path: Path
) -> None:
    live_mockcore.set_faults("announcement")  # unknown dialog blocks the Search click

    def operator_closes_dialog_and_searches(surface: WebSurface) -> None:
        main = surface.page.frame(name="main")
        assert main is not None
        main.get_by_text("Close", exact=True).click()
        main.get_by_text("Search", exact=True).click()
        main.wait_for_load_state("load")

    result = replay(
        mockcore_url,
        tmp_path,
        READ,
        {"member_id": "12345"},
        escalate=True,
        during=operator_closes_dialog_and_searches,
    )
    assert result.status == "success", result.error
    skipped = [e for e in events(tmp_path) if e["type"] == "action_skipped"]
    assert [e["step"] for e in skipped] == ["submit_search"]


class _FakeSurface:
    def install_human_recorder(self, cb: Any) -> None:
        pass

    def wait(self, ms: int) -> None:
        import time

        time.sleep(ms / 1000)


def test_an_unclaimed_request_times_out(tmp_path: Path) -> None:
    channel = OperatorChannel(
        _FakeSurface(),  # type: ignore[arg-type]
        SessionControl(),
        InterventionQueue(),
        EventLog(tmp_path, "run-x", Redactor()),
        poll_ms=10,
        max_wait_seconds=0.1,
    )
    request = ApprovalRequest("apr-1", "run-x", "cap", "confirm", "click", "irreversible", "r", "u")
    response = channel.approve(request)
    assert (response.approved, response.by) == (False, "timeout")


def test_the_operator_console_is_off_limits_to_automation() -> None:
    engine = PolicyEngine(Policy.load(ROOT / "policy.yaml"))
    engine.denied_origins.add("http://127.0.0.1:8766")
    assert not engine.check_url("http://127.0.0.1:8766/").allowed
    assert engine.check_url("http://127.0.0.1:8765/app").allowed


# ------------------------------------------------------------------ CLI


def test_cli_reports_unknown_capabilities_without_a_traceback() -> None:
    result = CliRunner().invoke(app, ["replay", "mockcore/no.such", "--input", "a=1"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # a clean exit, not a crash
    assert "no capability" in result.output


def test_cli_rejects_a_repeated_input() -> None:
    result = CliRunner().invoke(
        app,
        [
            "replay",
            "mockcore/member.read_savings_balance",
            "--input",
            "member_id=1",
            "--input",
            "member_id=2",
        ],
    )
    assert result.exit_code != 0 and "more than once" in result.output


def test_discovery_needs_a_key_before_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CUA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CUA_MODEL", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        make_llm()


def test_real_capability_files_are_loadable() -> None:
    for path in (ROOT / "capabilities").rglob("*.yaml"):
        load_capability(path)


def test_error_reports_describe_conditions_readably() -> None:
    from cua.replay.engine import _describe
    from cua.schema.conditions import Visible

    assert _describe(Visible(target="current_balance_value")) == "visible current_balance_value"


def test_merge_target_is_checked_before_any_model_call(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "discover",
            "--capability",
            "member.read_savings_balance",
            "--goal",
            "g",
            "--param",
            "member_id=99999",
            "--merge-into",
            str(tmp_path / "missing.yaml"),
        ],
    )
    assert result.exit_code == 1 and "--merge-into" in result.output
