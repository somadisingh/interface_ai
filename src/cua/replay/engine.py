"""Deterministic replay: run a capability with caller inputs, no model in the loop.

Per step:

1. **Resolve** the target with its ordered locator bundle, polling until the step deadline.
   While it doesn't resolve, the page is checked against the app profile's known states:
   interstitials are handled and the step resumes; business outcomes and known failures end
   the run with the right status. A fallback strategy that wins is recorded as drift.
2. **Gate** the action through policy: allowlist, effective risk, tiered approval.
3. **Act**. If the action could not complete (e.g. an overlay intercepted the click), known
   states are handled and the action is retried — it never completed, so a retry can't
   double-submit. An action that completed is never repeated.
4. **Verify** the step's checkpoint, polling until the deadline, while watching for the
   step's business-outcome rules and the profile's known states.

Nothing unknown is ever "proceeded past": an unrecognised state becomes a hard failure (or,
if escalation is enabled, a request for a human to take over the live session).
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from cua.evidence.log import EventLog, utcnow
from cua.handoff.channel import (
    ApprovalRequest,
    AssistRequest,
    HumanChannel,
    new_request_id,
)
from cua.policy import PolicyEngine, Redactor, SecretStore
from cua.policy.secrets import MissingSecretError
from cua.replay.support import (
    CapabilityRegistry,
    InputError,
    ParseError,
    parse_value,
    validate_inputs,
)
from cua.schema import AppProfile, Capability, KnownState, RunResult, Step, templates
from cua.schema.actions import Check, Click, Extract, Fill, Navigate, Press, Select
from cua.schema.app_profile import FailureCategory
from cua.schema.conditions import AllOf, AnyOf, Condition, OutputPresent
from cua.schema.locators import TargetSpec
from cua.schema.result import (
    DriftWarning,
    OutcomeInfo,
    RecoveryRecord,
    RunError,
    RunStatus,
)
from cua.schema.tenancy import Tenant, apply_overlay, version_in_range
from cua.surface.base import ActionResult, Resolved
from cua.surface.web import WebSurface

MAX_RESTARTS = 2


class RunStop(Exception):
    """Ends a run with a final status (used internally; also raised by
    ``ensure_requirements`` to callers such as discovery)."""

    def __init__(
        self,
        status: RunStatus,
        *,
        outcome: OutcomeInfo | None = None,
        error: RunError | None = None,
    ) -> None:
        super().__init__(status)
        self.status = status
        self.outcome = outcome
        self.error = error


class _Restart(Exception):
    pass


class _SkipStep(Exception):
    pass


class HandlerError(Exception):
    def __init__(self, category: FailureCategory, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass
class _Run:
    cap: Capability
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    recoveries: list[RecoveryRecord] = field(default_factory=list)
    drift: list[DriftWarning] = field(default_factory=list)
    interventions: list[str] = field(default_factory=list)
    step_recoveries: dict[str, int] = field(default_factory=dict)
    irreversible_done: bool = False
    restarts: int = 0
    ignore_states: frozenset[str] = frozenset()


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(2)}"


class ReplayEngine:
    def __init__(
        self,
        surface: WebSurface,
        *,
        profile: AppProfile,
        policy: PolicyEngine,
        registry: CapabilityRegistry,
        human: HumanChannel,
        log: EventLog,
        redactor: Redactor,
        secrets: SecretStore,
        escalate: bool = False,
        poll_ms: int = 150,
        mode: Literal["replay", "discovery"] = "replay",
        tenant: Tenant | None = None,
    ) -> None:
        self.surface = surface
        self.tenant = tenant
        self.app_version: str | None = None
        self.profile = profile
        self.policy = policy
        self.registry = registry
        self.human = human
        self.log = log
        self.redactor = redactor
        self.secrets = secrets
        self.escalate = escalate
        self.poll_ms = poll_ms
        self.mode = mode
        self._session_done: set[str] = set()

    # ================================================================== entry points

    def run(self, cap: Capability, inputs: Mapping[str, str]) -> RunResult:
        """Top-level invocation: returns the caller-facing result contract."""
        started = utcnow()
        cap = self._effective(cap)
        state = _Run(cap)
        for name, spec in cap.inputs.items():
            if spec.sensitivity == "pii" and inputs.get(name):
                self.redactor.add_pii(inputs[name])
        self.log.emit(
            "run_started",
            mode="replay",
            capability=cap.ref,
            content_hash=cap.content_hash(),
            review=cap.review.status,
            inputs=dict(inputs),
        )
        status: RunStatus = "success"
        outcome: OutcomeInfo | None = None
        error: RunError | None = None
        try:
            self._execute(state, inputs, top_level=True)
        except RunStop as stop:
            status, outcome, error = stop.status, stop.outcome, stop.error
        result = RunResult(
            run_id=self.log.run_id,
            capability=cap.ref,
            content_hash=cap.content_hash(),
            tenant=self.tenant.config.tenant if self.tenant else None,
            app_version=self.app_version,
            status=status,
            outputs=state.outputs if status == "success" else {},
            outcome=outcome,
            error=error,
            recoveries=state.recoveries,
            drift_warnings=state.drift,
            interventions=state.interventions,
            started_at=started,
            finished_at=utcnow(),
            evidence_dir=str(self.log.run_dir),
        )
        self.log.emit(
            "run_finished",
            status=status,
            outcome=outcome.code if outcome else None,
            error=error.model_dump() if error else None,
            outputs=result.outputs,
            duration_ms=result.duration_ms,
        )
        self.log.write_json("result.json", result.model_dump(mode="json"))
        return result

    def run_nested(
        self, cap: Capability, inputs: Mapping[str, str], *, ignore: frozenset[str] = frozenset()
    ) -> tuple[RunStatus, OutcomeInfo | None, RunError | None]:
        """Run a capability inside the current run (e.g. sign-on). Logged, not reported."""
        cap = self._effective(cap)
        state = _Run(cap, ignore_states=ignore)
        self.log.emit("subrun_started", capability=cap.ref)
        try:
            self._execute(state, inputs, top_level=False)
            result: tuple[RunStatus, OutcomeInfo | None, RunError | None] = ("success", None, None)
        except RunStop as stop:
            result = (stop.status, stop.outcome, stop.error)
        self.log.emit(
            "subrun_finished",
            capability=cap.ref,
            status=result[0],
            outcome=result[1].code if result[1] else None,
        )
        return result

    def ensure_session(self, cap: Capability) -> None:
        """Run the capabilities ``cap`` requires (once per engine/session)."""
        self.ensure_requirements(cap.app.product, cap.requires)

    def ensure_requirements(self, product: str, requires: list[str]) -> None:
        for req in requires:
            if req in self._session_done:
                continue
            sub = self.registry.get(product, req)
            status, outcome, error = self.run_nested(sub, {}, ignore=frozenset({"session_expired"}))
            if status != "success":
                detail = outcome.code if outcome else (error.message if error else status)
                raise RunStop(
                    "failed",
                    error=RunError(
                        category="SESSION_UNRECOVERABLE",
                        message=f"required capability {req!r} did not succeed: {detail}",
                    ),
                )
            self._session_done.add(req)

    # ================================================================== run loop

    def _execute(self, state: _Run, inputs: Mapping[str, str], *, top_level: bool) -> None:
        try:
            state.inputs = validate_inputs(state.cap.inputs, inputs)
        except InputError as exc:
            raise RunStop(
                "failed", error=RunError(category="INVALID_INPUT", message=str(exc))
            ) from exc
        self.ensure_session(state.cap)
        if top_level and state.cap.requires:
            self._check_application(state.cap)

        index = 0
        steps = state.cap.steps
        while index < len(steps):
            step = steps[index]
            try:
                self._run_step(state, step)
            except _SkipStep:
                self.log.emit("step_skipped", step=step.id, by="human")
            except _Restart:
                if state.irreversible_done:
                    raise RunStop(
                        "failed",
                        error=RunError(
                            category="SESSION_UNRECOVERABLE",
                            step_id=step.id,
                            message="session was lost after an irreversible step had already "
                            "executed; restarting could repeat it",
                        ),
                    ) from None
                state.restarts += 1
                if state.restarts > MAX_RESTARTS:
                    raise RunStop(
                        "failed",
                        error=RunError(
                            category="RECOVERY_EXHAUSTED",
                            step_id=step.id,
                            message="capability restarted too many times",
                        ),
                    ) from None
                self.log.emit("capability_restarted", attempt=state.restarts)
                state.outputs.clear()
                index = 0
                continue
            index += 1

        for cond in state.cap.success:
            if not self._check(cond, state):
                raise RunStop(
                    "failed",
                    error=self._error(
                        state,
                        None,
                        "CHECKPOINT_FAILED",
                        "success condition not met",
                        expected=_describe(cond),
                    ),
                )

    def _run_step(self, state: _Run, step: Step) -> None:
        self.log.emit("step_started", step=step.id, intent=step.intent, action=step.action.type)
        deadline = self._deadline(step)
        action = step.action
        resolved: Resolved | None = None

        if isinstance(action, Navigate):
            self._navigate(state, step, action)
        else:
            target_name = getattr(action, "target", None)
            if target_name is not None:
                resolved = self._resolve(state, step, target_name, deadline)
            self._gate(state, step, resolved)
            self._perform(state, step, resolved, deadline)

        self.surface.settle()
        self._verify(state, step, self._deadline(step))
        shot = self.log.screenshot(self.surface, step.id, mask=self._sensitive(state))
        self.log.emit(
            "step_completed",
            step=step.id,
            strategy=resolved.strategy if resolved else None,
            screenshot=shot,
        )

    # ================================================================== phases

    def _resolve(self, state: _Run, step: Step, target_name: str, deadline: float) -> Resolved:
        target = state.cap.targets[target_name]
        while True:
            res = self.surface.resolve(target, state.inputs)
            if isinstance(res, Resolved):
                if res.strategy_index > 0:
                    warning = DriftWarning(
                        step_id=step.id,
                        target=target_name,
                        used_strategy=res.strategy,
                        used_index=res.strategy_index,
                    )
                    state.drift.append(warning)
                    self.log.emit(
                        "drift", **warning.model_dump(), attempts=[a.__dict__ for a in res.attempts]
                    )
                return res
            if self._handle_known_state(state, step):
                deadline = max(deadline, self._deadline(step))
                continue
            if time.monotonic() > deadline:
                category: FailureCategory = (
                    "LOCATOR_AMBIGUOUS" if res.reason == "ambiguous" else "LOCATOR_NOT_FOUND"
                )
                self._fail_or_escalate(
                    state,
                    step,
                    category,
                    f"could not find {target_name!r} ({target.description})",
                    expected=f"exactly one visible element for target {target_name!r}",
                    observed=res.describe(),
                )
                deadline = self._deadline(step)  # human resumed: try again
                continue
            self.surface.wait(self.poll_ms)

    def _gate(self, state: _Run, step: Step, resolved: Resolved | None) -> None:
        target = getattr(step.action, "target", None)
        label = self.surface.label_of(resolved) if resolved else ""
        if target and not label:
            label = state.cap.targets[target].description
        decision = self.policy.check_action(
            step.action.type,
            label=label,
            mode=self.mode,
            declared_risk=step.risk,
            capability_approved=state.cap.review.status == "approved",
            auto_approve=step.auto_approve_on_replay,
            reviewer=state.cap.review.reviewed_by,
        )
        self.log.emit(
            "policy_decision",
            step=step.id,
            verdict=decision.verdict,
            risk=decision.risk,
            reason=decision.reason,
        )
        if decision.verdict == "deny":
            raise RunStop(
                "failed", error=self._error(state, step, "POLICY_VIOLATION", decision.reason)
            )
        if decision.verdict == "require_approval":
            shot = self.log.screenshot(
                self.surface, f"{step.id}-approval", mask=self._sensitive(state)
            )
            request = ApprovalRequest(
                id=new_request_id("apr"),
                run_id=self.log.run_id,
                subject=state.cap.ref,
                step=step.id,
                action=f"{step.action.type} {label!r}",
                risk=decision.risk,
                reason=decision.reason,
                url=self.surface.current_url(),
                screenshot=shot,
            )
            state.interventions.append(request.id)
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
                raise RunStop(
                    "escalated",
                    error=self._error(
                        state,
                        step,
                        "APPROVAL_DENIED",
                        f"{response.by} did not approve: {response.note or 'rejected'}",
                    ),
                )
        if decision.risk == "irreversible":
            state.irreversible_done = True  # from here on, never restart the flow

    def _perform(self, state: _Run, step: Step, resolved: Resolved | None, deadline: float) -> None:
        action = step.action
        while True:
            if isinstance(action, Extract):
                assert resolved is not None
                self._extract(state, step, action, resolved)
                return
            result = self._do(state, step, resolved)
            if result.completed:
                self.log.emit("action_performed", step=step.id, action=action.type)
                return
            # The action never completed, so repeating it is safe.
            if self._handle_known_state(state, step):
                deadline = max(deadline, self._deadline(step))
            elif time.monotonic() > deadline:
                category: FailureCategory = (
                    "UNKNOWN_STATE" if result.kind == "not_actionable" else "APP_ERROR"
                )
                self._fail_or_escalate(
                    state,
                    step,
                    category,
                    f"{action.type} could not be performed ({result.kind}): {result.detail}",
                    expected=f"{action.type} on an actionable element",
                    observed=self.surface.text_excerpt(),
                )
                deadline = self._deadline(step)
            else:
                self.surface.wait(self.poll_ms)
            target_name = getattr(action, "target", None)
            if target_name is not None:  # element may have re-rendered
                resolved = self._resolve(state, step, target_name, deadline)

    def _do(self, state: _Run, step: Step, resolved: Resolved | None) -> ActionResult:
        action = step.action
        try:
            if isinstance(action, Click):
                assert resolved is not None
                return self.surface.click(resolved)
            if isinstance(action, Fill):
                assert resolved is not None
                return self.surface.fill(resolved, self._render(state, step, action.value))
            if isinstance(action, Select):
                assert resolved is not None
                return self.surface.select(resolved, self._render(state, step, action.option))
            if isinstance(action, Check):
                assert resolved is not None
                return self.surface.set_checked(resolved, action.checked)
            if isinstance(action, Press):
                return self.surface.press(action.key, resolved)
        except MissingSecretError as exc:
            raise RunStop(
                "failed", error=self._error(state, step, "CONFIGURATION_ERROR", str(exc))
            ) from exc
        raise TypeError(f"unsupported action {action!r}")  # pragma: no cover

    def _render(self, state: _Run, step: Step, text: str) -> str:
        return templates.render(text, state.inputs, self.secrets)

    def _extract(self, state: _Run, step: Step, action: Extract, resolved: Resolved) -> None:
        text = self.surface.read_text(resolved)
        spec = state.cap.outputs[action.into]
        if spec.sensitivity == "pii":
            self.redactor.add_pii(text)
        try:
            value = parse_value(text, action.parse, action.pattern)
        except ParseError as exc:
            raise RunStop(
                "failed",
                error=self._error(
                    state,
                    step,
                    "CHECKPOINT_FAILED",
                    f"could not read {action.into!r}: {exc}",
                    expected=f"a {action.parse} value",
                    observed=text,
                ),
            ) from exc
        if spec.sensitivity == "pii" and isinstance(value, dict):
            self.redactor.add_pii_values(str(v) for v in value.values() if len(str(v)) > 3)
        state.outputs[action.into] = value
        self.log.emit("output_extracted", step=step.id, output=action.into, value=value)

    def _navigate(self, state: _Run, step: Step, action: Navigate) -> None:
        path = self._render(state, step, action.path)
        decision = self.policy.check_url(self.surface.base_url.rstrip("/") + "/" + path.lstrip("/"))
        if not decision.allowed:
            raise RunStop(
                "failed", error=self._error(state, step, "POLICY_VIOLATION", decision.reason)
            )
        result = self.surface.navigate(path)
        if not result.completed:
            raise RunStop(
                "failed",
                error=self._error(state, step, "APP_ERROR", f"navigation failed: {result.detail}"),
            )

    def _verify(self, state: _Run, step: Step, deadline: float) -> None:
        while True:
            if all(self._check(c, state) for c in step.expect):
                # Even when no checkpoint is declared, make sure the action didn't land on
                # a known outcome/error page.
                for rule in step.outcomes:
                    if self._check(rule.when, state):
                        self._raise_outcome(state, step, rule.outcome)
                if not step.expect:
                    self._handle_known_state(state, step)
                return
            for rule in step.outcomes:
                if self._check(rule.when, state):
                    self._raise_outcome(state, step, rule.outcome)
            if self._handle_known_state(state, step):
                deadline = max(deadline, self._deadline(step))
                continue
            if time.monotonic() > deadline:
                self._fail_or_escalate(
                    state,
                    step,
                    "CHECKPOINT_FAILED",
                    f"step {step.id!r} did not reach its expected state",
                    expected=" and ".join(_describe(c) for c in step.expect),
                    observed=self.surface.text_excerpt(),
                )
                deadline = self._deadline(step)
                continue
            self.surface.wait(self.poll_ms)

    # ================================================================== known states

    def _detect(self, ignore: frozenset[str] = frozenset()) -> tuple[str, KnownState] | None:
        for name, known in self.profile.states.items():
            if name in ignore:
                continue
            if self.surface.check(known.detect, self.profile.targets, {}):
                return name, known
        return None

    def _handle_known_state(self, state: _Run, step: Step) -> bool:
        """Returns True if a recoverable state was handled (caller resumes). Raises for
        business outcomes, known failures and restarts. False if nothing is recognised."""
        found = self._detect(state.ignore_states)
        if found is None:
            return False
        name, known = found
        if known.kind == "business_outcome":
            assert known.outcome is not None
            raise RunStop(
                "business_outcome",
                outcome=OutcomeInfo(
                    code=known.outcome,
                    description=known.description,
                    retryable=known.retryable,
                    step_id=step.id,
                    source="app_profile",
                ),
            )
        if known.kind == "failure":
            assert known.failure_category is not None
            raise RunStop(
                "failed",
                error=self._error(
                    state,
                    step,
                    known.failure_category,
                    known.description,
                    observed=self.surface.text_excerpt(),
                ),
            )
        count = state.step_recoveries.get(step.id, 0) + 1
        state.step_recoveries[step.id] = count
        total = sum(state.step_recoveries.values())
        policy = state.cap.recovery
        if count > policy.max_recoveries_per_step or total > policy.max_recoveries_total:
            raise RunStop(
                "failed",
                error=self._error(
                    state,
                    step,
                    "RECOVERY_EXHAUSTED",
                    f"state {name!r} kept recurring ({count} times at this step)",
                ),
            )
        handler = known.handler
        assert handler is not None
        try:
            done = self._run_handler(name, known, state.cap.app.product)
        except HandlerError as exc:
            error = self._error(state, step, exc.category, exc.message)
            raise RunStop("failed", error=error) from exc
        record = RecoveryRecord(
            step_id=step.id,
            state=name,
            action=f"{', '.join(done)}; then {handler.then}",
            attempt=count,
        )
        state.recoveries.append(record)
        self.log.emit("recovery", **record.model_dump())
        self.surface.settle()
        if handler.then == "restart_capability":
            raise _Restart
        return True

    def _run_handler(self, name: str, known: KnownState, product: str) -> list[str]:
        handler = known.handler
        assert handler is not None
        done: list[str] = []
        for action in handler.actions:
            target_name = getattr(action, "target", None)
            target = self.profile.targets[target_name] if target_name else None
            element = self._resolve_quick(target) if target else None
            if target is not None and element is None:
                raise HandlerError(
                    "UNKNOWN_STATE",
                    f"recognised {name!r} but could not find {target_name!r} to handle it",
                )
            if isinstance(action, Click) and element is not None:
                self.surface.click(element)
            done.append(f"{action.type} {target_name}")
        if handler.run_capability:
            sub = self.registry.get(product, handler.run_capability)
            status, outcome, error = self.run_nested(sub, {}, ignore=frozenset({name}))
            if status != "success":
                detail = outcome.code if outcome else error.message if error else status
                raise HandlerError(
                    "SESSION_UNRECOVERABLE",
                    f"{handler.run_capability} failed while recovering from {name!r}: {detail}",
                )
            done.append(f"run {handler.run_capability}")
        return done

    def recover_interruption(self, product: str) -> tuple[str, str] | None:
        """For discovery: if a *recoverable* known state is on screen, handle it exactly as
        replay would, so interruptions never become part of a recorded flow.

        Returns ``(state, then)`` when something was handled, else ``None``. Business
        outcomes and failures are left on screen for the agent to see."""
        found = self._detect()
        if found is None:
            return None
        name, known = found
        if known.kind not in ("interstitial", "session_expired") or known.handler is None:
            return None
        done = self._run_handler(name, known, product)
        self.log.emit(
            "recovery",
            step_id="(discovery)",
            state=name,
            action=f"{', '.join(done)}; then {known.handler.then}",
            attempt=1,
        )
        self.surface.settle()
        return name, known.handler.then

    def _resolve_quick(self, target: TargetSpec, timeout_ms: int = 3000) -> Resolved | None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            res = self.surface.resolve(target, {})
            if isinstance(res, Resolved):
                return res
            self.surface.wait(self.poll_ms)
        return None

    def _effective(self, cap: Capability) -> Capability:
        """Apply the tenant's overlay for this capability, if there is one."""
        if self.tenant is None or cap.id not in self.tenant.overlays:
            return cap
        overlay = self.tenant.overlays[cap.id]
        effective = apply_overlay(cap, overlay)
        self.log.emit(
            "overlay_applied",
            tenant=overlay.tenant,
            capability=cap.ref,
            reason=overlay.reason,
            base_hash=cap.content_hash(),
            effective_hash=effective.content_hash(),
        )
        return effective

    def _check_application(self, cap: Capability) -> None:
        """Before acting: is this the right product (fingerprint), and a version the
        capability was validated for? A mismatch stops the run instead of guessing."""
        self._check_fingerprint()
        if not self.profile.version_pattern:
            return
        m = re.search(self.profile.version_pattern, self.surface.text_excerpt(limit=50_000))
        if m is None:
            return
        self.app_version = m.group(1)
        self.log.emit("app_version", version=self.app_version, supported=cap.app.product_versions)
        if not version_in_range(self.app_version, cap.app.product_versions):
            raise RunStop(
                "failed",
                error=RunError(
                    category="UNKNOWN_STATE",
                    message=f"application version {self.app_version} is outside the range "
                    f"{cap.app.product_versions!r} this capability was validated for; "
                    "refusing to run (re-validate it, or add a tenant overlay)",
                ),
            )

    def _check_fingerprint(self) -> None:
        if not self.profile.fingerprint:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(
                self.surface.check(c, self.profile.targets, {}) for c in self.profile.fingerprint
            ):
                return
            self.surface.wait(self.poll_ms)
        raise RunStop(
            "failed",
            error=RunError(
                category="UNKNOWN_STATE",
                message=f"application does not match the {self.profile.product!r} profile "
                f"({self.profile.product_versions}); refusing to run",
                observed=self.redactor.text(self.surface.text_excerpt()),
            ),
        )

    # ================================================================== helpers

    def _raise_outcome(self, state: _Run, step: Step, code: str) -> None:
        spec = state.cap.outcomes[code]
        raise RunStop(
            "business_outcome",
            outcome=OutcomeInfo(
                code=code,
                description=spec.description,
                retryable=spec.retryable,
                step_id=step.id,
                source="capability",
            ),
        )

    def _check(self, cond: Condition, state: _Run) -> bool:
        if isinstance(cond, OutputPresent):
            return state.outputs.get(cond.name) not in (None, "", {})
        if isinstance(cond, AllOf):
            return all(self._check(c, state) for c in cond.conditions)
        if isinstance(cond, AnyOf):
            return any(self._check(c, state) for c in cond.conditions)
        return self.surface.check(cond, state.cap.targets, state.inputs)

    def _sensitive(self, state: _Run) -> list[Resolved]:
        out: list[Resolved] = []
        for target in state.cap.targets.values():
            if target.sensitive:
                res = self.surface.resolve(target, state.inputs)
                if isinstance(res, Resolved):
                    out.append(res)
        return out

    def _deadline(self, step: Step) -> float:
        return time.monotonic() + step.timeout_ms / 1000

    def _error(
        self,
        state: _Run,
        step: Step | None,
        category: FailureCategory,
        message: str,
        *,
        expected: str | None = None,
        observed: str | None = None,
    ) -> RunError:
        shot = self.log.screenshot(
            self.surface, f"{step.id if step else 'run'}-failure", mask=self._sensitive(state)
        )
        return RunError(
            category=category,
            message=self.redactor.text(message),
            step_id=step.id if step else None,
            expected=expected,
            observed=self.redactor.text(observed) if observed else None,
            evidence=[s for s in [shot] if s],
        )

    def _fail_or_escalate(
        self,
        state: _Run,
        step: Step,
        category: FailureCategory,
        message: str,
        *,
        expected: str | None,
        observed: str | None,
    ) -> None:
        """Hard failure; or, with escalation enabled, hand the live session to a human.
        Returns only if the human resolved it with 'resume'."""
        error = self._error(state, step, category, message, expected=expected, observed=observed)
        if not self.escalate:
            raise RunStop("failed", error=error)
        request = AssistRequest(
            id=new_request_id("hlp"),
            run_id=self.log.run_id,
            subject=state.cap.ref,
            step=step.id,
            reason="unknown_state" if category == "UNKNOWN_STATE" else "failure",
            message=error.message,
            url=self.surface.current_url(),
            screenshot=error.evidence[0] if error.evidence else None,
        )
        state.interventions.append(request.id)
        self.log.emit("assist_requested", request=request.__dict__, error=error.model_dump())
        response = self.human.assist(request)
        self.log.emit(
            "assist_resolved",
            request_id=request.id,
            resolution=response.resolution,
            by=response.by,
            note=response.note,
            human_actions=response.human_actions,
        )
        if response.resolution == "abort":
            raise RunStop(
                "escalated",
                error=error.model_copy(update={"message": f"{error.message} (operator aborted)"}),
            )
        self.surface.settle()
        if response.resolution == "skip_step":
            raise _SkipStep


def _describe(cond: Condition) -> str:
    return cond.model_dump_json(exclude_defaults=True)
