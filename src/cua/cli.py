"""Command-line entry point.

Single-process design: every mode (discovery, replay, operator surface, target app)
is a subcommand of one CLI. Subcommands are added as each component lands.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer

from cua import __version__

if TYPE_CHECKING:
    from cua.handoff.channel import HumanChannel

app = typer.Typer(
    name="cua",
    help="Computer-use automation: discover a flow with an LLM, save it as a capability, "
    "replay it deterministically.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """cua command-line interface."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def validate(
    paths: list[Path] = typer.Argument(..., help="Capability or app-profile YAML files."),
) -> None:
    """Validate artifacts against the schema (and all cross-references)."""
    from pydantic import ValidationError

    from cua.schema import load_app_profile, load_capability
    from cua.schema.yaml_io import ArtifactLoadError, load_yaml

    failed = False
    for path in paths:
        try:
            data = load_yaml(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "tenant" in data and "capability" in data:
                from cua.schema.tenancy import CapabilityOverlay

                overlay = CapabilityOverlay.model_validate(data)
                typer.echo(f"OK   {path}  overlay for {overlay.capability} ({overlay.tenant})")
            elif isinstance(data, dict) and "tenant" in data and "base_url" in data:
                from cua.schema.tenancy import TenantConfig

                cfg = TenantConfig.model_validate(data)
                typer.echo(f"OK   {path}  tenant {cfg.tenant} ({cfg.name})")
            elif isinstance(data, dict) and "states" in data and "session" in data:
                profile = load_app_profile(path)
                typer.echo(
                    f"OK   {path}  app profile '{profile.product}' ({len(profile.states)} states)"
                )
            else:
                cap = load_capability(path)
                stored = cap.provenance.content_hash
                note = "" if stored in (None, cap.content_hash()) else "  (stale content_hash!)"
                typer.echo(
                    f"OK   {path}  {cap.ref}  [{cap.review.status}]  "
                    f"{cap.content_hash()[:19]}{note}"
                )
        except (ValidationError, ArtifactLoadError, OSError) as exc:
            failed = True
            typer.echo(f"FAIL {path}\n{exc}", err=True)
    raise typer.Exit(code=1 if failed else 0)


@app.command("schema-export")
def schema_export(
    out: Path = typer.Option(Path("schema"), help="Directory for the JSON Schema files."),
) -> None:
    """Export JSON Schemas for the capability, app profile and run result formats."""
    import json

    from cua.schema import AppProfile, Capability, RunResult

    out.mkdir(parents=True, exist_ok=True)
    for name, model in (
        ("capability", Capability),
        ("app_profile", AppProfile),
        ("run_result", RunResult),
    ):
        target = out / f"{name}.schema.json"
        target.write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")
        typer.echo(f"wrote {target}")


DEFAULT_BASE_URL = "http://127.0.0.1:8765"


def _pairs(items: list[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"{what} must look like name=value, got {item!r}")
        out[key.strip()] = value
    return out


def _human_channel(kind: str) -> HumanChannel | Literal["operator"]:
    from cua.handoff.channel import ScriptedChannel, TerminalChannel

    if kind == "operator":
        return "operator"  # local operator page + visible browser window
    if kind == "terminal":
        return TerminalChannel()
    if kind == "none":
        return ScriptedChannel()  # unattended: approvals denied, assists aborted
    raise typer.BadParameter("--human must be 'operator', 'terminal' or 'none'")


def _announce(url: str | None) -> None:
    if url:
        typer.echo(
            f"operator page: {url}  (interventions appear there; the browser window "
            "is the live session)"
        )


@app.command()
def discover(
    goal: str = typer.Option(..., help="Goal in natural language."),
    capability: str = typer.Option(..., help="Capability id, e.g. member.read_savings_balance"),
    param: list[str] = typer.Option([], help="Goal parameter name=value (repeatable)."),
    output: list[str] = typer.Option(
        [], help="Output to extract, name:type[:description] (repeatable)."
    ),
    public_param: list[str] = typer.Option(
        [], help="Parameter names that are NOT personal data (default: all are treated as PII)."
    ),
    product: str = typer.Option("mockcore", help="App product (selects apps/<product>/)."),
    base_url: str = typer.Option(DEFAULT_BASE_URL, help="Base URL of the app instance."),
    headed: bool = typer.Option(False, help="Show the browser window."),
    model: str | None = typer.Option(None, help="Model id (default: $CUA_MODEL)."),
    provider: str | None = typer.Option(
        None, help="LLM provider: anthropic | gemini (default: $CUA_LLM_PROVIDER, else inferred)."
    ),
    max_steps: int | None = typer.Option(None, help="Step budget (default: policy.yaml)."),
    human: str = typer.Option(
        "operator", help="Human channel: operator (page + visible browser) | terminal | none."
    ),
    operator_port: int = typer.Option(8766, help="Port for the local operator page."),
    merge_into: Path | None = typer.Option(
        None, help="Existing capability YAML: merge this run's business outcome into it."
    ),
) -> None:
    """Run an LLM-driven discovery on the live app and compile the result into a capability."""
    from dotenv import load_dotenv

    from cua.agent.discovery import DiscoveryAgent, DiscoverySpec
    from cua.agent.llm import make_llm
    from cua.compiler.compile import compile_trace, merge_outcome, next_version
    from cua.recorder.trace import ParamSpec
    from cua.runtime import open_runtime
    from cua.schema import load_capability, save_capability
    from cua.schema.capability import OutputSpec

    load_dotenv()
    params = _pairs(param, "--param")
    outputs: dict[str, OutputSpec] = {}
    for item in output:
        name, _, rest = item.partition(":")
        otype, _, desc = rest.partition(":")
        outputs[name] = OutputSpec.model_validate(
            {
                "type": otype or "string",
                "description": desc or name.replace("_", " "),
                "sensitivity": "pii",
            }
        )
    types = {k: ParamSpec(sensitivity="none" if k in public_param else "pii") for k in params}
    try:
        llm = make_llm(provider, model)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    channel = _human_channel(human)
    with open_runtime(
        base_url,
        human=channel,
        product=product,
        mode="discovery",
        headless=not (headed or channel == "operator"),
        operator_port=operator_port,
    ) as rt:
        _announce(rt.operator_url)
        limits = rt.policy.policy.limits
        agent = DiscoveryAgent(
            rt.surface,
            llm,
            engine=rt.engine,
            policy=rt.policy,
            human=rt.human,
            log=rt.log,
            model_redactor=rt.redactor.model_view(),
            max_steps=max_steps or limits.max_discovery_steps,
            max_seconds=limits.max_run_seconds,
            app_description=rt.profile.description,
        )
        trace = agent.run(
            DiscoverySpec(
                capability_id=capability,
                product=product,
                goal=goal,
                params=params,
                outputs=outputs,
                requires=[rt.profile.session.sign_on_capability],
                param_types=types,
            )
        )
        if trace.status not in ("succeeded", "outcome"):
            rt.keep_trace = True
        run_dir, registry, profile = rt.log.run_dir, rt.registry, rt.profile

    typer.echo(f"discovery {trace.status}: {trace.summary}")
    typer.echo(f"  steps: {len(trace.steps)}  tokens: {trace.usage}  evidence: {run_dir}")
    target = None
    if merge_into is not None and trace.status == "outcome":
        cap = merge_outcome(load_capability(merge_into), trace)
        target = registry.root / product / cap.id / f"{cap.version}.yaml"
    elif trace.status == "succeeded":
        cap = compile_trace(
            trace,
            version=next_version(registry, product, capability),
            product_versions=profile.product_versions,
        )
        target = registry.root / product / cap.id / f"{cap.version}.yaml"
    if target is not None:
        save_capability(cap, target)
        save_capability(cap, run_dir / "capability.yaml")
        typer.echo(f"  capability: {target}  (draft — review with `cua review {target}`)")
    raise typer.Exit(code=0 if target is not None else 1)


@app.command()
def replay(
    capability: str = typer.Argument(..., help="Capability YAML path, or product/id[@version]."),
    input: list[str] = typer.Option([], "--input", help="Input name=value (repeatable)."),
    tenant: str | None = typer.Option(
        None, help="Tenant id (tenants/<id>/): its instance URL and capability overlays."
    ),
    base_url: str | None = typer.Option(
        None, help=f"Base URL of the app instance (default: tenant's, else {DEFAULT_BASE_URL})."
    ),
    headed: bool = typer.Option(False, help="Show the browser window."),
    escalate: bool = typer.Option(False, help="Hand unrecoverable states to a human."),
    human: str = typer.Option(
        "none", help="Human channel: operator (page + visible browser) | terminal | none."
    ),
    operator_port: int = typer.Option(8766, help="Port for the local operator page."),
) -> None:
    """Replay a capability deterministically (no model). Prints the RunResult as JSON.

    Exit codes: 0 success, 3 business outcome, 2 escalated, 1 failed."""
    from dotenv import load_dotenv

    from cua.replay.support import CapabilityRegistry
    from cua.runtime import open_runtime
    from cua.schema import load_capability

    load_dotenv()
    path = Path(capability)
    cap = (
        load_capability(path)
        if path.exists()
        else CapabilityRegistry("capabilities").resolve_ref(capability)
    )
    product = cap.app.product
    if base_url is None:
        from cua.schema.tenancy import Tenant

        base_url = Tenant.load(Path("."), tenant).config.base_url if tenant else DEFAULT_BASE_URL
    channel = _human_channel(human)
    with open_runtime(
        base_url,
        human=channel,
        product=product,
        tenant=tenant,
        headless=not (headed or channel == "operator"),
        escalate=escalate,
        operator_port=operator_port,
    ) as rt:
        _announce(rt.operator_url)
        result = rt.engine.run(cap, _pairs(input, "--input"))
        if result.status in ("failed", "escalated"):
            rt.keep_trace = True
    typer.echo(result.model_dump_json(indent=2, exclude_none=True))
    codes = {"success": 0, "business_outcome": 3, "escalated": 2, "failed": 1}
    raise typer.Exit(code=codes[result.status])


@app.command()
def review(
    path: Path = typer.Argument(..., help="Capability YAML to review."),
    approve: bool = typer.Option(False, help="Mark the capability approved."),
    by: str | None = typer.Option(None, help="Reviewer name (required with --approve)."),
    auto_approve: list[str] = typer.Option(
        [], help="Irreversible step id allowed to run unattended once approved (repeatable)."
    ),
    notes: str | None = typer.Option(None, help="Review notes."),
) -> None:
    """Show a capability's contract and steps; optionally approve it."""
    from datetime import UTC, datetime

    from cua.schema import load_capability, save_capability

    cap = load_capability(path)
    typer.echo(f"{cap.ref}  [{cap.review.status}]  {cap.content_hash()[:19]}")
    typer.echo(f"  {cap.summary}")
    typer.echo(f"  requires: {', '.join(cap.requires) or '-'}")
    typer.echo("  inputs:   " + (", ".join(f"{k}:{v.type}" for k, v in cap.inputs.items()) or "-"))
    typer.echo("  outputs:  " + (", ".join(f"{k}:{v.type}" for k, v in cap.outputs.items()) or "-"))
    typer.echo("  outcomes: " + (", ".join(cap.outcomes) or "-"))
    for i, step in enumerate(cap.steps, 1):
        target = getattr(step.action, "target", None)
        strategies = ""
        if target:
            strategies = " > ".join(loc.strategy for loc in cap.targets[target].locators)
        flag = "  [AUTO-APPROVED]" if step.auto_approve_on_replay else ""
        typer.echo(
            f"  {i:>2}. {step.id:<32} {step.action.type:<8} {step.risk:<16} {strategies}{flag}"
        )
    if not approve:
        return
    if not by:
        raise typer.BadParameter("--by is required with --approve")
    data = cap.model_dump(mode="json")
    for step in data["steps"]:
        if step["id"] in auto_approve:
            if step["risk"] != "irreversible":
                raise typer.BadParameter(f"step {step['id']!r} is not irreversible")
            step["auto_approve_on_replay"] = True
    data["review"] = {
        "status": "approved",
        "reviewed_by": by,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "notes": notes,
    }
    from cua.schema import Capability

    save_capability(Capability.model_validate(data), path)
    typer.echo(f"approved by {by}; saved {path}")


@app.command()
def report(run_dir: Path = typer.Argument(..., help="A run folder under runs/.")) -> None:
    """(Re)generate the human-readable run_report.md from a run's event log."""
    from cua.evidence.report import build_report

    typer.echo(f"wrote {build_report(run_dir)}")


@app.command()
def mockcore(
    port: int = typer.Option(8765, help="Port to listen on."),
    host: str = typer.Option("127.0.0.1", help="Interface to bind (local only by default)."),
    variant: str = typer.Option("a", help="Tenant variant: 'a' (base) or 'b' (second tenant)."),
    faults: str = typer.Option(
        "",
        help="Comma-separated faults, e.g. 'maintenance,modal,slow=2000,session_timeout=5,"
        "error500=/members,permission_denied'. Also switchable at runtime via /__admin/faults.",
    ),
) -> None:
    """Run the MockCore target app (a fictional legacy core-banking UI)."""
    import os

    import uvicorn

    os.environ["MOCKCORE_VARIANT"] = variant
    os.environ["MOCKCORE_FAULTS"] = faults
    from mockcore import create_app

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
