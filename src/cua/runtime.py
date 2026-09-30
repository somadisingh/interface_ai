"""Wiring: builds everything a run needs (browser surface with the policy enforced, redactor,
evidence log, replay engine) from repo configuration. Used by the CLI and by tests."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cua.evidence.log import EventLog
from cua.handoff.channel import HumanChannel
from cua.policy import Policy, PolicyEngine, Redactor, SecretStore
from cua.replay.engine import ReplayEngine, new_run_id
from cua.replay.support import CapabilityRegistry
from cua.schema import AppProfile, load_app_profile
from cua.surface.web import WebSurface


@dataclass
class Runtime:
    surface: WebSurface
    engine: ReplayEngine
    log: EventLog
    redactor: Redactor
    policy: PolicyEngine
    profile: AppProfile
    registry: CapabilityRegistry
    human: HumanChannel
    keep_trace: bool = False
    """Set to True (e.g. on failure) to save the Playwright trace into the run folder."""


@contextmanager
def open_runtime(
    base_url: str,
    *,
    human: HumanChannel,
    product: str = "mockcore",
    mode: str = "replay",
    root: Path = Path("."),
    runs_dir: Path = Path("runs"),
    run_id: str | None = None,
    headless: bool = True,
    escalate: bool = False,
    secrets_source: Mapping[str, str] | None = None,
) -> Iterator[Runtime]:
    run_id = run_id or new_run_id("disc" if mode == "discovery" else "run")
    redactor = Redactor()
    log = EventLog(runs_dir / run_id, run_id, redactor)
    policy = PolicyEngine(Policy.load(root / "policy.yaml"))
    profile = load_app_profile(root / "apps" / product / "profile.yaml")
    registry = CapabilityRegistry(root / "capabilities")
    with WebSurface.launch(
        base_url, headless=headless, request_guard=policy.request_guard, trace=True
    ) as surface:
        engine = ReplayEngine(
            surface,
            profile=profile,
            policy=policy,
            registry=registry,
            human=human,
            log=log,
            redactor=redactor,
            secrets=SecretStore(redactor, secrets_source),
            escalate=escalate,
            mode="discovery" if mode == "discovery" else "replay",
        )
        runtime = Runtime(surface, engine, log, redactor, policy, profile, registry, human)
        try:
            yield runtime
        finally:
            surface.stop_trace(log.run_dir / "trace.zip" if runtime.keep_trace else None)
