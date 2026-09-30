"""Actions: the operations a step performs on the surface.

Actions reference targets by name (see ``TargetSpec``), never by raw selector, so the
recorded flow is decoupled from how a particular surface finds elements. Values may contain
templates (``{{inputs.x}}``, ``{{secrets.X}}``); secrets are only allowed where a value is
typed into a control (``fill``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator

from cua.schema.common import Model, valid_regex


class Click(Model):
    type: Literal["click"] = "click"
    target: str


class Fill(Model):
    """Replace the content of a text control with ``value`` (template allowed)."""

    type: Literal["fill"] = "fill"
    target: str
    value: str


class Select(Model):
    """Choose an option in a dropdown by its visible label or value (template allowed)."""

    type: Literal["select"] = "select"
    target: str
    option: str


class Check(Model):
    type: Literal["check"] = "check"
    target: str
    checked: bool = True


class Press(Model):
    """Press a key, optionally with focus on a target (e.g. ``Enter`` in a field)."""

    type: Literal["press"] = "press"
    key: str
    target: str | None = None


class Navigate(Model):
    """Go to a path relative to the app instance's base URL (template allowed).

    Paths are relative on purpose: the same artifact runs against every tenant's instance of
    the app; the base URL is runtime configuration, not part of the flow."""

    type: Literal["navigate"] = "navigate"
    path: str


ParseAs = Literal["text", "money", "integer", "decimal", "date"]


class Extract(Model):
    """Read the text of a target into a declared output, parsed as ``parse``.

    ``pattern``, if given, is a regex whose first group selects the part of the text to keep.
    """

    type: Literal["extract"] = "extract"
    target: str
    into: str
    parse: ParseAs = "text"
    pattern: str | None = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        return None if v is None else valid_regex(v)


Action = Annotated[
    Click | Fill | Select | Check | Press | Navigate | Extract,
    Field(discriminator="type"),
]


def action_target(action: Action) -> str | None:
    return getattr(action, "target", None)


def action_strings(action: Action) -> list[str]:
    """Template-capable string fields of an action."""
    if isinstance(action, Fill):
        return [action.value]
    if isinstance(action, Select):
        return [action.option]
    if isinstance(action, Navigate):
        return [action.path]
    return []
