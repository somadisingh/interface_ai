# cua — Computer-Use Automation System

A backend integration layer that lets AI agents operate **legacy back-office applications
that have no API**, the long tail of bank and credit-union software where the only way in is
the UI a human operator uses.

> **The model discovers. The artifact becomes a reusable capability. Deterministic replay is
> how the AI agent invokes it in production.**

1. **Discover:** given a goal ("look up member 12345 and read their savings balance") and a
   target app, an LLM drives the live UI in an observe → decide → act loop until the goal is met.
2. **Record:** the successful run is compiled into a typed, versioned **capability
   artifact**: steps, robust element targeting, typed inputs and outputs, and checkpoints.
   It is decoupled from the model transcript.
3. **Replay:** the artifact is re-run **without the LLM**, using the caller's input parameters.
   Each result is classified as *success*, a *business outcome* (e.g. member not found),
   *recoverable* (handled and continued), or a *hard failure* (with step, expected vs. observed,
   and evidence).
4. **Escalate:** when the system can't proceed safely (stuck, unknown state, or an
   irreversible step), it pauses, raises an intervention request, and lets a human take over
   **the same live session**, then hands control back.
5. **Guardrails throughout:** an explicit allowlist, conservative handling of irreversible
   actions, and redaction so that no secrets or raw PII reach artifacts or logs.

## Status

This project is being built in vertical slices. Checked items are implemented and tested.

- [x] Project scaffold, CLI, lint/type-check/test tooling, CI
- [x] **MockCore**, a hostile legacy target app with injectable runtime faults and a second-tenant variant
- [x] Capability artifact schema, shared app profile, and replay result contract
- [x] Surface abstraction and Playwright web implementation (multi-frame, verified locator generation, drift detection, masked screenshots)
- [x] Guardrails: browser-enforced allowlist, risk classification with tiered approval, redaction
- [ ] LLM discovery loop and recorder
- [ ] Compiler (trace → artifact)
- [ ] Deterministic replay engine with error taxonomy and recovery
- [ ] Human-in-the-loop handoff (control lease, intervention queue, operator page)
- [ ] Evidence (`/evidence/`) and design write-up (`REPORT.md`)

The demo commands (discover a goal, then replay the resulting artifact) will be added here
as those components land.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/somadisingh/interface_ai.git
cd interface_ai
uv sync --all-groups                 # install dependencies (pinned in uv.lock)
uv run playwright install chromium   # browser used by discovery, replay and tests
cp .env.example .env                 # fill in values; .env is git-ignored
```

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | Discovery runs only. Replay never calls the model. |
| `CUA_MODEL` | Model used for discovery. |
| `MOCKCORE_USERNAME` / `MOCKCORE_PASSWORD` | Sign-on for the local MockCore app (fake credentials). |

Run the checks:

```bash
uv run pytest          # tests, including headless-browser runs against MockCore
uv run ruff check .    # lint
uv run mypy            # type-check
uv run cua --help      # CLI
```

## Target app: MockCore

The brief deliberately provides no access to a real bank system. MockCore is a **fictional**
credit-union back office built as a stand-in for one. It is intentionally hostile, like
the legacy surfaces it represents:

- server-rendered HTML 4 in a **frameset** (navigation frame + main frame)
- **nested layout tables**, **no element ids or test ids**
- labels that are **not associated with their inputs**
- `<span onclick>` "buttons" (not exposed as buttons in the accessibility tree) and `javascript:` links

All data is fake (fictional names, never-issued 900-series SSNs).

```bash
uv run cua mockcore                      # http://127.0.0.1:8765  (operator / mockcore-demo)
uv run cua mockcore --variant b          # second "tenant": same product, different labels/branding
uv run cua mockcore --faults maintenance,modal,slow=2000
```

**Flows:** sign on → member search → member detail (balances) → open sub-account → review →
confirm. Confirm is irreversible and uses a single-use transaction token.

**Business outcomes** come from the data itself: member `99999` doesn't exist, member
`55555` is restricted (access denied), and malformed ids or amounts produce validation errors.

**Injectable runtime conditions** (`--faults`, or at runtime via
`POST /__admin/faults {"spec": "..."}`):

| Fault | Effect |
|---|---|
| `maintenance` | "Scheduled maintenance" interstitial before main pages, once per session |
| `modal` | "Password expires" overlay that blocks clicks, once per session |
| `slow=MS` | Delays every main-frame page |
| `session_timeout=N` | Session expires (once) after N page loads; the main frame falls back to sign-on |
| `error500=/prefix` | Application error page (HTTP 500) for matching paths |
| `permission_denied` | Member detail returns ACCESS DENIED |

The `/__admin/*` endpoints are test hooks, not part of the app surface. The automation's
allowlist never permits them.

## Artifact formats

| Artifact | Where | What it is |
|---|---|---|
| Capability | `capabilities/<product>/<id>/<version>.yaml` | One reusable flow: typed inputs/outputs, business outcomes, targets (ordered locator bundles), steps with checkpoints, success condition, review status, provenance |
| App profile | `apps/<product>/profile.yaml` | Product-wide knowledge shared by all its capabilities: fingerprint, sign-on capability, known runtime states (interstitials, session expiry, access denied, error page) and how to handle each |
| Run result | returned by replay | `success` / `business_outcome` / `failed` / `escalated`, with outputs, outcome code, or a structured error (step, expected, observed, evidence) |

JSON Schemas for all three are in [`schema/`](schema/) (regenerate with `uv run cua schema-export`).
Validate artifacts, including every cross-reference, with:

```bash
uv run cua validate apps/mockcore/profile.yaml capabilities/mockcore/session.sign_on/1.0.0.yaml
```

YAML is loaded strictly: values like `01234` or `no` stay strings instead of being silently
converted, and duplicate keys are rejected.

## Guardrails

[`policy.yaml`](policy.yaml) configures what the automation may touch and do:

- **Allowlist:** permitted origins and paths, plus denied paths (deny always wins). This is
  enforced by the browser for **every request**, so it can't be bypassed by agent logic.
  MockCore's `/__admin` test hooks are denied.
- **Risk:** a click whose label contains words like *confirm, submit, transfer* is
  irreversible. Effective risk is the higher of what the capability declares and what the
  label implies, so a capability can never downgrade a Confirm button to "safe".
- **Tiered approval:** during discovery, irreversible actions always need a human. On
  replay they need a human unless the capability is `approved` **and** a reviewer marked
  that step `auto_approve_on_replay`.
- **Redaction:** secrets are resolved from the environment at the moment of use and
  replaced with `[SECRET]` everywhere. Declared PII values are masked (`***45`), and SSNs,
  Luhn-valid card numbers and e-mail addresses are caught by pattern. Screenshots have
  sensitive elements and matching text painted over before the image is written.

## Repository layout

```
src/cua/            the automation system
  schema/           typed models: capability artifact, run result, events, policy
  surface/          observe / act / resolve abstraction + Playwright web implementation
  agent/            LLM discovery loop
  recorder/         raw trace capture during discovery
  compiler/         trace → capability artifact
  replay/           deterministic replay, locator resolution, state classification, recovery
  policy/           allowlist, risk classification, redaction
  handoff/          control lease, intervention queue, operator surface
  evidence/         structured logs, screenshots, traces, run reports
  cli.py            single CLI entry point (`cua ...`)
src/mockcore/       the fictional legacy target app
apps/               app profiles (one per vendor product)
capabilities/       capability artifacts
policy.yaml         guardrail policy (allowlist, action types, risk keywords, limits)
schema/             generated JSON Schemas for the artifact formats
tests/              unit, HTTP-level and browser tests
```

## Deliverables (per the brief)

| Path | Contents |
|---|---|
| `/README.md` | This file: setup, configuration and the demo path |
| `/REPORT.md` | Design write-up: Architecture · Artifact schema · Determinism & error handling · Heterogeneity & multi-tenant · Escalation & handoff · Safety · Cuts |
| `/evidence/` | Example capability artifact, plus logs from a real discovery run and from replay runs (including error cases) |
