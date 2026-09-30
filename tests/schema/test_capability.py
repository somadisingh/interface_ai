from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cua.schema import Capability, load_capability, save_capability
from cua.schema.yaml_io import load_yaml

FIXTURE = Path(__file__).parents[1] / "fixtures" / "read_savings_balance.yaml"
SIGN_ON = Path(__file__).parents[2] / "capabilities/mockcore/session.sign_on/1.0.0.yaml"


@pytest.fixture
def raw() -> dict[str, Any]:
    data = load_yaml(FIXTURE.read_text())
    assert isinstance(data, dict)
    return data


def _invalid(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        Capability.model_validate(data)


def test_fixture_and_sign_on_are_valid() -> None:
    cap = load_capability(FIXTURE)
    assert cap.ref == "mockcore/member.read_savings_balance@1.0.0"
    assert [s.id for s in cap.steps][0] == "open_member_search"
    assert cap.inputs["member_id"].type == "string"
    assert load_capability(SIGN_ON).requires == []


def test_round_trip_preserves_content_and_stamps_hash(tmp_path: Path) -> None:
    cap = load_capability(FIXTURE)
    out = save_capability(cap, tmp_path / "cap.yaml")
    again = load_capability(out)
    assert again.content_hash() == cap.content_hash()
    assert again.provenance.content_hash == cap.content_hash()


def test_content_hash_ignores_review_but_tracks_behaviour(raw: dict[str, Any]) -> None:
    base = Capability.model_validate(raw).content_hash()

    approved = copy.deepcopy(raw)
    approved["review"] = {"status": "approved", "reviewed_by": "reviewer"}
    assert Capability.model_validate(approved).content_hash() == base

    changed = copy.deepcopy(raw)
    changed["steps"][0]["timeout_ms"] = "20000"
    assert Capability.model_validate(changed).content_hash() != base


# ------------------------------------------------------------------ cross-reference checks


def test_unknown_target_is_rejected(raw: dict[str, Any]) -> None:
    raw["steps"][0]["action"]["target"] = "no_such_target"
    _invalid(raw, "unknown target 'no_such_target'")


def test_unknown_target_inside_checkpoint_is_rejected(raw: dict[str, Any]) -> None:
    raw["steps"][0]["expect"] = [{"kind": "visible", "target": "ghost"}]
    _invalid(raw, "unknown target 'ghost'")


def test_undeclared_input_template_is_rejected(raw: dict[str, Any]) -> None:
    raw["steps"][1]["action"]["value"] = "{{inputs.account_no}}"
    _invalid(raw, "undeclared input 'account_no'")


def test_unused_input_is_rejected(raw: dict[str, Any]) -> None:
    raw["inputs"]["branch"] = {"type": "string", "description": "unused"}
    _invalid(raw, "input 'branch' is declared but never used")


def test_secret_outside_fill_is_rejected(raw: dict[str, Any]) -> None:
    raw["steps"].insert(
        0, {"id": "go", "intent": "x", "action": {"type": "navigate", "path": "/{{secrets.TOKEN}}"}}
    )
    _invalid(raw, "secrets may only be used as a fill value")


def test_secret_in_locator_is_rejected(raw: dict[str, Any]) -> None:
    raw["targets"]["search_button"]["locators"][0]["text"] = "{{secrets.X}}"
    _invalid(raw, "secrets may only be used as a fill value")


def test_malformed_template_is_rejected(raw: dict[str, Any]) -> None:
    raw["steps"][1]["action"]["value"] = "{{ inputs }}"
    _invalid(raw, "malformed template")


def test_output_must_be_extracted_exactly_once(raw: dict[str, Any]) -> None:
    raw["outputs"]["member_name"] = {"type": "string", "description": "never read"}
    _invalid(raw, "output 'member_name' is declared but never extracted")


def test_extract_into_undeclared_output_is_rejected(raw: dict[str, Any]) -> None:
    raw["steps"][-1]["action"]["into"] = "balance"
    _invalid(raw, "undeclared output 'balance'")


def test_outcome_code_must_be_declared(raw: dict[str, Any]) -> None:
    raw["steps"][2]["outcomes"][0]["outcome"] = "NOT_DECLARED"
    _invalid(raw, "outcome 'NOT_DECLARED' is not declared")


def test_duplicate_step_ids_are_rejected(raw: dict[str, Any]) -> None:
    raw["steps"][1]["id"] = raw["steps"][0]["id"]
    _invalid(raw, "duplicate step id")


def test_auto_approve_only_on_irreversible_steps(raw: dict[str, Any]) -> None:
    raw["steps"][0]["auto_approve_on_replay"] = "true"
    _invalid(raw, "auto_approve_on_replay only applies to irreversible steps")


def test_approval_requires_a_reviewer(raw: dict[str, Any]) -> None:
    raw["review"] = {"status": "approved"}
    _invalid(raw, "must name its reviewer")


def test_semantic_locators_must_precede_web_only_fallbacks(raw: dict[str, Any]) -> None:
    raw["targets"]["search_button"]["locators"].reverse()
    _invalid(raw, "semantic strategies must come first")


def test_invalid_regex_in_condition_is_rejected(raw: dict[str, Any]) -> None:
    raw["success"] = [{"kind": "text", "pattern": "(unclosed"}]
    _invalid(raw, "invalid regex")


def test_unknown_fields_are_rejected(raw: dict[str, Any]) -> None:
    raw["steps"][0]["tiemout_ms"] = "5000"  # typo must not be silently ignored
    _invalid(raw, "tiemout_ms")


def test_bad_semver_and_ids_are_rejected(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["version"] = "1.0"
    _invalid(bad, "version")
    bad = copy.deepcopy(raw)
    bad["id"] = "ReadBalance"
    _invalid(bad, "id")
