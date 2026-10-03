"""Cross-tenant reuse: one capability per vendor product, specialised per institution.

Two layers (design choice "A+B"):

* **Named targets (B).** Capabilities refer to elements by semantic names
  (``member_id_field``), never by selectors inline. A tenant whose screens differ only in
  wording or layout overrides the *locator bundle* of a name; the flow itself is untouched.
* **Overlays (A).** A small, reviewable per-tenant file patches only what differs: target
  locators (prepended by default, so the base locators remain as fallbacks and drift is
  still detected), extra targets, and extra steps inserted after a named step (e.g. a
  tenant that requires a consent checkbox before submitting).

A ``TenantConfig`` names the institution, its instance URL, the product version it runs, and
optional app-profile overrides (e.g. a differently-worded maintenance notice).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from cua.schema.app_profile import AppProfile, KnownState
from cua.schema.capability import Capability, Step
from cua.schema.common import SCHEMA_VERSION, DottedId, Identifier, Model
from cua.schema.locators import FrameRef, Locator, TargetSpec
from cua.schema.yaml_io import load_yaml

_COMPARATOR = re.compile(r"^(>=|<=|==|>|<)?\s*(\d+(?:\.\d+)*)$")


def _vtuple(v: str) -> tuple[int, ...]:
    parts = [int(p) for p in v.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def version_in_range(version: str, spec: str) -> bool:
    """``version_in_range("4.3.0", ">=4.0,<5.0")``. ``*`` or empty matches everything."""
    spec = spec.strip()
    if spec in ("", "*"):
        return True
    v = _vtuple(version)
    for clause in spec.split(","):
        m = _COMPARATOR.match(clause.strip())
        if not m:
            raise ValueError(f"bad version range clause {clause!r}")
        op, bound = m.group(1) or "==", _vtuple(m.group(2))
        ok = {">=": v >= bound, "<=": v <= bound, ">": v > bound, "<": v < bound, "==": v == bound}[
            op
        ]
        if not ok:
            return False
    return True


class TargetOverride(Model):
    mode: Literal["prepend", "replace"] = "prepend"
    """``prepend``: tenant locators first, base locators kept as fallbacks (default)."""
    locators: list[Locator] = Field(min_length=1)
    description: str | None = None
    frame: list[FrameRef] | None = None


class StepInsertion(Model):
    after: Identifier
    steps: list[Step] = Field(min_length=1)


class CapabilityOverlay(Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    tenant: Identifier
    capability: DottedId
    applies_to: str = "*"
    """Capability versions this overlay was written against, e.g. ``>=1.0.0,<2.0.0``."""
    reason: str
    targets: dict[Identifier, TargetOverride] = Field(default_factory=dict)
    add_targets: dict[Identifier, TargetSpec] = Field(default_factory=dict)
    insert_steps: list[StepInsertion] = Field(default_factory=list)


class ProfileOverrides(Model):
    targets: dict[Identifier, TargetSpec] = Field(default_factory=dict)
    states: dict[Identifier, KnownState] = Field(default_factory=dict)


class TenantConfig(Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    tenant: Identifier
    name: str
    product: Identifier
    base_url: str
    product_version: str | None = None
    """Version this institution runs (informational; the live page is authoritative)."""
    profile_overrides: ProfileOverrides = Field(default_factory=ProfileOverrides)

    @model_validator(mode="after")
    def _url(self) -> TenantConfig:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        return self


class OverlayError(ValueError):
    pass


def apply_overlay(cap: Capability, overlay: CapabilityOverlay) -> Capability:
    """Return the effective capability for a tenant. The result is fully re-validated, and
    its content hash differs from the base (it is a different artifact)."""
    if overlay.capability != cap.id:
        raise OverlayError(f"overlay is for {overlay.capability!r}, not {cap.id!r}")
    if not version_in_range(cap.version, overlay.applies_to):
        raise OverlayError(f"overlay applies to {overlay.applies_to}, capability is {cap.version}")
    data = cap.model_dump(mode="json")
    for name, override in overlay.targets.items():
        if name not in data["targets"]:
            raise OverlayError(f"overlay overrides unknown target {name!r}")
        target = data["targets"][name]
        new = [loc.model_dump(mode="json") for loc in override.locators]
        target["locators"] = (
            new if override.mode == "replace" else _dedupe(new + target["locators"])
        )
        if override.description:
            target["description"] = override.description
        if override.frame is not None:
            target["frame"] = [f.model_dump(mode="json") for f in override.frame]
    for name, spec in overlay.add_targets.items():
        if name in data["targets"]:
            raise OverlayError(f"target {name!r} already exists; override it instead")
        data["targets"][name] = spec.model_dump(mode="json")
    for insertion in overlay.insert_steps:
        ids = [s["id"] for s in data["steps"]]
        if insertion.after not in ids:
            raise OverlayError(f"cannot insert after unknown step {insertion.after!r}")
        at = ids.index(insertion.after) + 1
        data["steps"][at:at] = [s.model_dump(mode="json") for s in insertion.steps]
    if data["review"]["status"] == "approved":
        # Approval covers what the reviewer read: the base capability. An overlay can change
        # targets and add steps, so the effective capability runs as a draft (irreversible
        # steps need a person) until overlays get a review of their own.
        data["review"] = {
            "status": "draft",
            "notes": f"tenant overlay ({overlay.tenant}) applied: the base capability's "
            "approval does not carry over",
        }
    return Capability.model_validate(data)


def _dedupe(locators: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: list[dict[str, object]] = []
    for loc in locators:
        if loc not in seen:
            seen.append(loc)
    return seen


def apply_profile_overrides(profile: AppProfile, overrides: ProfileOverrides) -> AppProfile:
    data = profile.model_dump(mode="json")
    data["targets"].update({k: v.model_dump(mode="json") for k, v in overrides.targets.items()})
    data["states"].update({k: v.model_dump(mode="json") for k, v in overrides.states.items()})
    return AppProfile.model_validate(data)


class Tenant:
    """A tenant's configuration plus its capability overlays, from ``tenants/<id>/``."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.config = TenantConfig.model_validate(
            load_yaml((folder / "tenant.yaml").read_text(encoding="utf-8"))
        )
        self.overlays: dict[str, CapabilityOverlay] = {}
        for path in sorted((folder / "overrides").glob("*.yaml")):
            overlay = CapabilityOverlay.model_validate(load_yaml(path.read_text(encoding="utf-8")))
            if overlay.tenant != self.config.tenant:
                raise OverlayError(f"{path}: overlay tenant {overlay.tenant!r} != folder tenant")
            self.overlays[overlay.capability] = overlay

    @classmethod
    def load(cls, root: Path, tenant: str) -> Tenant:
        folder = root / "tenants" / tenant
        if not (folder / "tenant.yaml").exists():
            raise FileNotFoundError(f"no tenant config at {folder / 'tenant.yaml'}")
        return cls(folder)
