"""The discovery trace: what the agent did, recorded in a form the compiler can turn into a
capability — and safe to keep as evidence.

Parameter values are replaced by ``{{inputs.name}}`` placeholders *at record time* (in typed
values, chosen options and locator text), so the trace never stores the concrete values the
discovery run happened to use.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from cua.schema.capability import OutputSpec, ValueType
from cua.schema.common import Model, Risk
from cua.schema.locators import TargetSpec


class ParamSpec(Model):
    type: ValueType = "string"
    sensitivity: Literal["none", "pii"] = "pii"


class TraceStep(Model):
    index: int
    actor: Literal["agent", "human"] = "agent"
    tool: str
    reason: str = ""
    target: TargetSpec | None = None
    target_label: str | None = None
    value: str | None = None
    option: str | None = None
    key: str | None = None
    checked: bool | None = None
    output: str | None = None
    risk: Risk = "safe"
    verdict: Literal["allow", "deny", "require_approval"] = "allow"
    approved_by: str | None = None
    result: Literal["ok", "error", "denied", "rejected"] = "ok"
    detail: str | None = None
    url_after: str | None = None


class DiscoveryTrace(Model):
    run_id: str
    capability_id: str
    product: str
    goal: str
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    requires: list[str] = Field(default_factory=list)
    model: str
    started_at: datetime
    finished_at: datetime | None = None
    status: Literal["succeeded", "outcome", "failed", "aborted"] = "failed"
    summary: str = ""
    outcome_code: str | None = None
    outcome_text: str | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    recoveries: list[str] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)


class Templater:
    """Replaces parameter values with placeholders, whole tokens only (``12345`` inside
    ``1234567`` is left alone)."""

    def __init__(self, params: dict[str, str]) -> None:
        self._rules = [
            (re.compile(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])"), f"{{{{inputs.{k}}}}}")
            for k, v in sorted(params.items(), key=lambda kv: len(kv[1]), reverse=True)
            if v
        ]

    def text(self, value: str | None) -> str | None:
        if value is None:
            return None
        for pattern, placeholder in self._rules:
            value = pattern.sub(placeholder.replace("\\", "\\\\"), value)
        return value

    def target(self, spec: TargetSpec) -> TargetSpec:
        data = spec.model_dump(mode="json")
        data["description"] = self.text(data["description"])
        data["locators"] = [self._walk(loc) for loc in data["locators"]]
        return TargetSpec.model_validate(data)

    def _walk(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: (v if k == "strategy" else self._walk(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._walk(v) for v in obj]
        return obj
