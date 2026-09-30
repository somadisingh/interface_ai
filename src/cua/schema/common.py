"""Shared building blocks for every schema model."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

SCHEMA_VERSION: Literal["1.0"] = "1.0"


class Model(BaseModel):
    """Base for all artifact models: unknown keys are rejected (typos fail loudly) and
    instances are immutable once validated."""

    model_config = ConfigDict(extra="forbid", frozen=True)


Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
"""snake_case name for steps, targets, inputs, outputs and states."""

DottedId = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=128)
]
"""Capability id, namespaced by domain: e.g. ``member.read_savings_balance``."""

OutcomeCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)]
"""Business outcome code returned to callers, e.g. ``MEMBER_NOT_FOUND``."""

SemVer = Annotated[str, StringConstraints(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")]
"""Capability version. Major = input/output contract change; minor = new recovery or
fallback; patch = locator tweak."""

Risk = Literal["safe", "reversible_write", "irreversible"]
"""Risk class of a step's action. ``irreversible`` steps are gated (see policy)."""

Sensitivity = Literal["none", "pii", "secret"]
"""How a value must be treated in logs and evidence: ``pii`` is masked, ``secret`` is never
written anywhere."""


def valid_regex(pattern: str) -> str:
    """Validator helper: reject patterns that don't compile, as a normal validation error."""
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
    return pattern
