"""Command-line entry point.

Single-process design: every mode (discovery, replay, operator surface, target app)
is a subcommand of one CLI. Subcommands are added as each component lands.
"""

from __future__ import annotations

from pathlib import Path

import typer

from cua import __version__

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
            if isinstance(data, dict) and "states" in data and "session" in data:
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
