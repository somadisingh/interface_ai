"""Who is in control of the live session: the control lease.

Exactly one party may act on the session at a time:

* ``automation`` — discovery or replay holds the lease and may act.
* ``paused`` — automation raised an intervention request and is waiting; nobody acts.
* ``human`` — an operator took control of the same live session; automation must not act.

Every browser action goes through ``assert_automation()`` (enforced in the web surface), so
automation can never act while a person is in control. Every transition is recorded with who
made it and why, which is how the evidence shows the handoff.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

Controller = Literal["automation", "paused", "human"]

_ALLOWED: dict[Controller, set[Controller]] = {
    "automation": {"paused"},
    "paused": {"human", "automation"},
    "human": {"automation"},
}


class LeaseViolation(RuntimeError):
    """Automation tried to act while it did not hold the control lease."""


class InvalidTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class Transition:
    at: datetime
    frm: Controller
    to: Controller
    by: str
    reason: str
    request_id: str | None = None


class SessionControl:
    def __init__(self, on_change: Callable[[Transition], None] | None = None) -> None:
        self._state: Controller = "automation"
        self._holder = "automation"
        self._lock = threading.Lock()
        self._on_change = on_change
        self.history: list[Transition] = []

    @property
    def state(self) -> Controller:
        return self._state

    @property
    def holder(self) -> str:
        return self._holder

    def assert_automation(self) -> None:
        if self._state != "automation":
            raise LeaseViolation(
                f"automation may not act: session is {self._state} (held by {self._holder})"
            )

    def transfer(
        self, to: Controller, *, by: str, reason: str, request_id: str | None = None
    ) -> Transition:
        with self._lock:
            if to not in _ALLOWED[self._state]:
                raise InvalidTransition(f"cannot go from {self._state} to {to}")
            t = Transition(datetime.now(UTC), self._state, to, by, reason, request_id)
            self._state = to
            self._holder = by if to == "human" else to
            self.history.append(t)
        if self._on_change is not None:
            self._on_change(t)
        return t
