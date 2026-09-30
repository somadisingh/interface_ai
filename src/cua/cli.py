"""Command-line entry point.

Single-process design: every mode (discovery, replay, operator surface, target app)
is a subcommand of one CLI. Subcommands are added as each component lands.
"""

from __future__ import annotations

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
