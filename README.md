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
- [x] LLM discovery loop and recorder (Claude Sonnet 5.5 by default; scripted stand-in for offline tests)
- [x] Compiler (trace → artifact), including merging business outcomes learned from negative discovery runs
- [x] Deterministic replay engine with error taxonomy, recovery, drift detection and approvals
- [x] Human-in-the-loop handoff: control lease, intervention queue, local operator page, capture of the person's actions
- [x] Evidence per run: redacted event log, masked screenshots, Playwright trace on failure, generated `run_report.md`
- [x] Stretch: cross-tenant reuse (tenant overlays + product-version gate)
- [ ] Curated `/evidence/` (real discovery run + replay runs) and design write-up (`REPORT.md`)

## Demo path

Terminal 1: start the target app.

```bash
uv run cua mockcore
```

Terminal 2: discover a capability with the LLM, review it, then replay it without the LLM.

```bash
# 1. Discovery (needs ANTHROPIC_API_KEY in .env). Signs on with the service account from .env
#    first; the model never sees credentials. Writes capabilities/mockcore/<id>/<version>.yaml.
uv run cua discover \
  --capability member.read_savings_balance \
  --goal "Look up member 12345 and read their current savings balance" \
  --param member_id=12345 \
  --output "savings_balance:money:Current balance of the member's Share Savings account"

# 2. Teach it a business outcome with a negative discovery run (bad input).
uv run cua discover \
  --capability member.read_savings_balance \
  --goal "Look up member 99999 and read their current savings balance" \
  --param member_id=99999 --output savings_balance:money \
  --merge-into capabilities/mockcore/member.read_savings_balance/1.0.0.yaml

# 3. Review (and optionally approve) the capability.
uv run cua review capabilities/mockcore/member.read_savings_balance/1.1.0.yaml

# 4. Deterministic replay: no model in the loop. Prints the RunResult JSON.
uv run cua replay mockcore/member.read_savings_balance --input member_id=34567   # success
uv run cua replay mockcore/member.read_savings_balance --input member_id=99999   # business outcome
uv run cua replay mockcore/member.read_savings_balance --input member_id=55555   # PERMISSION_DENIED
```

Replay exit codes: `0` success, `3` business outcome, `2` escalated, `1` failed. Each run
writes its evidence to `runs/<run-id>/`:
- `events.jsonl`: the redacted event log
- `screenshots/`: masked screenshots
- `result.json` for replays, or `trace.json` for discovery runs
- `trace.zip`: the Playwright trace, kept when a run fails

To exercise runtime conditions, restart MockCore with faults, e.g.
`uv run cua mockcore --faults maintenance,modal`.

**Without live services:** the whole test suite runs offline. The discovery loop is tested
with a scripted stand-in for the model (`tests/agent/test_discovery.py`), driving the real
browser against a local MockCore.

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
uv run cua mockcore                      # http://127.0.0.1:8765  (svc-cua / mockcore-demo)
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

## Human handoff

Run with `--human operator` (the default for `discover`, and optional for `replay` together with
`--escalate`). A local operator page opens at `http://127.0.0.1:8766`, and the browser runs
visibly. When automation needs a person (an irreversible step to approve, an unrecognised
state, or the agent is stuck), it pauses and a request appears on the page with the
screenshot, the step and the reason.

- **Approve / Reject** an irreversible step.
- **Take control:** the session's control lease moves to you. Automation is now blocked from
  acting (enforced at the browser layer), and you operate **the same browser window**. Your
  clicks and changes are recorded, with values redacted.
- **Hand back:** choose *resume*, *skip step* or *abort*. Automation re-checks the page and
  carries on from the current step.

Every control transfer (automation → paused → human → automation) is logged with who made it
and why, and appears in the run report.

```bash
uv run cua mockcore --faults modal          # an overlay the app profile could fail to recognise
uv run cua replay mockcore/member.read_savings_balance --input member_id=12345 \
  --human operator --escalate
```

## Cross-tenant reuse

Many institutions run the same vendor product, configured differently. Capabilities are
written once per **product** and refer to elements by name (`member_id_field`). A tenant
that differs gets a small, reviewable overlay in `tenants/<tenant>/overrides/<capability>.yaml`
instead of a re-recording. An overlay can:
- prepend tenant-specific locators to named targets (the base locators remain as
  fallbacks, so drift is still detected);
- add targets;
- insert extra steps (e.g. a consent checkbox this tenant requires).

Tenant-level app-profile overrides go in `tenant.yaml`. Before acting, replay reads the
product version off the page and refuses to run a capability outside the version range it
was validated for.

```bash
uv run cua mockcore --variant b --port 8767      # "Harbor Valley": same product, v4.3, different wording
uv run cua replay mockcore/member.read_savings_balance --input member_id=12345 --tenant harbor_valley_fcu
```

Without an overlay, the base capability still succeeds on the second tenant via fallback
locators, and the result lists drift warnings. With the overlay it runs with no drift, and
the result records `tenant` and the detected `app_version`.

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
tenants/            per-institution config and capability overlays
schema/             generated JSON Schemas for the artifact formats
tests/              unit, HTTP-level and browser tests
```

## Deliverables (per the brief)

| Path | Contents |
|---|---|
| `/README.md` | This file: setup, configuration and the demo path |
| `/REPORT.md` | Design write-up: Architecture · Artifact schema · Determinism & error handling · Heterogeneity & multi-tenant · Escalation & handoff · Safety · Cuts |
| `/evidence/` | Example capability artifact, plus logs from a real discovery run and from replay runs (including error cases) |
