"""The operator channel: a real handoff of the *same live session* to a person.

Flow for a help request (assist):

    automation --(raise request)--> paused --(operator: Take control)--> human
         ^                                                                  |
         +----------------(operator: Hand back: resume / skip / abort)------+

* While paused or human, the automation thread only waits. It keeps pumping the browser's
  event loop, so the recorder's reports of the person's clicks and changes are delivered.
* The web surface enforces the lease: an automated action while a human holds the session
  raises ``LeaseViolation``.
* What the person does is captured (redacted) and returned with the hand-back, and every
  control transfer is logged with who made it and why.

Approvals use the same pause, without a transfer to the human: the operator approves or
rejects, and control returns to automation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cua.evidence.log import EventLog
from cua.handoff.channel import (
    ApprovalRequest,
    ApprovalResponse,
    AssistRequest,
    AssistResponse,
)
from cua.handoff.control import SessionControl
from cua.handoff.queue import InterventionQueue, Ticket
from cua.surface.web import WebSurface


class OperatorChannel:
    def __init__(
        self,
        surface: WebSurface,
        control: SessionControl,
        queue: InterventionQueue,
        log: EventLog,
        *,
        poll_ms: int = 200,
        simulate_operator: Callable[[WebSurface], None] | None = None,
    ) -> None:
        self.surface = surface
        self.control = control
        self.queue = queue
        self.log = log
        self.poll_ms = poll_ms
        self._simulate = simulate_operator
        """Test/demo hook: runs in the automation thread once a person has taken control,
        standing in for them with raw mouse/keyboard input on the same page."""
        self.human_actions: list[dict[str, Any]] = []
        surface.install_human_recorder(self._on_human_action)

    # ------------------------------------------------------------------ recorder

    def _on_human_action(self, payload: dict[str, Any]) -> None:
        state = self.control.state
        if state == "automation":
            return  # automation's own clicks fire DOM events too; only record people
        # While paused, automation only waits, so any input comes from a person who has not
        # taken the lease (e.g. clicked in the browser before pressing "Take control"). It is
        # still recorded, flagged, so nothing done to the session goes unaudited.
        without_lease = state != "human"
        record = self.log.redactor.value(
            {
                "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "by": "unknown" if without_lease else self.control.holder,
                **({"without_lease": True} if without_lease else {}),
                **payload,
            }
        )
        self.human_actions.append(record)
        self.log.emit("human_action", **record)

    # ------------------------------------------------------------------ HumanChannel

    def approve(self, request: ApprovalRequest) -> ApprovalResponse:
        self.control.transfer(
            "paused",
            by="automation",
            request_id=request.id,
            reason=f"approval needed: {request.action}",
        )
        ticket = self.queue.submit("approval", request)
        self._wait(ticket)
        res = ticket.resolution or {}
        approved = bool(res.get("approved"))
        by = str(res.get("by", "operator"))
        self.control.transfer(
            "automation",
            by=by,
            request_id=request.id,
            reason="approved" if approved else "rejected",
        )
        return ApprovalResponse(approved, by, res.get("note"))

    def assist(self, request: AssistRequest) -> AssistResponse:
        self.control.transfer(
            "paused", by="automation", request_id=request.id, reason=request.message
        )
        ticket = self.queue.submit("assist", request)
        first = len(self.human_actions)
        self._wait(ticket)
        res = ticket.resolution or {}
        resolution = res.get("resolution", "abort")
        by = str(res.get("by", "operator"))
        self.control.transfer(
            "automation", by=by, request_id=request.id, reason=f"handed back: {resolution}"
        )
        return AssistResponse(resolution, by, res.get("note"), self.human_actions[first:])

    def _wait(self, ticket: Ticket) -> None:
        simulated = False
        while ticket.open:
            if self._simulate and not simulated and self.control.state == "human":
                simulated = True
                self._simulate(self.surface)
            self.surface.wait(self.poll_ms)  # pumps browser events (recorder bindings)
