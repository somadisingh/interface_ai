"""The discovery loop: an LLM completes a goal on the live application, one guarded action
per turn, while the recorder captures a replayable trace.

Loop (per turn):
  1. Known interruptions (app profile) are handled exactly as replay would handle them, so
     they never become part of the recorded flow.
  2. Observe: numbered elements + a screenshot, both passed through the model-side redactor.
  3. Decide: the model returns exactly one tool call.
  4. Guard: refs are checked, the element's locator bundle is captured *before* acting,
     and the policy classifies the action; irreversible actions need human approval.
  5. Act, settle, record.

Stopping conditions: the model finishes; the step budget or time budget runs out; the agent
is stuck (the same action repeated without the page changing, or repeated errors). Stuck
states are escalated to a human through the same channel replay uses.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from cua.agent.llm import LLMClient, ToolCall
from cua.agent.prompting import TOOLS, render_observation, system_prompt
from cua.evidence.log import EventLog, utcnow
from cua.handoff.channel import (
    ApprovalRequest,
    AssistRequest,
    HumanChannel,
    new_request_id,
)
from cua.policy import PolicyEngine, Redactor
from cua.policy.engine import ActionType
from cua.recorder.trace import DiscoveryTrace, ParamSpec, Templater, TraceStep
from cua.replay.engine import HandlerError, ReplayEngine, RunStop
from cua.replay.support import ParseError, parse_value
from cua.schema.actions import ParseAs
from cua.schema.capability import OutputSpec
from cua.surface.base import ActionResult, Observation
from cua.surface.web import WebSurface

_PARSE_FOR: dict[str, ParseAs] = {
    "string": "text",
    "enum": "text",
    "boolean": "text",
    "money": "money",
    "integer": "integer",
    "decimal": "decimal",
    "date": "date",
}
_ACTION_TOOLS = {"click", "fill", "select", "check", "press", "extract"}
KEEP_FULL_OBSERVATIONS = 2


@dataclass
class DiscoverySpec:
    capability_id: str
    product: str
    goal: str
    params: dict[str, str]
    outputs: dict[str, OutputSpec]
    requires: list[str] = field(default_factory=list)
    param_types: dict[str, ParamSpec] = field(default_factory=dict)


class _Finished(Exception):
    pass


class DiscoveryAgent:
    def __init__(
        self,
        surface: WebSurface,
        llm: LLMClient,
        *,
        engine: ReplayEngine,
        policy: PolicyEngine,
        human: HumanChannel,
        log: EventLog,
        model_redactor: Redactor,
        max_steps: int = 30,
        max_seconds: int = 300,
        app_description: str = "",
    ) -> None:
        self.surface = surface
        self.llm = llm
        self.engine = engine
        self.policy = policy
        self.human = human
        self.log = log
        self.model_redactor = model_redactor
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self.app_description = app_description

    # ================================================================== main loop

    def run(self, spec: DiscoverySpec) -> DiscoveryTrace:
        templater = Templater(spec.params)
        params = {k: spec.param_types.get(k, ParamSpec()) for k in spec.params}
        for name, value in spec.params.items():
            if params[name].sensitivity == "pii":
                self.log.redactor.add_pii(value)
        self._trace = DiscoveryTrace(
            run_id=self.log.run_id,
            capability_id=spec.capability_id,
            product=spec.product,
            goal=templater.text(spec.goal) or spec.goal,
            params=params,
            outputs=spec.outputs,
            requires=spec.requires,
            model=self.llm.model,
            started_at=utcnow(),
        )
        self._steps: list[TraceStep] = []
        self._extracted: dict[str, Any] = {}
        self._templater = templater
        self._spec = spec
        self._usage = {"input_tokens": 0, "output_tokens": 0}
        self.log.emit(
            "run_started",
            mode="discovery",
            capability=spec.capability_id,
            goal=spec.goal,
            model=self.llm.model,
        )

        try:
            self.engine.ensure_requirements(spec.product, spec.requires)
        except RunStop as stop:
            return self._end("failed", f"could not establish a session: {stop.error}")

        system = system_prompt(
            spec.goal,
            spec.params,
            {k: (o.type, o.description) for k, o in spec.outputs.items()},
            self.app_description or spec.product,
        )
        messages: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        deadline = time.monotonic() + self.max_seconds
        last_signature: tuple[str, str] | None = None
        repeats = errors = 0
        budget = self.max_steps

        try:
            while budget > 0:
                if time.monotonic() > deadline:
                    budget = self._escalate("stuck", "time budget exhausted", budget)
                    deadline = time.monotonic() + self.max_seconds / 2
                    continue
                self._handle_interruptions()
                obs = self.surface.observe()
                page_state = self._page_hash(obs)
                content = [*pending, *self._observation_blocks(obs)]
                pending = []
                messages.append({"role": "user", "content": content})
                try:
                    reply = self.llm.complete(system, _compact(messages), TOOLS)
                except Exception as exc:  # provider outage, quota, bad key: stop with evidence
                    detail = f"{type(exc).__name__}: {exc}"[:500]
                    self.log.emit("llm_error", model=self.llm.model, error=detail)
                    return self._end("aborted", f"model call failed after retries ({detail})")
                messages.append({"role": "assistant", "content": reply.content})
                for k, v in reply.usage.items():
                    self._usage[k] = self._usage.get(k, 0) + v
                budget -= 1
                call = reply.tool_call
                if call is None:
                    messages.append({"role": "user", "content": "Call exactly one tool."})
                    continue
                self.log.emit(
                    "agent_decision",
                    turn=self.max_steps - budget,
                    tool=call.name,
                    input=call.input,
                    text=reply.text,
                    usage=reply.usage,
                )
                ok, message = self._dispatch(call, obs)
                pending = [_tool_result(call, message, error=not ok)]

                # Stuck detection: the same action again without the page changing, or a
                # run of errors, means the agent is not making progress.
                signature = (call.name, str(sorted(call.input.items())))
                repeats = (
                    repeats + 1
                    if (
                        signature == last_signature
                        and page_state == self._page_hash(self.surface.observe())
                    )
                    else 0
                )
                last_signature = signature
                errors = errors + 1 if not ok else 0
                if repeats >= 2 or errors >= 3:
                    why = "repeating the same action" if repeats >= 2 else "repeated errors"
                    budget = self._escalate("stuck", f"agent appears stuck ({why})", budget)
                    repeats = errors = 0
            return self._end("aborted", "step budget exhausted")
        except _Finished:
            return self._trace
        except RunStop as stop:
            return self._end("failed", f"stopped: {stop.error}")

    # ================================================================== tools

    def _dispatch(self, call: ToolCall, obs: Observation) -> tuple[bool, str]:
        args = call.input
        if call.name == "finish":
            return self._finish(args)
        if call.name == "wait":
            self.surface.wait(int(args.get("seconds", 1)) * 1000)
            self.surface.settle()
            return True, "waited"
        if call.name == "request_human":
            resolution = self._assist("agent_requested", str(args.get("reason", "")))
            return resolution != "abort", f"human resolved the request: {resolution}"
        if call.name not in _ACTION_TOOLS:
            return False, f"unknown tool {call.name!r}"
        return self._action(call, obs)

    def _action(self, call: ToolCall, obs: Observation) -> tuple[bool, str]:
        args = call.input
        tool: ActionType = call.name  # type: ignore[assignment]
        reason = str(args.get("reason", ""))
        ref = args.get("ref")
        element = obs.element(ref) if ref else None
        if ref and element is None:
            return False, f"unknown ref {ref!r}; use refs from the latest observation"
        if tool == "extract" and args.get("output") not in self._spec.outputs:
            return (
                False,
                f"unknown output {args.get('output')!r}; expected one of "
                f"{sorted(self._spec.outputs)}",
            )

        resolved = self.surface.ref_locator(ref) if ref else None
        if ref and resolved is None:
            return False, f"element {ref} is no longer on the page"
        target = None
        if ref:
            try:
                target = self.surface.describe_ref(
                    ref, purpose="read" if tool == "extract" else "act"
                )
            except (KeyError, LookupError) as exc:
                return False, f"cannot identify {ref} reliably: {exc}"
        label = (element.name or element.text) if element else ""
        decision = self.policy.check_action(tool, label=label, mode="discovery")
        step = TraceStep(
            index=len(self._steps),
            tool=tool,
            reason=self._templater.text(reason) or "",
            target=self._templater.target(target) if target else None,
            target_label=self._templater.text(label),
            value=self._templater.text(args.get("value")),
            option=self._templater.text(args.get("option")),
            key=args.get("key"),
            checked=args.get("checked"),
            output=args.get("output"),
            risk=decision.risk,
            verdict=decision.verdict,
        )
        self.log.emit(
            "policy_decision",
            step=step.index,
            verdict=decision.verdict,
            risk=decision.risk,
            reason=decision.reason,
        )
        if decision.verdict == "deny":
            self._record(step.model_copy(update={"result": "denied", "detail": decision.reason}))
            return False, f"not allowed: {decision.reason}"
        if decision.verdict == "require_approval":
            request = ApprovalRequest(
                id=new_request_id("apr"),
                run_id=self.log.run_id,
                subject=self._trace.goal,
                step=str(step.index),
                action=f"{tool} {label!r}",
                risk=decision.risk,
                reason=decision.reason,
                url=self.surface.current_url(),
                screenshot=self.log.screenshot(self.surface, f"approval-{step.index}"),
            )
            self._trace.interventions.append(request.id)
            self.log.emit("approval_requested", request=request.__dict__)
            response = self.human.approve(request)
            self.log.emit(
                "approval_resolved",
                request_id=request.id,
                approved=response.approved,
                by=response.by,
                note=response.note,
            )
            if not response.approved:
                self._record(
                    step.model_copy(update={"result": "rejected", "detail": response.note})
                )
                return False, f"a human rejected this action: {response.note or 'no reason given'}"
            step = step.model_copy(update={"approved_by": response.by})

        if tool == "extract":
            assert resolved is not None
            output = str(args["output"])
            text = self.surface.read_text(resolved)
            spec = self._spec.outputs[output]
            try:
                value = parse_value(text, _PARSE_FOR[spec.type])
            except ParseError as exc:
                return False, f"that element's text does not look like a {spec.type}: {exc}"
            if spec.sensitivity == "pii":
                self.log.redactor.add_pii(text)
                if isinstance(value, dict):
                    self.log.redactor.add_pii_values(
                        str(v) for v in value.values() if len(str(v)) > 3
                    )
                else:
                    self.log.redactor.add_pii(str(value))
            self._extracted[output] = value
            self.log.emit("output_extracted", step=step.index, output=output, value=value)
            self._record(step.model_copy(update={"url_after": self.surface.current_url()}))
            return True, f"extracted {output}"

        result = self._perform(tool, args, resolved)
        if not result.completed:
            self._record(step.model_copy(update={"result": "error", "detail": result.detail}))
            return False, f"{tool} failed ({result.kind}): {result.detail}"
        self.surface.settle()
        self._record(step.model_copy(update={"url_after": self.surface.current_url()}))
        return True, "done"

    def _perform(self, tool: str, args: dict[str, Any], resolved: Any) -> ActionResult:
        if tool == "click":
            return self.surface.click(resolved)
        if tool == "fill":
            return self.surface.fill(resolved, str(args["value"]))
        if tool == "select":
            return self.surface.select(resolved, str(args["option"]))
        if tool == "check":
            return self.surface.set_checked(resolved, bool(args["checked"]))
        return self.surface.press(str(args["key"]), resolved)

    def _finish(self, args: dict[str, Any]) -> tuple[bool, str]:
        summary = str(args.get("summary", ""))
        if args.get("success"):
            missing = sorted(set(self._spec.outputs) - set(self._extracted))
            if missing:
                return False, f"cannot finish yet: outputs not extracted: {missing}"
            self._end("succeeded", summary)
            raise _Finished
        code, text = args.get("outcome_code"), args.get("outcome_text")
        if code and text:
            on_screen = self.surface.text_excerpt(limit=50_000)
            if " ".join(str(text).split()) not in on_screen:
                return False, "outcome_text must be copied exactly from the screen"
            self._trace = self._trace.model_copy(
                update={"outcome_code": code, "outcome_text": text}
            )
            self._end("outcome", summary)
        else:
            self._end("failed", summary)
        raise _Finished

    # ================================================================== human & interruptions

    def _escalate(self, reason: Literal["stuck"], message: str, budget: int) -> int:
        resolution = self._assist(reason, message)
        if resolution == "abort":
            self._end("aborted", f"{message}; operator aborted")
            raise _Finished
        return max(budget, 5)  # a little more room after a human helped

    def _assist(self, reason: Literal["stuck", "agent_requested"], message: str) -> str:
        request = AssistRequest(
            id=new_request_id("hlp"),
            run_id=self.log.run_id,
            subject=self._trace.goal,
            step=str(len(self._steps)),
            reason=reason,
            message=message,
            url=self.surface.current_url(),
            screenshot=self.log.screenshot(self.surface, "assist"),
        )
        self._trace.interventions.append(request.id)
        self.log.emit("assist_requested", request=request.__dict__)
        response = self.human.assist(request)
        self.log.emit(
            "assist_resolved",
            request_id=request.id,
            resolution=response.resolution,
            by=response.by,
            note=response.note,
            human_actions=response.human_actions,
        )
        for action in response.human_actions:
            self._record(
                TraceStep(
                    index=len(self._steps),
                    actor="human",
                    tool=str(action.get("action", "manual")),
                    detail=str(action),
                )
            )
        self.surface.settle()
        return response.resolution

    def _handle_interruptions(self) -> None:
        for _ in range(3):
            try:
                handled = self.engine.recover_interruption(self._spec.product)
            except HandlerError as exc:
                raise RunStop("failed") from exc
            if handled is None:
                return
            state, then = handled
            self._trace.recoveries.append(state)
            if then == "restart_capability":
                # Session was re-established; the recorded path so far no longer applies.
                self._steps.clear()
                self._extracted.clear()
                self.log.emit("discovery_restarted", reason=state)

    # ================================================================== helpers

    def _observation_blocks(self, obs: Observation) -> list[dict[str, Any]]:
        text = render_observation(obs, self.model_redactor)
        shot_path = self.log.run_dir / "model-view.png"
        self.surface.screenshot(shot_path, mask_text_patterns=self.model_redactor.dom_patterns())
        self.log.screenshot(self.surface, f"turn{len(self._steps):02d}")
        image = base64.b64encode(shot_path.read_bytes()).decode()
        return [
            {"type": "text", "text": text},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": image},
            },
        ]

    @staticmethod
    def _page_hash(obs: Observation) -> str:
        key = obs.url + "|" + "|".join(f"{e.role}:{e.name}:{e.value}" for e in obs.elements)
        return hashlib.sha1(key.encode()).hexdigest()

    def _record(self, step: TraceStep) -> None:
        self._steps.append(step)
        self.log.emit("trace_step", **step.model_dump(mode="json", exclude_none=True))

    def _end(
        self, status: Literal["succeeded", "outcome", "failed", "aborted"], summary: str
    ) -> DiscoveryTrace:
        self._trace = self._trace.model_copy(
            update={
                "status": status,
                "summary": self._templater.text(summary) or "",
                "steps": list(self._steps),
                "finished_at": utcnow(),
                "usage": dict(self._usage),
            }
        )
        self.log.emit("run_finished", status=status, summary=summary, usage=self._usage)
        self.log.write_json("trace.json", self._trace.model_dump(mode="json"))
        return self._trace


def _tool_result(call: ToolCall, message: str, *, error: bool) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": call.id, "content": message, "is_error": error}


def _compact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep full observations (text + screenshot) only for the latest turns; older ones are
    reduced to a stub so the context stays small. Tool calls/results are always kept."""
    user_turns = [
        i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], list)
    ]
    keep = set(user_turns[-KEEP_FULL_OBSERVATIONS:])
    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        if m["role"] != "user" or i in keep or not isinstance(m["content"], list):
            out.append(m)
            continue
        blocks = [b for b in m["content"] if b.get("type") == "tool_result"]
        blocks.append({"type": "text", "text": "(earlier observation omitted)"})
        out.append({"role": "user", "content": blocks})
    return out
