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


if __name__ == "__main__":
    app()
