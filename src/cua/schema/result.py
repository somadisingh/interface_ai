"""The run result: what a caller (an AI agent) gets back from invoking a capability.

Four mutually exclusive statuses, so callers never have to parse error strings:

* ``success`` — goal reached, checkpoint verified, declared outputs returned.
* ``business_outcome`` — a legitimate non-success answer (e.g. ``MEMBER_NOT_FOUND``). Not an
  error: the caller should act on ``outcome.code``.
* ``failed`` — a hard failure. ``error`` says which step, what was expected, what was
  observed, and where the evidence is.
* ``escalated`` — the run is waiting for, or was ended by, a human intervention.

Recoverable conditions (dismissed interstitials, re-established sessions, retried slow
loads) do not change the status; they are listed in ``recoveries`` for transparency.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from cua.schema.app_profile import FailureCategory
from cua.schema.common import Model, OutcomeCode

RunStatus = Literal["success", "business_outcome", "failed", "escalated"]


class Money(Model):
    amount: str = Field(pattern=r"^-?\d+\.\d{2}$")
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class OutcomeInfo(Model):
    code: OutcomeCode
    description: str
    retryable: bool
    step_id: str | None = None
    source: Literal["capability", "app_profile"]


class RunError(Model):
    category: FailureCategory
    message: str
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    evidence: list[str] = Field(default_factory=list)
    """Paths (relative to the run's evidence directory) of screenshots, DOM snapshots,
    traces."""


class RecoveryRecord(Model):
    step_id: str
    state: str
    """The app-profile state that was recognised, e.g. ``maintenance_notice``."""
    action: str
    """What was done, e.g. ``handler: click maintenance_ack, then reevaluate``."""
    attempt: int = Field(ge=1)


class DriftWarning(Model):
    step_id: str
    target: str
    used_strategy: str
    used_index: int = Field(ge=1)
    """Position of the strategy that resolved the target (0 would be the preferred one)."""


class RunResult(Model):
    run_id: str
    capability: str
    """``product/id@version`` of the capability that ran."""
    content_hash: str
    status: RunStatus
    outputs: dict[str, JsonValue] = Field(default_factory=dict)
    outcome: OutcomeInfo | None = None
    error: RunError | None = None
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    drift_warnings: list[DriftWarning] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list)
    """Ids of human intervention requests raised during the run."""
    started_at: datetime
    finished_at: datetime
    evidence_dir: str | None = None

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @model_validator(mode="after")
    def _status_consistency(self) -> RunResult:
        s = self.status
        if s == "success" and (self.error or self.outcome):
            raise ValueError("success carries neither an error nor a business outcome")
        if s == "business_outcome" and (self.outcome is None or self.error is not None):
            raise ValueError("business_outcome requires 'outcome' and no 'error'")
        if s == "failed" and (self.error is None or self.outcome is not None):
            raise ValueError("failed requires 'error' and no 'outcome'")
        if s == "escalated" and not self.interventions:
            raise ValueError("escalated requires at least one intervention id")
        if s != "success" and self.outputs:
            raise ValueError("outputs are only returned on success")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        return self
