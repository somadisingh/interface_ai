"""Compiler: discovery trace -> capability artifact.

What it decides, and why:

* **Only successful agent actions become steps.** Errors, denied and rejected attempts are
  evidence, not part of the flow. Waits are dropped (replay waits on conditions instead).
* **Targets are deduplicated and named** from what an operator would call them
  (``member_search_link``, ``member_id_field``), so the artifact reads like a procedure.
* **Checkpoints come from the next target.** After a click/press/select, the step expects
  the element the *next* step needs to be visible. That is exactly the evidence that the
  action landed where the flow continues — no guessing from page titles or timings.
* **Success = every declared output was extracted.**
* **Business outcomes** are added by merging a "negative" discovery run (bad input) into an
  existing capability: the outcome text the agent saw becomes an outcome rule on the step
  after which it appeared.

The result is always a ``draft``; a human reviews and approves it.
"""

from __future__ import annotations

import re
from pathlib import Path

from cua.recorder.trace import DiscoveryTrace, TraceStep
from cua.replay.support import CapabilityRegistry
from cua.schema import Capability
from cua.schema.capability import InputSpec
from cua.schema.locators import TargetSpec

_PARSE = {
    "string": "text",
    "enum": "text",
    "boolean": "text",
    "money": "money",
    "integer": "integer",
    "decimal": "decimal",
    "date": "date",
}
_ROLE_SUFFIX = {
    "link": "link",
    "button": "button",
    "clickable": "button",
    "textbox": "field",
    "password": "field",
    "combobox": "select",
    "checkbox": "checkbox",
    "radio": "option",
    "cell": "cell",
    "columnheader": "header",
}
_VERB = {
    "click": "open",
    "fill": "enter",
    "select": "choose",
    "check": "set",
    "press": "press",
    "extract": "read",
}
_PAGE_CHANGING = {"click", "press", "select"}


class CompileError(ValueError):
    pass


def _slug(text: str) -> str:
    text = re.sub(r"\{\{\s*inputs\.(\w+)\s*\}\}", r"\1", text)
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "_".join(words[:4]) or "element"


def _target_name(spec: TargetSpec, tool: str) -> str:
    """``link "Member Search"`` -> ``member_search_link``;
    ``"Current Balance" value in the row for "S00"`` -> ``current_balance_value``."""
    desc = spec.description
    quoted = re.findall(r'"([^"]+)"', desc)
    role = desc.split(" ", 1)[0].strip('"')
    if tool == "extract":
        return f"{_slug(quoted[0]) if quoted else 'value'}_value"
    base = _slug(quoted[0]) if quoted else _slug(desc)
    suffix = _ROLE_SUFFIX.get(role, "element")
    return base if base.endswith(suffix) else f"{base}_{suffix}"


def compile_trace(
    trace: DiscoveryTrace,
    *,
    version: str = "1.0.0",
    summary: str | None = None,
    product_versions: str = "*",
) -> Capability:
    if trace.status != "succeeded":
        raise CompileError(f"only successful discovery runs compile (status: {trace.status})")
    steps = [s for s in trace.steps if s.actor == "agent" and s.result == "ok" and s.tool != "wait"]
    if not steps:
        raise CompileError("the trace has no successful actions")

    # --- targets (dedupe identical specs, stable readable names)
    targets: dict[str, TargetSpec] = {}
    target_of: dict[int, str] = {}
    for s in steps:
        if s.target is None:
            continue
        existing = next((n for n, t in targets.items() if t == s.target), None)
        if existing is None:
            name = _target_name(s.target, s.tool)
            n, unique = 2, name
            while unique in targets:
                unique, n = f"{name}_{n}", n + 1
            targets[unique] = s.target
            existing = unique
        target_of[s.index] = existing

    # --- steps
    out_steps: list[dict[str, object]] = []
    used_ids: set[str] = set()
    for i, s in enumerate(steps):
        tname = target_of.get(s.index)
        base_id = f"{_VERB.get(s.tool, s.tool)}_{tname or s.key or 'page'}"
        step_id, n = base_id, 2
        while step_id in used_ids:
            step_id, n = f"{base_id}_{n}", n + 1
        used_ids.add(step_id)
        step: dict[str, object] = {
            "id": step_id,
            "intent": s.reason or step_id.replace("_", " "),
            "action": _action(s, tname, trace),
            "risk": s.risk,
        }
        if s.tool in _PAGE_CHANGING:
            nxt = next(
                (
                    target_of.get(t.index)
                    for t in steps[i + 1 :]
                    if target_of.get(t.index) not in (None, tname)
                ),
                None,
            )
            if nxt:
                step["expect"] = [{"kind": "visible", "target": nxt}]
        out_steps.append(step)

    # --- contract
    used = {
        m
        for s in steps
        for text in (s.value, s.option)
        if text
        for m in re.findall(r"inputs\.(\w+)", text)
    }
    used |= {m for t in targets.values() for m in re.findall(r"inputs\.(\w+)", t.model_dump_json())}
    inputs = {
        name: InputSpec(
            type=p.type,
            description=f"'{name}' parameter of the discovered goal",
            sensitivity=p.sensitivity,
        ).model_dump(mode="json")
        for name, p in trace.params.items()
        if name in used
    }

    goal = re.sub(r"\{\{\s*inputs\.(\w+)\s*\}\}", r"<\1>", trace.goal)
    data = {
        "id": trace.capability_id,
        "version": version,
        "summary": summary or goal,
        "description": f"Discovered from the goal: {goal}",
        "app": {"product": trace.product, "product_versions": product_versions, "surface": "web"},
        "requires": trace.requires,
        "inputs": inputs,
        "outputs": {k: v.model_dump(mode="json") for k, v in trace.outputs.items()},
        "targets": {k: v.model_dump(mode="json") for k, v in targets.items()},
        "steps": out_steps,
        "success": [{"kind": "output", "name": name} for name in trace.outputs],
        "provenance": {
            "source": "discovery",
            "recorded_from_run": trace.run_id,
            "recorded_at": trace.finished_at,
            "model": trace.model,
        },
    }
    return Capability.model_validate(data)


def _action(s: TraceStep, target: str | None, trace: DiscoveryTrace) -> dict[str, object]:
    if s.tool == "extract":
        assert s.output is not None
        return {
            "type": "extract",
            "target": target,
            "into": s.output,
            "parse": _PARSE[trace.outputs[s.output].type],
        }
    if s.tool == "fill":
        return {"type": "fill", "target": target, "value": s.value or ""}
    if s.tool == "select":
        return {"type": "select", "target": target, "option": s.option or ""}
    if s.tool == "check":
        return {"type": "check", "target": target, "checked": bool(s.checked)}
    if s.tool == "press":
        return {"type": "press", "key": s.key or "Enter", **({"target": target} if target else {})}
    return {"type": "click", "target": target}


def merge_outcome(capability: Capability, trace: DiscoveryTrace) -> Capability:
    """Add the business outcome observed by a negative discovery run (e.g. unknown member)
    to an existing capability, as a rule on the step after which it appeared."""
    if trace.status != "outcome" or not trace.outcome_code or not trace.outcome_text:
        raise CompileError("trace does not describe a business outcome")
    last = next(
        (
            s
            for s in reversed(trace.steps)
            if s.actor == "agent" and s.result == "ok" and s.target is not None
        ),
        None,
    )
    if last is None or last.target is None:
        raise CompileError("no action precedes the outcome")
    name = next(
        (n for n, t in capability.targets.items() if t.locators == last.target.locators), None
    )
    if name is None:
        raise CompileError("could not align the outcome with a step of the capability")
    data = capability.model_dump(mode="json")
    step = next(s for s in data["steps"] if s["action"].get("target") == name)
    frame = capability.targets[name].frame
    rule = {
        "when": {
            "kind": "text",
            "pattern": re.escape(trace.outcome_text),
            **({"frame": [f.model_dump() for f in frame]} if frame else {}),
        },
        "outcome": trace.outcome_code,
    }
    if rule not in step.setdefault("outcomes", []):
        step["outcomes"].append(rule)
    data["outcomes"].setdefault(
        trace.outcome_code,
        {"description": trace.summary or trace.outcome_code.replace("_", " ").lower()},
    )
    major, minor, _ = (int(x) for x in capability.version.split("."))
    data["version"] = f"{major}.{minor + 1}.0"
    data["review"] = {"status": "draft"}
    data["provenance"]["content_hash"] = None
    return Capability.model_validate(data)


def next_version(registry: CapabilityRegistry, product: str, cap_id: str) -> str:
    versions = registry.versions(product, cap_id)
    if not versions:
        return "1.0.0"
    major, minor, _ = (int(x) for x in Path(versions[-1]).stem.split("."))
    return f"{major}.{minor + 1}.0"
