"""Human-readable run report, generated from the run's evidence.

The JSONL event log is the source of truth; ``run_report.md`` is only a view of it (plus
``result.json`` / ``trace.json``), so it can always be regenerated and can never disagree
with the log. It reads only already-redacted evidence and so cannot leak anything new.
Markdown was chosen because GitHub renders it (with the screenshot thumbnails) in place.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

REPORT_NAME = "run_report.md"


def _load(
    run_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]:
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    result = (
        json.loads((run_dir / "result.json").read_text())
        if (run_dir / "result.json").exists()
        else None
    )
    trace = (
        json.loads((run_dir / "trace.json").read_text())
        if (run_dir / "trace.json").exists()
        else None
    )
    return events, result, trace


def _cell(text: Any, limit: int = 160) -> str:
    s = " ".join(str(text).split()) if text is not None else ""
    s = s if len(s) <= limit else s[: limit - 1] + "…"
    return s.replace("|", "\\|")


def _img(path: str | None, width: int = 200) -> str:
    return f'<img src="{path}" width="{width}">' if path else ""


def _describe(e: dict[str, Any], intents: dict[str, str]) -> tuple[str, str, str, str] | None:
    """(who, what, step, detail) for one event, or None to leave it out of the timeline."""
    t = e["type"]
    step = str(e.get("step", e.get("step_id", "")) or "")
    if t == "run_started":
        subject = e.get("capability") or ""
        extra = f"goal: {e['goal']}" if e.get("goal") else f"inputs: {e.get('inputs', {})}"
        return "system", f"Run started ({e.get('mode')})", "", f"{subject} {extra}"
    if t == "subrun_started":
        return "automation", "Required capability started", "", e.get("capability", "")
    if t == "subrun_finished":
        return "automation", f"Required capability {e.get('status')}", "", e.get("capability", "")
    if t == "step_started":
        intents[step] = e.get("intent", "")
        return None
    if t == "step_completed":
        via = f" (located via {e['strategy']})" if e.get("strategy") else ""
        return "automation", "Step completed", step, f"{intents.get(step, '')}{via}"
    if t == "step_skipped":
        return "human", "Step skipped", step, ""
    if t == "agent_decision":
        args = e.get("input", {})
        target = f" {args['ref']}" if args.get("ref") else ""
        return "agent", f"Model chose `{e.get('tool')}`{target}", "", args.get("reason", "")
    if t == "trace_step":
        if e.get("result") == "ok":
            return None
        return "system", f"Action {e.get('result')}", str(e.get("index", "")), e.get("detail", "")
    if t == "policy_decision":
        if e.get("verdict") == "allow" and e.get("risk") == "safe":
            return None
        return "policy", f"Policy: {e.get('verdict')} ({e.get('risk')})", step, e.get("reason", "")
    if t == "approval_requested":
        r = e.get("request", {})
        return (
            "automation",
            "Approval requested",
            str(r.get("step", "")),
            f"{r.get('action')}: {r.get('reason')}",
        )
    if t == "approval_resolved":
        verdict = "approved" if e.get("approved") else "rejected"
        return str(e.get("by", "human")), f"Approval {verdict}", "", e.get("note") or ""
    if t == "assist_requested":
        r = e.get("request", {})
        return (
            "automation",
            "Help requested",
            str(r.get("step", "")),
            f"{r.get('reason')}: {r.get('message')}",
        )
    if t == "assist_resolved":
        n = len(e.get("human_actions") or [])
        return (
            str(e.get("by", "human")),
            f"Handed back: {e.get('resolution')}",
            "",
            f"{e.get('note') or ''} ({n} recorded actions)",
        )
    if t == "control_transferred":
        return str(e.get("by")), f"Control: {e.get('frm')} → {e.get('to')}", "", e.get("reason", "")
    if t == "human_action":
        what = e.get("text") or e.get("name") or e.get("tag")
        value = f" = {e['value']}" if e.get("value") else ""
        return (
            str(e.get("by", "human")),
            f"Person {e.get('action')}ed",
            "",
            f"'{what}'{value} in {e.get('frame')}",
        )
    if t == "recovery":
        return (
            "automation",
            f"Recovered from `{e.get('state')}`",
            str(e.get("step_id", "")),
            e.get("action", ""),
        )
    if t == "drift":
        return (
            "automation",
            "Drift: fallback locator used",
            step,
            f"{e.get('target')} via {e.get('used_strategy')} "
            f"(strategy #{int(e.get('used_index', 0)) + 1})",
        )
    if t == "output_extracted":
        return "automation", f"Extracted `{e.get('output')}`", step, str(e.get("value"))
    if t in ("capability_restarted", "discovery_restarted"):
        return (
            "automation",
            "Flow restarted",
            "",
            e.get("reason", f"attempt {e.get('attempt', '')}"),
        )
    if t == "run_finished":
        detail = (
            e.get("outcome") or (e.get("error") or {}).get("category") or e.get("summary") or ""
        )
        return "system", f"Run finished: **{e.get('status')}**", "", detail
    return None


def build_report(run_dir: Path) -> Path:
    events, result, trace = _load(run_dir)
    if not events:
        raise ValueError(f"no events in {run_dir}")
    start = datetime.fromisoformat(events[0]["ts"])
    first = next((e for e in events if e["type"] == "run_started"), events[0])
    finished = next((e for e in reversed(events) if e["type"] == "run_finished"), {})
    mode = first.get("mode", "replay")

    lines = [f"# Run report — `{first.get('run_id', run_dir.name)}`", ""]
    rows = [("Mode", mode)]
    if mode == "discovery":
        rows += [
            ("Goal", first.get("goal", "")),
            ("Capability id", first.get("capability", "")),
            ("Model", first.get("model", "")),
        ]
        if trace:
            rows += [
                ("Status", f"**{trace['status']}**"),
                ("Summary", trace.get("summary", "")),
                ("Agent steps recorded", str(len(trace.get("steps", [])))),
                ("Tokens", str(trace.get("usage", {}))),
            ]
            if trace.get("outcome_code"):
                rows.append(
                    ("Business outcome", f"`{trace['outcome_code']}` — {trace.get('outcome_text')}")
                )
    else:
        rows += [
            ("Capability", first.get("capability", "")),
            ("Content hash", f"`{first.get('content_hash', '')}`"),
            ("Review status", first.get("review", "")),
        ]
        if result:
            rows.append(("Status", f"**{result['status']}**"))
            if result.get("outputs"):
                rows.append(("Outputs", f"`{json.dumps(result['outputs'])}`"))
            if result.get("outcome"):
                o = result["outcome"]
                rows.append(
                    (
                        "Business outcome",
                        f"`{o['code']}` — {o['description']} "
                        f"(from {o['source']}, step `{o.get('step_id')}`)",
                    )
                )
            if result.get("error"):
                err = result["error"]
                rows.append(
                    (
                        "Error",
                        f"`{err['category']}` at step `{err.get('step_id')}` — {err['message']}",
                    )
                )
    end = datetime.fromisoformat(finished["ts"]) if finished else start
    rows.append(("Duration", f"{(end - start).total_seconds():.1f} s"))
    evidence = ["[`events.jsonl`](events.jsonl)", "`screenshots/`"]
    if (run_dir / "result.json").exists():
        evidence.append("[`result.json`](result.json)")
    if (run_dir / "trace.json").exists():
        evidence.append("[`trace.json`](trace.json)")
    if (run_dir / "capability.yaml").exists():
        evidence.append("[`capability.yaml`](capability.yaml)")
    if (run_dir / "trace.UNREDACTED.zip").exists():
        evidence.append(
            "`trace.UNREDACTED.zip` (raw Playwright trace, **contains credentials and unmasked "
            "data**: local debugging only)"
        )
    rows.append(("Evidence", " · ".join(evidence)))
    lines += ["| | |", "|---|---|", *[f"| {k} | {_cell(v, 400)} |" for k, v in rows], ""]

    if result and result.get("error"):
        err = result["error"]
        lines += [
            "## Failure detail",
            "",
            f"- **Category:** `{err['category']}`",
            f"- **Step:** `{err.get('step_id')}`",
            f"- **Expected:** {_cell(err.get('expected'), 600)}",
            f"- **Observed:** {_cell(err.get('observed'), 600)}",
            "",
        ]
        lines += [_img(p, 480) for p in err.get("evidence", [])] + [""]
    if result and (
        result.get("recoveries") or result.get("drift_warnings") or result.get("interventions")
    ):
        lines += ["## Recoveries, drift and interventions", ""]
        lines += [
            f"- Recovered from `{r['state']}` at step `{r['step_id']}`: {r['action']}"
            for r in result.get("recoveries", [])
        ]
        lines += [
            f"- Drift at step `{d['step_id']}`: `{d['target']}` "
            f"resolved by fallback `{d['used_strategy']}`"
            for d in result.get("drift_warnings", [])
        ]
        lines += [f"- Human intervention `{i}`" for i in result.get("interventions", [])]
        lines.append("")

    lines += [
        "## Timeline",
        "",
        "| # | t | who | what | step | detail | screen |",
        "|---|---|---|---|---|---|---|",
    ]
    intents: dict[str, str] = {}
    n = 0
    for e in events:
        described = _describe(e, intents)
        if described is None:
            continue
        who, what, step, detail = described
        n += 1
        t = (datetime.fromisoformat(e["ts"]) - start).total_seconds()
        shot = e.get("screenshot") or (e.get("request") or {}).get("screenshot")
        lines.append(
            f"| {n} | {t:.1f}s | {_cell(who, 30)} | {_cell(what, 80)} | {_cell(step, 40)} "
            f"| {_cell(detail)} | {_img(shot, 160)} |"
        )
    lines += [
        "",
        "_Generated from `events.jsonl` by `cua report`; the log is the source of truth._",
        "",
    ]
    out = run_dir / REPORT_NAME
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
