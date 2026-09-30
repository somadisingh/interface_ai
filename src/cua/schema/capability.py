"""The capability artifact: a typed, versioned, reviewable, replayable description of a flow.

A capability is what a calling AI agent invokes: it has a contract (typed inputs, typed
outputs, the business outcomes it can return) and an implementation (targets + steps +
checkpoints) that the replay engine executes without any model in the loop.

Serialized as YAML under ``capabilities/<product>/<id>/<version>.yaml``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from cua.schema import templates
from cua.schema.actions import Action, Extract, Fill, action_strings, action_target
from cua.schema.common import (
    SCHEMA_VERSION,
    DottedId,
    Identifier,
    Model,
    OutcomeCode,
    Risk,
    SemVer,
    valid_regex,
)
from cua.schema.conditions import Condition, output_refs, target_refs
from cua.schema.locators import TargetSpec

ValueType = Literal["string", "integer", "decimal", "money", "boolean", "date", "enum"]


class InputSpec(Model):
    """A typed parameter the caller supplies per invocation."""

    type: ValueType
    description: str
    pattern: str | None = None
    """Regex the value must fully match (validated before touching the UI)."""
    enum: list[str] | None = None
    required: bool = True
    default: str | None = None
    sensitivity: Literal["none", "pii"] = "none"
    """Inputs may be PII (masked in logs). Credentials are never inputs: they are
    ``{{secrets.*}}`` references resolved from the runtime environment."""

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        return None if v is None else valid_regex(v)

    @model_validator(mode="after")
    def _enum_consistency(self) -> InputSpec:
        if (self.type == "enum") != (self.enum is not None):
            raise ValueError("'enum' values are required for, and only allowed on, type enum")
        return self


class OutputSpec(Model):
    """A typed value the capability returns to the caller on success."""

    type: ValueType
    description: str
    sensitivity: Literal["none", "pii"] = "none"


class BusinessOutcome(Model):
    """A legitimate, expected result that is not success, e.g. ``MEMBER_NOT_FOUND``.

    Returned as ``status: business_outcome`` — never as a failure."""

    description: str
    retryable: bool = False


class OutcomeRule(Model):
    """If ``when`` holds after a step, the run ends with business outcome ``outcome``."""

    when: Condition
    outcome: OutcomeCode


class Step(Model):
    id: Identifier
    intent: str
    """What this step is for, in operator language (shown in logs and escalations)."""
    action: Action
    risk: Risk = "safe"
    auto_approve_on_replay: bool = False
    """Only for ``irreversible`` steps: a reviewer allows unattended replay of this step once
    the capability is approved. Otherwise irreversible steps wait for human approval."""
    expect: list[Condition] = Field(default_factory=list)
    """Checkpoint: all must hold after the action, or the run is not where it thinks it is."""
    outcomes: list[OutcomeRule] = Field(default_factory=list)
    """Known business outcomes that can appear instead of the expected state."""
    timeout_ms: int = Field(default=10_000, gt=0, le=120_000)

    @model_validator(mode="after")
    def _auto_approve_only_irreversible(self) -> Step:
        if self.auto_approve_on_replay and self.risk != "irreversible":
            raise ValueError(
                f"step {self.id!r}: auto_approve_on_replay only applies to irreversible steps"
            )
        return self


class AppRef(Model):
    product: Identifier
    """The vendor product this flow targets (shared across tenants running it)."""
    product_versions: str
    """Version range the flow is known to work on, e.g. ``>=4.0,<5.0``."""
    surface: Literal["web", "desktop"] = "web"


class RecoveryPolicy(Model):
    max_recoveries_per_step: int = Field(default=2, ge=0, le=5)
    max_recoveries_total: int = Field(default=5, ge=0, le=20)


class Review(Model):
    status: Literal["draft", "approved", "deprecated"] = "draft"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _approval_needs_reviewer(self) -> Review:
        if self.status == "approved" and not self.reviewed_by:
            raise ValueError("an approved capability must name its reviewer")
        return self


class Provenance(Model):
    source: Literal["discovery", "hand_authored"]
    recorded_from_run: str | None = None
    recorded_at: datetime | None = None
    model: str | None = None
    content_hash: str | None = None
    """sha256 over the contract and implementation (excludes review and provenance)."""


class Capability(Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    id: DottedId
    version: SemVer
    summary: str
    description: str | None = None
    app: AppRef
    requires: list[DottedId] = Field(default_factory=list)
    """Capabilities that must have run first in the session, e.g. ``session.sign_on``."""

    inputs: dict[Identifier, InputSpec] = Field(default_factory=dict)
    outputs: dict[Identifier, OutputSpec] = Field(default_factory=dict)
    outcomes: dict[OutcomeCode, BusinessOutcome] = Field(default_factory=dict)

    targets: dict[Identifier, TargetSpec]
    steps: list[Step] = Field(min_length=1)
    success: list[Condition] = Field(min_length=1)
    recovery: RecoveryPolicy = Field(default_factory=RecoveryPolicy)

    review: Review = Field(default_factory=Review)
    provenance: Provenance

    @property
    def ref(self) -> str:
        """Stable reference used by callers and in results: ``product/id@version``."""
        return f"{self.app.product}/{self.id}@{self.version}"

    @field_validator("steps")
    @classmethod
    def _unique_step_ids(cls, steps: list[Step]) -> list[Step]:
        seen: set[str] = set()
        for s in steps:
            if s.id in seen:
                raise ValueError(f"duplicate step id {s.id!r}")
            seen.add(s.id)
        return steps

    @model_validator(mode="after")
    def _cross_references(self) -> Capability:
        errors: list[str] = []

        def check_target(ref: str | None, where: str) -> None:
            if ref is not None and ref not in self.targets:
                errors.append(f"{where}: unknown target {ref!r}")

        def check_templates(text: str, where: str, *, secrets_ok: bool) -> None:
            try:
                refs = templates.references(text)
            except templates.TemplateError as exc:
                errors.append(f"{where}: {exc}")
                return
            for ns, name in refs:
                if ns == "inputs" and name not in self.inputs:
                    errors.append(f"{where}: references undeclared input {name!r}")
                if ns == "secrets" and not secrets_ok:
                    errors.append(f"{where}: secrets may only be used as a fill value")

        def check_condition(cond: Condition, where: str) -> None:
            for t in target_refs(cond):
                check_target(t, where)
            for o in output_refs(cond):
                if o not in self.outputs:
                    errors.append(f"{where}: references undeclared output {o!r}")

        used_inputs: set[str] = set()
        for tname, target in self.targets.items():
            for text in target.strings():
                check_templates(text, f"target {tname}", secrets_ok=False)
                used_inputs.update(n for ns, n in _safe_refs(text) if ns == "inputs")

        extracted: dict[str, str] = {}
        for step in self.steps:
            where = f"step {step.id}"
            check_target(action_target(step.action), where)
            for text in action_strings(step.action):
                check_templates(text, where, secrets_ok=isinstance(step.action, Fill))
                used_inputs.update(n for ns, n in _safe_refs(text) if ns == "inputs")
            if isinstance(step.action, Extract):
                if step.action.into not in self.outputs:
                    errors.append(f"{where}: extracts into undeclared output {step.action.into!r}")
                elif step.action.into in extracted:
                    errors.append(
                        f"{where}: output {step.action.into!r} already extracted "
                        f"by step {extracted[step.action.into]!r}"
                    )
                else:
                    extracted[step.action.into] = step.id
            for cond in step.expect:
                check_condition(cond, f"{where} expect")
            for rule in step.outcomes:
                check_condition(rule.when, f"{where} outcomes")
                if rule.outcome not in self.outcomes:
                    errors.append(
                        f"{where}: outcome {rule.outcome!r} is not declared in 'outcomes'"
                    )

        for cond in self.success:
            check_condition(cond, "success")
        for name in self.outputs:
            if name not in extracted:
                errors.append(f"output {name!r} is declared but never extracted")
        for name in self.inputs:
            if name not in used_inputs:
                errors.append(f"input {name!r} is declared but never used")

        if errors:
            raise ValueError("; ".join(errors))
        return self

    # ------------------------------------------------------------------ identity

    def content_hash(self) -> str:
        """Deterministic hash of what the capability *does* (contract + implementation).

        Review status and provenance are excluded: approving a capability does not change
        its behaviour, so it must not change its identity."""
        body = self.model_dump(mode="json", exclude={"review", "provenance"})
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    def has_irreversible_steps(self) -> bool:
        return any(s.risk == "irreversible" for s in self.steps)


def _safe_refs(text: str) -> list[tuple[templates.Namespace, str]]:
    try:
        return templates.references(text)
    except templates.TemplateError:
        return []
