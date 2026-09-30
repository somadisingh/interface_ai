"""Intervention requests awaiting a human, shared between the automation thread (which
submits and waits) and the operator surface (which lists and resolves)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from cua.handoff.channel import ApprovalRequest, AssistRequest

Kind = Literal["approval", "assist"]


@dataclass
class Ticket:
    kind: Kind
    request: ApprovalRequest | AssistRequest
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    taken_by: str | None = None
    resolution: dict[str, Any] | None = None
    resolved_at: datetime | None = None

    @property
    def id(self) -> str:
        return self.request.id

    @property
    def open(self) -> bool:
        return self.resolution is None


class InterventionQueue:
    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.Lock()

    def submit(self, kind: Kind, request: ApprovalRequest | AssistRequest) -> Ticket:
        with self._lock:
            ticket = Ticket(kind, request)
            self._tickets[ticket.id] = ticket
            return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        return self._tickets.get(ticket_id)

    def all(self) -> list[Ticket]:
        with self._lock:
            return sorted(self._tickets.values(), key=lambda t: t.created_at, reverse=True)

    def mark_taken(self, ticket_id: str, by: str) -> Ticket:
        with self._lock:
            ticket = self._require_open(ticket_id)
            ticket.taken_by = by
            return ticket

    def resolve(self, ticket_id: str, resolution: dict[str, Any]) -> Ticket:
        with self._lock:
            ticket = self._require_open(ticket_id)
            ticket.resolution = resolution
            ticket.resolved_at = datetime.now(UTC)
            return ticket

    def _require_open(self, ticket_id: str) -> Ticket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise KeyError(f"no such request {ticket_id!r}")
        if not ticket.open:
            raise ValueError(f"request {ticket_id!r} is already resolved")
        return ticket
