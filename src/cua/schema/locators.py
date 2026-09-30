"""How a target element/control is identified: an ordered bundle of locator strategies.

Resolution contract (implemented by the replay engine):

1. Strategies are tried **in the order listed**, most semantic first.
2. A strategy only counts if it matches **exactly one** visible element. Zero matches or
   more than one match means "try the next strategy"; a strategy never guesses between
   candidates.
3. If a strategy other than the first one resolves the target, replay continues but records
   a **drift warning** (which target, which strategy was used, which was preferred). Drift
   warnings are how UI changes get noticed before they turn into failures.
4. If no strategy resolves to exactly one element, the step fails with
   ``LOCATOR_NOT_FOUND`` (nothing matched) or ``LOCATOR_AMBIGUOUS`` (only multi-matches).

Surface independence: ``role``, ``label``, ``anchor``, ``table_cell`` and ``text`` describe
what a human operator sees, and have direct equivalents in desktop accessibility APIs
(Windows UI Automation, macOS AX). ``attribute`` and ``css`` are web-only fallbacks and must
come after all semantic strategies.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from cua.schema.common import Model


class RoleLocator(Model):
    """Accessible role + accessible name, e.g. button "Sign On", link "Member Search"."""

    strategy: Literal["role"] = "role"
    role: str
    name: str | None = None
    exact: bool = True


class LabelLocator(Model):
    """The control whose accessible label is ``text`` (``<label>``, aria-label, title)."""

    strategy: Literal["label"] = "label"
    text: str
    exact: bool = True


class AnchorLocator(Model):
    """A control positioned relative to visible anchor text, for when the label is only
    visually adjacent and not programmatically associated (common in legacy UIs).

    ``same_row`` means the innermost table row (or row-like container) that contains the
    anchor text. ``role`` narrows the control type inside that row, e.g. ``textbox``.
    """

    strategy: Literal["anchor"] = "anchor"
    anchor_text: str
    relation: Literal["same_row", "right_of", "below"] = "same_row"
    role: str | None = None


class TableCellLocator(Model):
    """A cell addressed like a human reads a grid: the row containing ``row_text``, in the
    column headed ``column_header``."""

    strategy: Literal["table_cell"] = "table_cell"
    row_text: str
    column_header: str


class TextLocator(Model):
    """An element by its visible text (e.g. a ``<span onclick>`` "button")."""

    strategy: Literal["text"] = "text"
    text: str
    exact: bool = True


class AttributeLocator(Model):
    """Web-only: an element by tag and a stable attribute, e.g. ``input[name=mid]``."""

    strategy: Literal["attribute"] = "attribute"
    tag: str
    attribute: str
    value: str


class CssLocator(Model):
    """Web-only last resort: a structural CSS selector. Recorded, but inherently fragile."""

    strategy: Literal["css"] = "css"
    selector: str


Locator = Annotated[
    RoleLocator
    | LabelLocator
    | AnchorLocator
    | TableCellLocator
    | TextLocator
    | AttributeLocator
    | CssLocator,
    Field(discriminator="strategy"),
]

SEMANTIC_STRATEGIES = frozenset({"role", "label", "anchor", "table_cell", "text"})
WEB_ONLY_STRATEGIES = frozenset({"attribute", "css"})


class FrameRef(Model):
    """One hop in a frame path (frameset ``<frame>`` or ``<iframe>``), by name or URL."""

    name: str | None = None
    url_pattern: str | None = None

    @model_validator(mode="after")
    def _one_of(self) -> FrameRef:
        if not self.name and not self.url_pattern:
            raise ValueError("frame reference needs a name or a url_pattern")
        return self


class TargetSpec(Model):
    """A named element/control the flow interacts with or reads from."""

    description: str
    """Human-readable description, also shown to operators during escalation."""
    frame: list[FrameRef] = Field(default_factory=list)
    """Frame path from the top-level page; empty means the top-level document."""
    locators: list[Locator] = Field(min_length=1)
    robustness_note: str | None = None
    """Why these strategies, in this order (the reviewer-facing reasoning)."""
    sensitive: bool = False
    """The element displays sensitive data: masked in screenshots and evidence."""

    @model_validator(mode="after")
    def _semantic_first(self) -> TargetSpec:
        seen_web_only = False
        for loc in self.locators:
            if loc.strategy in WEB_ONLY_STRATEGIES:
                seen_web_only = True
            elif seen_web_only:
                raise ValueError(
                    f"locator strategy {loc.strategy!r} is listed after a web-only fallback; "
                    "semantic strategies must come first"
                )
        return self

    def strings(self) -> list[str]:
        """All free-text fields (used to validate template references)."""
        out: list[str] = []
        for loc in self.locators:
            for value in loc.model_dump().values():
                if isinstance(value, str):
                    out.append(value)
        return out
