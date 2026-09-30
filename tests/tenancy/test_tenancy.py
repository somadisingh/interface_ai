"""Cross-tenant reuse: overlays, the version gate, and one capability running on a second
institution's differently-configured instance of the same product."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cua.handoff.channel import ApprovalResponse, ScriptedChannel
from cua.runtime import open_runtime
from cua.schema import Capability, RunResult, load_capability
from cua.schema.tenancy import (
    CapabilityOverlay,
    OverlayError,
    Tenant,
    apply_overlay,
    version_in_range,
)
from cua.schema.yaml_io import load_yaml
from mockcore import MockCoreConfig
from tests.conftest import PASSWORD, USERNAME, LiveServer

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests/fixtures"
READ = load_capability(FIXTURES / "read_savings_balance.yaml")
OPEN = load_capability(FIXTURES / "open_share_account.yaml")
HARBOR = Tenant(FIXTURES / "tenants/harbor_valley_fcu")
SECRETS = {"MOCKCORE_USERNAME": USERNAME, "MOCKCORE_PASSWORD": PASSWORD}
OPEN_INPUTS = {
    "member_id": "12345",
    "account_type": "S01",
    "nickname": "Rainy day",
    "deposit": "100.00",
    "funding_suffix": "S00",
}


# ------------------------------------------------------------------ units


@pytest.mark.parametrize(
    ("version", "spec", "ok"),
    [
        ("4.2.1", ">=4.0,<5.0", True),
        ("4.3.0", ">=4.0,<4.3", False),
        ("5.0", ">=4.0,<5.0", False),
        ("4.3.0", "*", True),
        ("1.0.0", "==1.0", True),
    ],
)
def test_version_ranges(version: str, spec: str, ok: bool) -> None:
    assert version_in_range(version, spec) is ok


def _overlay(**changes: Any) -> CapabilityOverlay:
    data = load_yaml(
        (
            FIXTURES / "tenants/harbor_valley_fcu/overrides/member.read_savings_balance.yaml"
        ).read_text()
    )
    data.update(changes)
    return CapabilityOverlay.model_validate(data)


def test_prepend_keeps_base_locators_as_fallbacks() -> None:
    effective = apply_overlay(READ, _overlay())
    strategies = [loc.strategy for loc in effective.targets["member_id_field"].locators]
    assert strategies == ["anchor", "anchor", "attribute"]  # tenant first, then base
    assert effective.content_hash() != READ.content_hash()
    assert effective.steps == READ.steps


def test_replace_mode_and_errors() -> None:
    replaced = apply_overlay(
        READ,
        _overlay(
            targets={
                "search_button": {
                    "mode": "replace",
                    "locators": [{"strategy": "text", "text": "Find"}],
                }
            }
        ),
    )
    assert len(replaced.targets["search_button"].locators) == 1
    with pytest.raises(OverlayError, match="unknown target"):
        apply_overlay(
            READ, _overlay(targets={"ghost": {"locators": [{"strategy": "text", "text": "x"}]}})
        )
    with pytest.raises(OverlayError, match="applies to"):
        apply_overlay(READ, _overlay(applies_to=">=2.0.0"))
    with pytest.raises(OverlayError, match="not 'member.read_savings_balance'"):
        apply_overlay(READ, _overlay(capability="member.other"))


def test_step_insertion_is_validated() -> None:
    overlay = HARBOR.overlays["member.open_share_account"]
    effective = apply_overlay(OPEN, overlay)
    ids = [s.id for s in effective.steps]
    assert ids[ids.index("continue_to_review") + 1] == "record_consent"


# ------------------------------------------------------------------ live second tenant


@pytest.fixture(scope="module")
def harbor() -> Iterator[LiveServer]:
    server = LiveServer(MockCoreConfig(variant="b"))
    server.start()
    yield server
    server.stop()


def _run(
    url: str,
    runs: Path,
    cap: Capability,
    inputs: dict[str, str],
    *,
    tenant: Tenant | None,
    human: ScriptedChannel | None = None,
) -> tuple[RunResult, Path]:
    with open_runtime(
        url, human=human or ScriptedChannel(), root=ROOT, runs_dir=runs, secrets_source=SECRETS
    ) as rt:
        rt.engine.tenant = tenant
        return rt.engine.run(cap, inputs), rt.log.run_dir


def test_overlay_removes_drift_on_the_second_tenant(harbor: LiveServer, tmp_path: Path) -> None:
    harbor.reset()
    base, _ = _run(harbor.url, tmp_path, READ, {"member_id": "12345"}, tenant=None)
    assert base.status == "success" and len(base.drift_warnings) >= 3  # degrades gracefully

    tuned, run_dir = _run(harbor.url, tmp_path, READ, {"member_id": "12345"}, tenant=HARBOR)
    assert tuned.status == "success", tuned.error
    assert tuned.drift_warnings == []
    assert (tuned.tenant, tuned.app_version) == ("harbor_valley_fcu", "4.3.0")
    assert tuned.outputs["savings_balance"] == {"amount": "1520.33", "currency": "USD"}
    types = [json.loads(x)["type"] for x in (run_dir / "events.jsonl").read_text().splitlines()]
    assert "overlay_applied" in types


def test_overlay_adds_the_tenant_specific_step(harbor: LiveServer, tmp_path: Path) -> None:
    harbor.reset()
    approve = ScriptedChannel(approve=lambda r: ApprovalResponse(True, "supervisor"))
    without, _ = _run(harbor.url, tmp_path, OPEN, OPEN_INPUTS, tenant=None, human=approve)
    assert without.status == "failed"  # base flow can't complete on this tenant

    harbor.reset()
    approve = ScriptedChannel(approve=lambda r: ApprovalResponse(True, "supervisor"))
    with_overlay, _ = _run(harbor.url, tmp_path, OPEN, OPEN_INPUTS, tenant=HARBOR, human=approve)
    assert with_overlay.status == "success", with_overlay.error
    assert with_overlay.outputs["confirmation_number"] == "CNF-100001"


def test_version_gate_refuses_unvalidated_versions(harbor: LiveServer, tmp_path: Path) -> None:
    harbor.reset()
    data = READ.model_dump(mode="json")
    data["app"]["product_versions"] = ">=4.0,<4.3"
    narrow = Capability.model_validate(data)
    result, _ = _run(harbor.url, tmp_path, narrow, {"member_id": "12345"}, tenant=None)
    assert result.status == "failed" and result.error is not None
    assert result.error.category == "UNKNOWN_STATE"
    assert "4.3.0" in result.error.message and result.app_version == "4.3.0"
