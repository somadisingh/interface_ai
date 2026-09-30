"""App profile: knowledge about a vendor product shared by every capability that targets it.

Runtime conditions such as a maintenance interstitial, an expired session or the generic
error page are properties of the *application*, not of one flow. Defining them once per
product (rather than per capability) means:

* every capability gets the same recovery behaviour for free,
* a wording change is one edit, not N,
* a tenant running a differently-configured instance can override just the states that
  differ (see REPORT.md, "Heterogeneity & multi-tenant").

Serialized as YAML under ``apps/<product>/profile.yaml``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from cua.schema import templates
from cua.schema.actions import Action, action_strings, action_target
from cua.schema.common import SCHEMA_VERSION, DottedId, Identifier, Model, OutcomeCode
from cua.schema.conditions import Condition, output_refs, target_refs
from cua.schema.locators import TargetSpec

StateKind = Literal["interstitial", "session_expired", "business_outcome", "failure"]

FailureCategory = Literal[
    "INVALID_INPUT",  # caller-supplied inputs violate the capability contract
    "POLICY_VIOLATION",  # an action was outside the allowlist
    "LOCATOR_NOT_FOUND",  # no locator strategy matched the target
    "LOCATOR_AMBIGUOUS",  # strategies matched, but never exactly one element
    "CHECKPOINT_FAILED",  # the action ran, but the expected state never appeared
    "UNKNOWN_STATE",  # the UI is in a state nothing recognises
    "APP_ERROR",  # the application reported an error of its own
    "TIMEOUT",  # the page/app did not respond in time
    "SESSION_UNRECOVERABLE",  # session expired and could not be safely re-established
    "RECOVERY_EXHAUSTED",  # a known interstitial kept recurring past its limit
    "APPROVAL_DENIED",  # a human rejected an irreversible step
    "HUMAN_ABORTED",  # a human aborted the run during a handoff
    "CONFIGURATION_ERROR",  # the runtime is misconfigured (e.g. a required secret is unset)
]


class Handler(Model):
    """How to get past a recoverable state.

    ``then`` says how to continue afterwards:

    * ``resume`` — continue the current step: re-check its checkpoint, and perform its
      action again **only if the action never completed** (e.g. an overlay intercepted the
      click). An action that did complete is never repeated, so a recovery can't cause a
      double submission.
    * ``restart_capability`` — start the flow over (e.g. after re-signing on, the page
      context is gone). Only allowed while no irreversible step has executed in this run;
      otherwise the run stops with ``SESSION_UNRECOVERABLE``.
    """

    actions: list[Action] = Field(default_factory=list)
    run_capability: DottedId | None = None
    then: Literal["resume", "restart_capability"]

    @model_validator(mode="after")
    def _does_something(self) -> Handler:
        if not self.actions and not self.run_capability:
            raise ValueError("handler needs actions and/or run_capability")
        return self


class KnownState(Model):
    description: str
    kind: StateKind
    detect: Condition
    handler: Handler | None = None
    outcome: OutcomeCode | None = None
    retryable: bool = False
    failure_category: FailureCategory | None = None

    @model_validator(mode="after")
    def _kind_fields(self) -> KnownState:
        recoverable = self.kind in ("interstitial", "session_expired")
        if recoverable and self.handler is None:
            raise ValueError(f"{self.kind} state needs a handler")
        if not recoverable and self.handler is not None:
            raise ValueError(f"{self.kind} state must not have a handler")
        if (self.kind == "business_outcome") != (self.outcome is not None):
            raise ValueError("'outcome' is required for, and only allowed on, business_outcome")
        if (self.kind == "failure") != (self.failure_category is not None):
            raise ValueError("'failure_category' is required for, and only allowed on, failure")
        return self


class SessionConfig(Model):
    sign_on_capability: DottedId
    """Capability that establishes an authenticated session (credentials come from
    ``{{secrets.*}}``; no other capability ever handles them)."""


class AppProfile(Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    product: Identifier
    product_versions: str
    description: str
    fingerprint: list[Condition] = Field(default_factory=list)
    """Conditions that confirm we're looking at this product (checked before replay)."""
    session: SessionConfig
    targets: dict[Identifier, TargetSpec] = Field(default_factory=dict)
    states: dict[Identifier, KnownState] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cross_references(self) -> AppProfile:
        errors: list[str] = []
        conditions = [("fingerprint", c) for c in self.fingerprint]
        conditions += [(f"state {n}", s.detect) for n, s in self.states.items()]
        for where, cond in conditions:
            for t in target_refs(cond):
                if t not in self.targets:
                    errors.append(f"{where}: unknown target {t!r}")
            if output_refs(cond):
                errors.append(f"{where}: app profiles have no outputs")
        for name, state in self.states.items():
            if state.handler is None:
                continue
            for action in state.handler.actions:
                handler_target = action_target(action)
                if handler_target is not None and handler_target not in self.targets:
                    errors.append(f"state {name}: unknown target {handler_target!r}")
                for text in action_strings(action):
                    if templates.references(text):
                        errors.append(f"state {name}: handler actions cannot use templates")
        for tname, target in self.targets.items():
            if any(templates.references(s) for s in target.strings()):
                errors.append(f"target {tname}: app profile targets cannot use templates")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def business_outcomes(self) -> dict[str, KnownState]:
        found: dict[str, KnownState] = {}
        for state in self.states.values():
            if state.outcome is not None:
                found[state.outcome] = state
        return found
