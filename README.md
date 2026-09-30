# cua — Computer-Use Automation System

An LLM discovers how to complete a goal in a UI-only (no API) application. The run is compiled
into a typed, versioned **capability artifact**, and that artifact is then **replayed
deterministically** with no model in the loop. When the system can't proceed safely, it
escalates to a human, who takes over the same live session and hands control back.

> Work in progress. Setup, the demo path and the design write-up (`REPORT.md`) will be added
> as the components land.

## Development setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups
uv run playwright install chromium
cp .env.example .env        # fill in values; .env is git-ignored
uv run pytest
uv run cua --help
```
