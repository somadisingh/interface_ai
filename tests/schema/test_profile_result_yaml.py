from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cua.schema import AppProfile, RunResult, load_app_profile, templates
from cua.schema.yaml_io import ArtifactLoadError, load_yaml

PROFILE = Path(__file__).parents[2] / "apps/mockcore/profile.yaml"


# ------------------------------------------------------------------ strict YAML


def test_strict_loader_keeps_scalars_as_strings() -> None:
    data = load_yaml("member_id: 01234\nanswer: no\ntime: 12:30\nflag: true\nnothing: null\n")
    assert data == {
        "member_id": "01234",  # plain YAML 1.1 would give the octal integer 668
        "answer": "no",  # plain YAML 1.1 would give False
        "time": "12:30",  # plain YAML 1.1 would give 750 (sexagesimal)
        "flag": True,
        "nothing": None,
    }


def test_strict_loader_rejects_duplicate_keys() -> None:
    with pytest.raises(ArtifactLoadError, match="duplicate key 'id'"):
        load_yaml("id: a\nid: b\n")


# ------------------------------------------------------------------ templates


def test_template_references_and_rendering() -> None:
    text = "{{inputs.member_id}}/{{ secrets.PW }}"
    assert templates.references(text) == [("inputs", "member_id"), ("secrets", "PW")]
    assert templates.render(text, {"member_id": "7"}, {"PW": "s"}) == "7/s"
    with pytest.raises(templates.TemplateError, match="no value for inputs.member_id"):
        templates.render(text, {}, {"PW": "s"})
    with pytest.raises(templates.TemplateError, match="malformed"):
        templates.references("{{inputs.}}")


# ------------------------------------------------------------------ app profile


@pytest.fixture
def raw_profile() -> dict[str, Any]:
    data = load_yaml(PROFILE.read_text())
    assert isinstance(data, dict)
    return data


def test_mockcore_profile_is_valid() -> None:
    profile = load_app_profile(PROFILE)
    assert profile.session.sign_on_capability == "session.sign_on"
    assert set(profile.business_outcomes()) == {
        "PERMISSION_DENIED",
        "RECORD_NOT_FOUND",
        "TRANSACTION_NOT_PROCESSED",
    }
    assert profile.states["session_expired"].handler is not None
    assert profile.states["session_expired"].handler.then == "restart_capability"


def test_recoverable_state_needs_handler(raw_profile: dict[str, Any]) -> None:
    del raw_profile["states"]["maintenance_notice"]["handler"]
    with pytest.raises(ValidationError, match="interstitial state needs a handler"):
        AppProfile.model_validate(raw_profile)


def test_business_state_needs_outcome_and_no_handler(raw_profile: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw_profile)
    del bad["states"]["access_denied"]["outcome"]
    with pytest.raises(ValidationError, match="'outcome' is required"):
        AppProfile.model_validate(bad)
    bad = copy.deepcopy(raw_profile)
    bad["states"]["access_denied"]["handler"] = {
        "run_capability": "session.sign_on",
        "then": "resume",
    }
    with pytest.raises(ValidationError, match="must not have a handler"):
        AppProfile.model_validate(bad)


def test_profile_handler_target_must_exist(raw_profile: dict[str, Any]) -> None:
    raw_profile["states"]["maintenance_notice"]["handler"]["actions"][0]["target"] = "nope"
    with pytest.raises(ValidationError, match="unknown target 'nope'"):
        AppProfile.model_validate(raw_profile)


# ------------------------------------------------------------------ run result


def _result(**kw: Any) -> RunResult:
    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    base: dict[str, Any] = {
        "run_id": "run_1",
        "capability": "mockcore/member.read_savings_balance@1.0.0",
        "content_hash": "sha256:x",
        "started_at": t0,
        "finished_at": t0 + timedelta(seconds=2),
    }
    base.update(kw)
    return RunResult.model_validate(base)


def test_success_result_with_money_output() -> None:
    r = _result(
        status="success", outputs={"savings_balance": {"amount": "1520.33", "currency": "USD"}}
    )
    assert r.duration_ms == 2000


def test_business_outcome_is_not_an_error() -> None:
    r = _result(
        status="business_outcome",
        outcome={
            "code": "MEMBER_NOT_FOUND",
            "description": "No such member",
            "retryable": False,
            "step_id": "submit_search",
            "source": "capability",
        },
    )
    assert r.error is None
    with pytest.raises(ValidationError, match="business_outcome requires 'outcome'"):
        _result(status="business_outcome")


def test_failed_result_needs_error_and_returns_no_outputs() -> None:
    err = {"category": "APP_ERROR", "message": "error page", "step_id": "open_member"}
    assert _result(status="failed", error=err).error is not None
    with pytest.raises(ValidationError, match="failed requires 'error'"):
        _result(status="failed")
    with pytest.raises(ValidationError, match="outputs are only returned on success"):
        _result(status="failed", error=err, outputs={"savings_balance": "1"})


def test_unknown_failure_category_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _result(status="failed", error={"category": "OOPS", "message": "x"})


def test_escalated_needs_an_intervention() -> None:
    with pytest.raises(ValidationError, match="escalated requires"):
        _result(status="escalated")
    assert _result(status="escalated", interventions=["int_1"]).interventions == ["int_1"]
