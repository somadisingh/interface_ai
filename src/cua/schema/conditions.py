"""Conditions: checkpoints, outcome detectors and success criteria.

A condition is a predicate over the current UI state. The same vocabulary is used for:

* ``Step.expect`` — the checkpoint that proves a step actually reached the expected state,
* ``Step.outcomes`` — detectors for known business outcomes after a step,
* ``KnownState.detect`` in an app profile — recognising interstitials, expired sessions,
  error pages,
* ``Capability.success`` — the final success condition of the whole capability.

Frame scoping for text/url conditions: ``frame=None`` searches every frame; a frame path
restricts the check to that frame.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import Field, field_validator

from cua.schema.common import Model, valid_regex
from cua.schema.locators import FrameRef


class Visible(Model):
    """The target resolves to exactly one visible element."""

    kind: Literal["visible"] = "visible"
    target: str


class Hidden(Model):
    """The target resolves to no visible element."""

    kind: Literal["hidden"] = "hidden"
    target: str


class TextMatches(Model):
    """Visible text matching the regular expression ``pattern`` is present."""

    kind: Literal["text"] = "text"
    pattern: str
    frame: list[FrameRef] | None = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        return valid_regex(v)


class UrlMatches(Model):
    """The document URL (of the given frame, or the top page) matches ``pattern``."""

    kind: Literal["url"] = "url"
    pattern: str
    frame: list[FrameRef] | None = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        return valid_regex(v)


class OutputPresent(Model):
    """A declared output was extracted and is non-empty."""

    kind: Literal["output"] = "output"
    name: str


class AllOf(Model):
    kind: Literal["all"] = "all"
    conditions: list[Condition] = Field(min_length=1)


class AnyOf(Model):
    kind: Literal["any"] = "any"
    conditions: list[Condition] = Field(min_length=1)


Condition = Annotated[
    Visible | Hidden | TextMatches | UrlMatches | OutputPresent | AllOf | AnyOf,
    Field(discriminator="kind"),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()


def walk(condition: Condition) -> Iterator[Condition]:
    """Yield the condition and all nested conditions."""
    yield condition
    if isinstance(condition, AllOf | AnyOf):
        for child in condition.conditions:
            yield from walk(child)


def target_refs(condition: Condition) -> set[str]:
    return {c.target for c in walk(condition) if isinstance(c, Visible | Hidden)}


def output_refs(condition: Condition) -> set[str]:
    return {c.name for c in walk(condition) if isinstance(c, OutputPresent)}
