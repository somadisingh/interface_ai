"""Guardrail policy: what the automation may touch and do, and how risk is classified.

The policy is configuration (``policy.yaml``), separate from capabilities: the same
capability can run under a stricter policy at a more conservative tenant.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from cua.schema.common import SCHEMA_VERSION, Model, Risk
from cua.schema.yaml_io import load_yaml

ActionType = Literal["click", "fill", "select", "check", "press", "navigate", "extract"]
Mode = Literal["discovery", "replay"]

_RISK_ORDER: dict[Risk, int] = {"safe": 0, "reversible_write": 1, "irreversible": 2}
_COMMITTING_ACTIONS = frozenset({"click", "press"})
"""Only these can commit a transaction; typing into or selecting in a field never does."""


class RiskRules(Model):
    irreversible_keywords: list[str]
    """If the control's visible label contains one of these words, a click on it is
    irreversible (e.g. confirm, submit, transfer)."""
    reversible_write_keywords: list[str] = Field(default_factory=list)


class Limits(Model):
    max_discovery_steps: int = Field(default=40, gt=0, le=200)
    max_run_seconds: int = Field(default=300, gt=0, le=3600)


class Policy(Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    name: str
    allowed_origins: list[str] = Field(min_length=1)
    """``scheme://host:port`` glob patterns, e.g. ``http://127.0.0.1:*``."""
    allowed_paths: list[str] = Field(min_length=1)
    """Path globs (``*`` also matches ``/``). A request must match one of these..."""
    denied_paths: list[str] = Field(default_factory=list)
    """...and none of these. Deny always wins."""
    allowed_actions: list[ActionType]
    risk: RiskRules
    limits: Limits = Field(default_factory=Limits)

    @classmethod
    def load(cls, path: Path | str) -> Policy:
        return cls.model_validate(load_yaml(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Decision:
    verdict: Literal["allow", "deny", "require_approval"]
    reason: str
    risk: Risk = "safe"

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme, 0)
    return f"{parts.scheme}://{parts.hostname}:{port}", parts.path or "/"


class PolicyEngine:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._irreversible = self._compile(policy.risk.irreversible_keywords)
        self._reversible = self._compile(policy.risk.reversible_write_keywords)

    @staticmethod
    def _compile(words: list[str]) -> re.Pattern[str] | None:
        if not words:
            return None
        alternatives = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)

    # ------------------------------------------------------------------ where

    def check_url(self, url: str) -> Decision:
        """Enforced for every browser request (see ``WebSurface.install_request_guard``)."""
        if url.startswith(("about:", "data:")):
            return Decision("allow", "internal browser URL")
        origin, path = _origin(url)
        if not any(fnmatch.fnmatchcase(origin, p) for p in self.policy.allowed_origins):
            return Decision("deny", f"origin {origin} is not in the allowlist")
        for pattern in self.policy.denied_paths:
            if fnmatch.fnmatchcase(path, pattern):
                return Decision("deny", f"path {path} matches denied pattern {pattern!r}")
        if not any(fnmatch.fnmatchcase(path, p) for p in self.policy.allowed_paths):
            return Decision("deny", f"path {path} is not in the allowlist")
        return Decision("allow", "allowlisted")

    def request_guard(self, url: str) -> str | None:
        decision = self.check_url(url)
        return None if decision.allowed else decision.reason

    # ------------------------------------------------------------------ what

    def classify(self, action: ActionType, label: str, declared: Risk | None = None) -> Risk:
        """Effective risk = the higher of what the artifact declares and what the control's
        label implies. A capability can never *downgrade* a Confirm button to safe."""
        inferred: Risk = "safe"
        if action in _COMMITTING_ACTIONS:
            if self._irreversible is not None and self._irreversible.search(label):
                inferred = "irreversible"
            elif self._reversible is not None and self._reversible.search(label):
                inferred = "reversible_write"
        if declared is None:
            return inferred
        return declared if _RISK_ORDER[declared] >= _RISK_ORDER[inferred] else inferred

    def check_action(
        self,
        action: ActionType,
        *,
        label: str,
        mode: Mode,
        declared_risk: Risk | None = None,
        capability_approved: bool = False,
        auto_approve: bool = False,
        reviewer: str | None = None,
    ) -> Decision:
        """Tiered policy for risky actions:

        * discovery: irreversible actions always require a human's approval;
        * replay: irreversible actions require approval unless the capability is approved
          AND a reviewer marked this step ``auto_approve_on_replay``.
        """
        if action not in self.policy.allowed_actions:
            return Decision("deny", f"action type {action!r} is not allowed by policy")
        risk = self.classify(action, label, declared_risk)
        if risk != "irreversible":
            return Decision("allow", f"{risk} action", risk)
        if mode == "replay" and capability_approved and auto_approve:
            return Decision(
                "allow",
                f"irreversible step pre-approved for unattended replay by {reviewer or 'reviewer'}",
                risk,
            )
        why = (
            "irreversible action during discovery"
            if mode == "discovery"
            else "irreversible step without reviewer pre-approval"
        )
        return Decision("require_approval", f"{why}: {label!r}", risk)
