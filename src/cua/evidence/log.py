"""Structured, redacted run evidence: an append-only JSONL event log plus masked screenshots.

Every event passes through the run's redactor before it touches disk. The log is the single
source of truth for a run; the human-readable report is generated from it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cua.policy.redaction import Redactor
from cua.surface.base import Resolved
from cua.surface.web import WebSurface


def utcnow() -> datetime:
    return datetime.now(UTC)


class EventLog:
    def __init__(self, run_dir: Path, run_id: str, redactor: Redactor) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.redactor = redactor
        self._seq = 0
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "screenshots").mkdir(exist_ok=True)
        self._path = run_dir / "events.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, type: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        event = {
            "ts": utcnow().isoformat(timespec="milliseconds"),
            "seq": self._seq,
            "run_id": self.run_id,
            "type": type,
            **fields,
        }
        safe = self.redactor.value(event)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe, default=str, ensure_ascii=False) + "\n")
        return dict(safe)

    def screenshot(
        self, surface: WebSurface, name: str, *, mask: Sequence[Resolved] = ()
    ) -> str | None:
        """Masked screenshot; returns its path relative to the run directory."""
        rel = f"screenshots/{self._seq:03d}-{name}.png"
        try:
            surface.screenshot(
                self.run_dir / rel, mask=mask, mask_text_patterns=self.redactor.dom_patterns()
            )
        except Exception:  # evidence must never break a run
            return None
        return rel

    def write_json(self, name: str, data: Any) -> Path:
        path = self.run_dir / name
        path.write_text(
            json.dumps(self.redactor.value(data), indent=2, default=str, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path
