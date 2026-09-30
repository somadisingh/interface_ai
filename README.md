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

## Target app: MockCore

MockCore is a deliberately hostile, fictional "legacy core banking" web app used as the
automation target. It has a frameset UI, table layouts, no element ids or test ids, labels
not tied to their inputs, `<span onclick>` buttons and `javascript:` links. All of its data
is fake.

```bash
uv run cua mockcore                      # http://127.0.0.1:8765  (user: operator / mockcore-demo)
uv run cua mockcore --variant b          # second "tenant": same product, different labels/branding
uv run cua mockcore --faults maintenance,modal,slow=2000
```

Flows: sign on → member search → member detail (balances) → open sub-account → review →
confirm (irreversible, single-use transaction token).

Injectable runtime conditions (`--faults`, or at runtime via `POST /__admin/faults {"spec": ...}`):

| Fault | Effect |
|---|---|
| `maintenance` | "Scheduled maintenance" interstitial before main pages, once per session |
| `modal` | "Password expires" overlay that blocks clicks, once per session |
| `slow=MS` | Delays every main-frame page |
| `session_timeout=N` | Session expires after N page loads; the main frame falls back to sign-on |
| `error500=/prefix` | Application error page (HTTP 500) for matching paths |
| `permission_denied` | Member detail returns ACCESS DENIED |

Business outcomes come from the data itself: member `99999` doesn't exist, member `55555`
is restricted, and malformed ids or amounts trigger validation errors.
