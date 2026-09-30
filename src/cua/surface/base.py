"""The surface seam: how the system perceives and acts on an application.

Everything above this interface (discovery agent, recorder, replay engine, handoff) speaks
in terms of *targets* (``TargetSpec``: an ordered bundle of locator strategies), *conditions*
and *actions*. Everything below it is specific to one kind of surface:

* ``WebSurface`` — Playwright, for modern and legacy web apps (framesets, iframes).
* a desktop surface — Windows UI Automation / macOS AX (not built; see ``desktop.py``).

The recorded flow never contains anything surface-specific except, optionally, web-only
fallback locators placed after the semantic ones.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from cua.schema.conditions import Condition
from cua.schema.locators import TargetSpec


@dataclass(frozen=True)
class ElementInfo:
    """One element as presented to the discovery agent (a numbered ref it can act on)."""

    ref: str
    frame: str
    """Frame path, e.g. ``main`` or ``(top)``."""
    tag: str
    role: str
    """ARIA role where one applies; ``clickable`` (element with a click handler but no
    role), ``password``, or ``text`` otherwise."""
    name: str
    text: str
    value: str | None = None
    row: str | None = None
    """Text of the innermost table row containing the element (layout context)."""
    column: str | None = None
    """Header of the column, for table cells."""
    disabled: bool = False
    checked: bool | None = None
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class FrameText:
    frame: str
    url: str
    text: str


@dataclass(frozen=True)
class Observation:
    url: str
    title: str
    frames: tuple[FrameText, ...]
    elements: tuple[ElementInfo, ...]
    screenshot_png: bytes | None = None

    def element(self, ref: str) -> ElementInfo | None:
        return next((e for e in self.elements if e.ref == ref), None)


@dataclass(frozen=True)
class StrategyAttempt:
    strategy: str
    matches: int
    """Visible matches for this strategy (-1 = the strategy could not be evaluated)."""


@dataclass
class Resolved:
    handle: Any
    """Surface-specific element handle (a Playwright ``Locator`` for the web surface)."""
    strategy_index: int
    strategy: str
    attempts: list[StrategyAttempt] = field(default_factory=list)


@dataclass
class Unresolved:
    reason: Literal["not_found", "ambiguous", "frame_not_found"]
    attempts: list[StrategyAttempt] = field(default_factory=list)

    def describe(self) -> str:
        tried = ", ".join(f"{a.strategy}={a.matches}" for a in self.attempts) or "none"
        return f"{self.reason} (visible matches per strategy: {tried})"


Resolution = Resolved | Unresolved


@dataclass(frozen=True)
class ActionResult:
    completed: bool
    kind: Literal["ok", "not_actionable", "detached", "error"] = "ok"
    detail: str | None = None


class Surface(Protocol):
    """Operations every surface implements. Single attempts only: waiting and retrying are
    the caller's responsibility, so timing policy lives in one place (the replay engine)."""

    # perception
    def observe(self, *, screenshot: bool = False) -> Observation: ...
    def resolve(self, target: TargetSpec, inputs: Mapping[str, str]) -> Resolution: ...
    def check(
        self,
        condition: Condition,
        targets: Mapping[str, TargetSpec],
        inputs: Mapping[str, str],
    ) -> bool: ...
    def current_url(self) -> str: ...

    # action
    def navigate(self, path_or_url: str) -> ActionResult: ...
    def click(self, element: Resolved) -> ActionResult: ...
    def fill(self, element: Resolved, value: str) -> ActionResult: ...
    def select(self, element: Resolved, option: str) -> ActionResult: ...
    def set_checked(self, element: Resolved, checked: bool) -> ActionResult: ...
    def press(self, key: str, element: Resolved | None = None) -> ActionResult: ...
    def read_text(self, element: Resolved) -> str: ...

    # recording & evidence
    def describe_ref(self, ref: str, *, purpose: Literal["act", "read"]) -> TargetSpec: ...
    def screenshot(
        self,
        path: Path,
        *,
        mask: Sequence[Resolved] = (),
        mask_text_patterns: Sequence[str] = (),
    ) -> Path: ...
