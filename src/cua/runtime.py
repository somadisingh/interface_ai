"""Wiring: builds everything a run needs (browser surface with the policy enforced, control
lease, redactor, evidence log, replay engine, human channel) from repo configuration. Used by
the CLI and by tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cua.evidence.log import EventLog
from cua.evidence.report import build_report
from cua.handoff.channel import HumanChannel
from cua.handoff.control import SessionControl, Transition
from cua.handoff.operator import OperatorChannel
from cua.handoff.queue import InterventionQueue
from cua.handoff.server import OperatorServer, create_operator_app
from cua.policy import Policy, PolicyEngine, Redactor, SecretStore
from cua.replay.engine import ReplayEngine, new_run_id
from cua.replay.support import CapabilityRegistry
from cua.schema import AppProfile, load_app_profile
from cua.schema.tenancy import Tenant, apply_profile_overrides
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
    control: SessionControl
    operator_url: str | None = None
    keep_trace: bool = False
    """Set to True (e.g. on failure) to save the Playwright trace into the run folder."""


@contextmanager
def open_runtime(
    base_url: str,
    *,
    human: HumanChannel | Literal["operator"],
    product: str = "mockcore",
    mode: str = "replay",
    root: Path = Path("."),
    runs_dir: Path = Path("runs"),
    run_id: str | None = None,
    headless: bool = True,
    escalate: bool = False,
    secrets_source: Mapping[str, str] | None = None,
    operator_port: int = 8766,
    simulate_operator: Callable[[WebSurface], None] | None = None,
    tenant: str | None = None,
) -> Iterator[Runtime]:
    run_id = run_id or new_run_id("disc" if mode == "discovery" else "run")
    redactor = Redactor()
    log = EventLog(runs_dir / run_id, run_id, redactor)
    policy = PolicyEngine(Policy.load(root / "policy.yaml"))
    profile = load_app_profile(root / "apps" / product / "profile.yaml")
    tenant_cfg = Tenant.load(root, tenant) if tenant else None
    if tenant_cfg is not None:
        if tenant_cfg.config.product != product:
            raise ValueError(
                f"tenant {tenant!r} runs {tenant_cfg.config.product!r}, not {product!r}"
            )
        profile = apply_profile_overrides(profile, tenant_cfg.config.profile_overrides)
    registry = CapabilityRegistry(root / "capabilities")

    def on_transfer(t: Transition) -> None:
        log.emit(
            "control_transferred",
            frm=t.frm,
            to=t.to,
            by=t.by,
            reason=t.reason,
            request_id=t.request_id,
        )

    control = SessionControl(on_change=on_transfer)
    server: OperatorServer | None = None
    with WebSurface.launch(
        base_url, headless=headless, request_guard=policy.request_guard, trace=True
    ) as surface:
        surface.control = control
        channel: HumanChannel
        if human == "operator":
            queue = InterventionQueue()
            channel = OperatorChannel(
                surface, control, queue, log, simulate_operator=simulate_operator
            )
            server = OperatorServer(
                create_operator_app(control, queue, log.run_dir), port=operator_port
            )
            server.start()
        else:
            channel = human
        engine = ReplayEngine(
            surface,
            profile=profile,
            policy=policy,
            registry=registry,
            human=channel,
            log=log,
            redactor=redactor,
            secrets=SecretStore(redactor, secrets_source),
            escalate=escalate,
            mode="discovery" if mode == "discovery" else "replay",
            tenant=tenant_cfg,
        )
        runtime = Runtime(
            surface,
            engine,
            log,
            redactor,
            policy,
            profile,
            registry,
            channel,
            control,
            server.url if server else None,
        )
        try:
            yield runtime
        finally:
            surface.stop_trace(log.run_dir / "trace.zip" if runtime.keep_trace else None)
            if server is not None:
                server.stop()
            try:
                build_report(log.run_dir)
            except Exception as exc:  # a report must never mask the run's own outcome
                log.emit("report_failed", error=str(exc))
