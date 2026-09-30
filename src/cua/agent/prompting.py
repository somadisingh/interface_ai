"""The discovery agent's tools, prompt, and how an observation is shown to the model."""

from __future__ import annotations

from typing import Any

from cua.policy.redaction import Redactor
from cua.surface.base import Observation

_REF = {"type": "string", "description": "Element ref from the latest observation, e.g. e12"}
_REASON = {"type": "string", "description": "One short sentence: why this action, now"}


def _tool(
    name: str, description: str, props: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {**props, "reason": _REASON},
            "required": [*required, "reason"],
            "additionalProperties": False,
        },
    }


TOOLS: list[dict[str, Any]] = [
    _tool("click", "Click an element (link, button, clickable text).", {"ref": _REF}, ["ref"]),
    _tool(
        "fill",
        "Replace the text in a text box.",
        {"ref": _REF, "value": {"type": "string"}},
        ["ref", "value"],
    ),
    _tool(
        "select",
        "Choose an option in a dropdown by its visible label or value.",
        {"ref": _REF, "option": {"type": "string"}},
        ["ref", "option"],
    ),
    _tool(
        "check",
        "Tick or untick a checkbox.",
        {"ref": _REF, "checked": {"type": "boolean"}},
        ["ref", "checked"],
    ),
    _tool(
        "press",
        "Press a keyboard key, optionally in an element (e.g. Enter).",
        {"key": {"type": "string"}, "ref": _REF},
        ["key"],
    ),
    _tool(
        "extract",
        "Read the value shown by an element into one of the required outputs.",
        {"ref": _REF, "output": {"type": "string", "description": "Output name"}},
        ["ref", "output"],
    ),
    _tool(
        "wait",
        "Wait for the page to finish loading.",
        {"seconds": {"type": "integer", "minimum": 1, "maximum": 5}},
        ["seconds"],
    ),
    _tool(
        "request_human",
        "Ask a human operator to take over the live session when you are "
        "blocked or unsure. They will act and hand control back.",
        {},
        [],
    ),
    _tool(
        "finish",
        "End the task. success=true only when every required output is extracted "
        "and the goal is achieved. If the application shows a legitimate negative result "
        "(e.g. no such record, access denied, validation error), finish with success=false, "
        "an UPPER_SNAKE outcome_code, and outcome_text copied exactly from the screen.",
        {
            "success": {"type": "boolean"},
            "summary": {"type": "string"},
            "outcome_code": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
            "outcome_text": {"type": "string"},
        },
        ["success", "summary"],
    ),
]


def system_prompt(
    goal: str, params: dict[str, str], outputs: dict[str, tuple[str, str]], app: str
) -> str:
    param_lines = "\n".join(f"- {k} = {v}" for k, v in params.items()) or "- (none)"
    output_lines = "\n".join(f"- {k} ({t}): {d}" for k, (t, d) in outputs.items()) or "- (none)"
    return f"""You operate a back-office business application ({app}) through its user interface,
the way a trained human operator would. You are already signed on.

GOAL: {goal}

PARAMETERS (use these exact values):
{param_lines}

REQUIRED OUTPUTS (use the extract tool once for each):
{output_lines}

How you see the application: each turn you get the page's elements, grouped by frame, each
with a ref like [e12], its role, its name/text, and layout context (the table row it sits in,
the column header for grid cells), plus a screenshot. Many controls have no label of their
own; use the row context (e.g. a textbox in the row "Member ID:"). Clickable text such as
"Search" may be a plain element with role "clickable" rather than a button.

Rules:
- Exactly one tool call per turn, using refs from the LATEST observation only.
- Navigate using the application's own menus and links.
- Maintenance notices and similar interruptions are dismissed for you automatically.
- Some values are deliberately masked (e.g. ***-**-1234, [SECRET]). That is expected.
- Irreversible actions (confirm, submit, transfer...) pause for human approval; that is normal.
- If the application reports a legitimate negative result, do not retry endlessly: finish
  with success=false, an outcome_code and the exact outcome_text shown on screen.
- If you are blocked or unsure, call request_human instead of guessing.
- When every required output has been extracted and the goal is met, call finish."""


def render_observation(obs: Observation, redactor: Redactor) -> str:
    """Compact, model-facing text. Passed through the model-side redactor (the model never
    sees secrets or pattern-matched regulated data)."""
    lines = [f"URL: {obs.url}"]
    frames: dict[str, list[str]] = {}
    for e in obs.elements:
        parts = [f"[{e.ref}] {e.role}"]
        label = e.name or e.text
        if label:
            parts.append(f'"{label}"')
        if e.value is not None:
            parts.append(f'value="{e.value}"')
        if e.options:
            parts.append("options=[" + ", ".join(e.options) + "]")
        if e.column:
            parts.append(f"column: {e.column}")
        if e.row and e.row != label:
            parts.append(f"row: {e.row}")
        if e.disabled:
            parts.append("(disabled)")
        if e.checked is not None:
            parts.append("(checked)" if e.checked else "(unchecked)")
        frames.setdefault(e.frame, []).append("  " + " · ".join(parts))
    for frame in obs.frames:
        items = frames.get(frame.frame)
        if not items:
            continue
        lines.append(f"\nFrame {frame.frame} ({frame.url}):")
        lines.extend(items)
    return redactor.text("\n".join(lines))
