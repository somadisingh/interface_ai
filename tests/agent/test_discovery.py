"""Discovery end to end, offline: a scripted model drives MockCore, the trace compiles into a
capability, the capability replays deterministically, and a negative discovery run teaches
it a business outcome."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cua.agent.discovery import DiscoveryAgent, DiscoverySpec
from cua.agent.llm import ScriptedClient
from cua.compiler.compile import CompileError, compile_trace, merge_outcome
from cua.handoff.channel import ScriptedChannel
from cua.policy import Redactor
from cua.recorder.trace import DiscoveryTrace
from cua.runtime import open_runtime
from cua.schema import Capability, RunResult, load_capability, save_capability
from cua.schema.capability import OutputSpec
from tests.conftest import PASSWORD, USERNAME, LiveServer

ROOT = Path(__file__).parents[2]
SECRETS = {"MOCKCORE_USERNAME": USERNAME, "MOCKCORE_PASSWORD": PASSWORD}
OUTPUTS = {
    "savings_balance": OutputSpec(
        type="money", sensitivity="pii", description="Share Savings balance"
    )
}


def ref(pattern: str) -> Callable[[str], str]:
    def find(observation: str) -> str:
        for line in observation.splitlines():
            if re.search(pattern, line):
                m = re.search(r"\[(e\d+)\]", line)
                if m:
                    return m.group(1)
        raise AssertionError(f"no element matching {pattern!r} in:\n{observation}")

    return find


def act(
    tool: str, target: str | None = None, **args: Any
) -> Callable[[str], tuple[str, dict[str, Any]]]:
    def step(observation: str) -> tuple[str, dict[str, Any]]:
        payload = {"reason": f"{tool} step", **args}
        if target is not None:
            payload["ref"] = ref(target)(observation)
        return tool, payload

    return step


def search_steps(member_id: str) -> list[Callable[[str], tuple[str, dict[str, Any]]]]:
    return [
        act("click", r'link · "Member Search"'),
        act("fill", r"textbox .*row: Member ID:", value=member_id),
        act("click", r'clickable · "Search"'),
    ]


HAPPY = [
    *search_steps("12345"),
    act("click", r'link · "12345"'),
    act("extract", r"cell · .*column: Current Balance · row: S00", output="savings_balance"),
    act("finish", success=True, summary="Read the member's savings balance"),
]
NOT_FOUND = [
    *search_steps("99999"),
    act(
        "finish",
        success=False,
        summary="No member matches that member number",
        outcome_code="MEMBER_NOT_FOUND",
        outcome_text="NO MEMBERS FOUND MATCHING SEARCH CRITERIA",
    ),
]


def discover(base: str, runs: Path, script: list[Any], member_id: str) -> DiscoveryTrace:
    human = ScriptedChannel()
    with open_runtime(
        base, human=human, mode="discovery", root=ROOT, runs_dir=runs, secrets_source=SECRETS
    ) as rt:
        agent = DiscoveryAgent(
            rt.surface,
            ScriptedClient(script),
            engine=rt.engine,
            policy=rt.policy,
            human=human,
            log=rt.log,
            model_redactor=Redactor(),
            max_steps=12,
        )
        return agent.run(
            DiscoverySpec(
                capability_id="member.read_savings_balance",
                product="mockcore",
                goal=f"Look up member {member_id} and read their current savings balance",
                params={"member_id": member_id},
                outputs=OUTPUTS,
                requires=["session.sign_on"],
            )
        )


def replay(base: str, runs: Path, cap: Capability, member_id: str) -> RunResult:
    with open_runtime(
        base, human=ScriptedChannel(), root=ROOT, runs_dir=runs, secrets_source=SECRETS
    ) as rt:
        return rt.engine.run(cap, {"member_id": member_id})


@pytest.fixture
def base(live_mockcore: LiveServer, mockcore_url: str) -> str:
    return mockcore_url


def test_discover_compile_replay_and_learn_an_outcome(base: str, tmp_path: Path) -> None:
    # 1. discovery (scripted model) -> trace
    trace = discover(base, tmp_path / "runs", HAPPY, "12345")
    assert trace.status == "succeeded", trace.summary
    assert "12345" not in trace.model_dump_json()  # templated at record time
    assert (
        trace.goal == "Look up member {{inputs.member_id}} and read their current savings balance"
    )

    # 2. compile -> capability (draft) with readable names and derived checkpoints
    cap = compile_trace(trace, product_versions=">=4.0,<5.0")
    path = save_capability(cap, tmp_path / "cap.yaml")
    cap = load_capability(path)
    assert cap.review.status == "draft" and cap.provenance.source == "discovery"
    assert set(cap.targets) == {
        "member_search_link",
        "member_id_field",
        "search_button",
        "member_id_link",
        "current_balance_value",
    }
    first = cap.steps[0]
    assert first.expect and first.expect[0].model_dump()["target"] == "member_id_field"
    assert cap.targets["member_id_field"].locators[0].strategy == "anchor"
    assert "{{inputs.member_id}}" in cap.targets["member_id_link"].model_dump_json()

    # 3. deterministic replay with other inputs
    ok = replay(base, tmp_path / "runs", cap, "34567")
    assert ok.status == "success", ok.error
    assert ok.outputs["savings_balance"] == {"amount": "10250.00", "currency": "USD"}

    # 4. without knowledge of the not-found screen, a bad member id is a hard failure...
    unknown = replay(base, tmp_path / "runs", cap, "99999")
    assert unknown.status == "failed" and unknown.error is not None
    assert unknown.error.category == "CHECKPOINT_FAILED"

    # 5. ...until a negative discovery run teaches the capability that business outcome
    negative = discover(base, tmp_path / "runs", NOT_FOUND, "99999")
    assert negative.status == "outcome" and negative.outcome_code == "MEMBER_NOT_FOUND"
    merged = merge_outcome(cap, negative)
    assert merged.version == "1.1.0" and "MEMBER_NOT_FOUND" in merged.outcomes
    known = replay(base, tmp_path / "runs", merged, "99999")
    assert known.status == "business_outcome" and known.outcome is not None
    assert known.outcome.code == "MEMBER_NOT_FOUND"


def test_discovery_log_is_redacted_and_complete(base: str, tmp_path: Path) -> None:
    trace = discover(base, tmp_path / "runs", HAPPY, "12345")
    run_dir = tmp_path / "runs" / trace.run_id
    text = (run_dir / "events.jsonl").read_text()
    assert PASSWORD not in text
    assert "1,520.33" not in text and "1520.33" not in text  # pii output masked
    types = [json.loads(line)["type"] for line in text.splitlines()]
    assert types.count("agent_decision") == len(HAPPY)
    assert (run_dir / "trace.json").exists()


def test_irreversible_action_in_discovery_needs_approval(base: str, tmp_path: Path) -> None:
    script = [
        *search_steps("12345"),
        act("click", r'link · "12345"'),
        act("click", r'link · "Open Sub-Account"'),
        act("select", r"combobox .*row: Account Type:", option="S01"),
        act("fill", r"textbox .*row: Opening Deposit:", value="25.00"),
        act("select", r"combobox .*row: Fund From:", option="S00"),
        act("click", r'clickable · "Continue"'),
        act("click", r'button · "Confirm & Open Account"'),
        act("finish", success=False, summary="Approval was refused"),
    ]
    trace = discover(base, tmp_path / "runs", script, "12345")
    confirm = [s for s in trace.steps if s.target_label == "Confirm & Open Account"]
    assert len(confirm) == 1
    assert (confirm[0].verdict, confirm[0].result) == ("require_approval", "rejected")
    assert confirm[0].risk == "irreversible"
    assert trace.interventions  # the approval request is on record
    import httpx

    assert httpx.get(base + "/__admin/state").json()["opened"] == []  # nothing was submitted


def test_unfinished_trace_does_not_compile(base: str, tmp_path: Path) -> None:
    trace = discover(base, tmp_path / "runs", search_steps("12345"), "12345")
    with pytest.raises(CompileError):
        compile_trace(trace)


def test_stuck_agent_is_escalated(base: str, tmp_path: Path) -> None:
    same = act("click", r'link · "Home"')
    trace = discover(base, tmp_path / "runs", [same, same, same, same], "12345")
    assert trace.interventions, "a stuck agent must raise an intervention request"
    assert trace.status == "aborted"  # unattended channel aborts
