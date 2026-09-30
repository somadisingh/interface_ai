"""The human-in-the-loop seam used by discovery and replay.

Automation never talks to a person directly; it raises a typed request through a
``HumanChannel`` and blocks until a response arrives:

* ``approve`` — an irreversible step needs a person's go-ahead (policy tier, D9).
* ``assist`` — the run is stuck or hit a state it can't recover from; a person takes over
  the *same live session*, fixes it, and hands control back with a resolution.

How the person is reached and how they operate the session is an implementation of this
protocol (the operator surface). Implementations here: a scripted channel for tests and
unattended runs, and a terminal channel for local use.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

AssistReason = Literal["stuck", "unknown_state", "failure", "agent_requested"]


def new_request_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(2)}"


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    run_id: str
    subject: str
    """Capability ref (replay) or goal (discovery)."""
    step: str
    action: str
    """Human-readable action, e.g. ``click button "Confirm & Open Account"``."""
    risk: str
    reason: str
    url: str
    screenshot: str | None = None


@dataclass(frozen=True)
class ApprovalResponse:
    approved: bool
    by: str
    note: str | None = None


@dataclass(frozen=True)
class AssistRequest:
    id: str
    run_id: str
    subject: str
    step: str
    reason: AssistReason
    message: str
    url: str
    screenshot: str | None = None
    recent_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class AssistResponse:
    resolution: Literal["resume", "skip_step", "abort"]
    by: str
    note: str | None = None
    human_actions: list[dict[str, Any]] = field(default_factory=list)


class HumanChannel(Protocol):
    def approve(self, request: ApprovalRequest) -> ApprovalResponse: ...
    def assist(self, request: AssistRequest) -> AssistResponse: ...


class ScriptedChannel:
    """Answers from callables (tests) or fixed policy (unattended: deny and abort)."""

    def __init__(
        self,
        approve: Callable[[ApprovalRequest], ApprovalResponse] | None = None,
        assist: Callable[[AssistRequest], AssistResponse] | None = None,
    ) -> None:
        self._approve = approve
        self._assist = assist
        self.approvals: list[ApprovalRequest] = []
        self.assists: list[AssistRequest] = []

    def approve(self, request: ApprovalRequest) -> ApprovalResponse:
        self.approvals.append(request)
        if self._approve is None:
            return ApprovalResponse(False, "system", "no human available (unattended run)")
        return self._approve(request)

    def assist(self, request: AssistRequest) -> AssistResponse:
        self.assists.append(request)
        if self._assist is None:
            return AssistResponse("abort", "system", "no human available (unattended run)")
        return self._assist(request)


class TerminalChannel:
    """Asks in the terminal. The browser window (headed) is the live session."""

    def __init__(self, operator: str = "operator") -> None:
        self.operator = operator

    def approve(self, request: ApprovalRequest) -> ApprovalResponse:
        print(f"\n[APPROVAL NEEDED] {request.subject} / step {request.step}")
        print(f"  action: {request.action}  (risk: {request.risk})")
        print(f"  why:    {request.reason}")
        answer = input("  approve? [y/N] ").strip().lower()
        return ApprovalResponse(answer == "y", self.operator)

    def assist(self, request: AssistRequest) -> AssistResponse:
        print(f"\n[HELP NEEDED] {request.subject} / step {request.step} ({request.reason})")
        print(f"  {request.message}")
        print("  Operate the browser window if needed, then choose:")
        answer = input("  [r]esume / [s]kip step / [a]bort? ").strip().lower()
        resolution: Literal["resume", "skip_step", "abort"] = (
            "resume" if answer == "r" else "skip_step" if answer == "s" else "abort"
        )
        return AssistResponse(resolution, self.operator)
