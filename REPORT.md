# REPORT — Computer-Use Automation System

A goal is discovered once by an LLM on a live, hostile, frameset-based back-office UI. The run
is compiled into a typed, versioned capability, and that capability is replayed
deterministically with no model in the loop. Every result is classified as a business
outcome, a recoverable condition, or a hard failure. When the system can't proceed safely, a
person takes over the same live session and hands it back.

## 1. Architecture

```
 goal + params ──► DISCOVERY (LLM) ──► trace ──► COMPILER ──► capability (draft) ──review──► approved
                        │                                          │
            observe → decide → guard → act                    REPLAY (no LLM) ──► RunResult
                        │                                          │
        ┌───────────────┴──────────────── shared ──────────────────┴───────────────────┐
        │ Surface (Playwright) · Policy (allowlist, risk, redaction) · App profile       │
        │ Control lease + human channel (operator page) · Evidence (JSONL, screenshots)  │
        └────────────────────────────────────────────────────────────────────────────────┘
```

- **One process, one CLI** (`cua discover | replay | review | report | mockcore`). The only
  concurrent piece is the local operator page, which runs in a thread and touches just the
  control lease and the request queue, never the browser. That is why Playwright's **sync**
  API is enough. Queues and services were deliberately not built (§7).
- **Perception is hybrid.** The model sees a compact, ref-numbered list of the page's
  elements across all frames, with legacy layout context (the table row a control sits in,
  the column header of a cell), plus a screenshot. It acts **only by ref**, never by pixel
  coordinates. Before each action, the recorder turns the chosen element into a verified
  locator bundle, so what gets recorded is replayable next month on a differently sized
  window. Pure screenshot-and-coordinates control was rejected because coordinates are not a
  replayable identity.
- **Credentials never reach the model.** Sign-on is a hand-authored capability that
  discovery and replay both run as a prerequisite. The agent starts already signed in.
- **Target:** *MockCore*, a fictional credit-union back office built to be hostile:
  - HTML 4 frameset
  - nested layout tables
  - no ids or test ids
  - labels that aren't associated with their inputs
  - `<span onclick>` "buttons" and `javascript:` links

  It has switchable runtime faults and a second-tenant variant. Building it cost half a day
  but made every error class reproducible in tests and evidence. A public demo site would
  not allow that.
- **Model:** Claude (Sonnet 5.5) or Gemini (3.8 Flash) behind a one-method `LLMClient`; the
  Gemini adapter translates tools, history and screenshots, and returns thought signatures. A scripted client makes the
  whole discovery → compile → replay loop testable offline against the real browser.

## 2. Artifact schema

A capability (`capabilities/<product>/<id>/<version>.yaml`) has two halves. The
**contract** is what a calling agent relies on. The **implementation** is what replay
executes.

- **Contract:**
  - `inputs`: typed, with regex, enum and sensitivity. They are validated *before* the UI is
    touched.
  - `outputs`: typed, e.g. `money` → `{amount, currency}`.
  - `outcomes`: the business outcome codes the capability can return.
  - `requires`: e.g. `session.sign_on`.
  - `app.product_versions`: the version range it was validated on.
- **Implementation:**
  - `targets`: named elements, each an ordered **locator bundle** with a frame path and a
    `robustness_note` explaining the choice.
  - `steps`: each has an action on a named target, a `risk` class, an `expect` checkpoint,
    and outcome rules.
  - `success`: the success condition.
- **Governance:**
  - `review` (`draft` → `approved`, by whom and when). Steps can carry an
    `auto_approve_on_replay` flag, allowed only for irreversible steps.
  - `provenance`: source, discovery run, model.
  - `content_hash`: covers contract and implementation but *not* review or provenance.
    Approving a capability doesn't change its identity; changing a step does.
- **Versioning:** semver. A contract change is major, a new recovery or fallback is minor,
  a locator tweak is patch. `schema_version` versions the format itself.
- **Values are templates:** `{{inputs.member_id}}`, and `{{secrets.X}}` allowed only as a
  fill value. The recorder swaps parameter values for templates *at record time*, so
  neither traces nor artifacts ever contain the concrete values from discovery.
- **Knowledge shared across a product lives in an app profile**
  (`apps/<product>/profile.yaml`), not in every capability: the fingerprint, a version
  pattern, the sign-on capability, and **known states** with handlers, such as maintenance
  interstitials, session expiry, access denied and the error page.
- **Why YAML:** it's reviewable in a pull request and allows comments. Its type coercion
  hazards are removed by a **strict loader**: `01234` stays a string, `no` is not `false`,
  and duplicate keys are rejected. Pydantic models reject unknown keys and check every
  cross-reference: targets, templates, outputs extracted exactly once, and declared outcome
  codes. JSON Schemas are generated into `schema/`, and a test fails if they go stale.
- **Result contract** (`RunResult`), with four mutually exclusive statuses:
  - `success`, with typed outputs
  - `business_outcome`, with a code and its source (capability or app profile)
  - `failed`, with `category`, `step_id`, `expected`, `observed` and evidence paths
  - `escalated`

  It also lists recoveries, drift warnings, interventions, tenant and detected app version.
  Failure categories form a closed set of 13. Callers never parse strings.

## 3. Determinism & error handling

**Locating elements.** Each target carries an ordered bundle of strategies:
- semantic strategies first: role+name, label, "control in the innermost row labelled X",
  grid cell by row text and column header, and exact text;
- web-only strategies last: attribute, then CSS.

A strategy counts only if it matches **exactly one visible element**. Zero or several means
"try the next one"; replay never picks among candidates. At record time, every candidate
strategy is **verified to hit the chosen element**, and unverified ones are dropped. For
data being read, strategies keyed on the element's own text are excluded, because that text
is the value and it changes. If a lower-ranked strategy wins at replay, the run records a
**drift warning**, so UI changes surface before they cause failures.

**Waiting and checkpoints.**
- There are no sleeps. Every wait is a condition polled until a per-step deadline.
- After each page-changing action, the checkpoint is *"the element the next step needs is
  visible"*. The compiler derives it from the recorded flow, and it is direct evidence that
  the flow can continue.

**Error taxonomy, per step, while polling:**
- **Business outcome.** The step's outcome rules (e.g. `NO MEMBERS FOUND…` →
  `MEMBER_NOT_FOUND`) or the profile's (`ACCESS DENIED` → `PERMISSION_DENIED`) end the run
  as a result, not an error. Rules are learned by **negative discovery**: a discovery run
  with bad input whose outcome text must appear verbatim on screen is merged into the
  capability as a minor version.
- **Recoverable.** A known interstitial is handled and the step **resumes**. An action is
  re-attempted *only if it never completed* (e.g. an overlay intercepted the click), so a
  recovery can never double-submit. Session expiry re-runs sign-on and **restarts** the
  flow, unless an irreversible step already ran; then it stops with
  `SESSION_UNRECOVERABLE`. Slow pages are simply waited out. Per-step and total recovery
  limits prevent loops.
- **Hard failure.** For example `LOCATOR_NOT_FOUND`, `LOCATOR_AMBIGUOUS`,
  `CHECKPOINT_FAILED`, `UNKNOWN_STATE` (an unrecognised dialog blocks the action) or
  `APP_ERROR`. Each carries the step, expected vs. observed (redacted) and a masked
  screenshot, and a Playwright trace is kept. **An unrecognised state is never proceeded
  past.**

**Checks before acting.** Replay confirms the app fingerprint and reads the product version
off the page. A capability outside its validated range is refused rather than guessed at.

All of this is tested against live MockCore with injected faults: maintenance notice,
blocking modal, slow pages, session timeout, HTTP 500, restricted and unknown members, and
validation errors.

## 4. Heterogeneity & multi-tenant

**Surface seam.** Everything above the `Surface` protocol (observe, resolve, check, act,
describe_ref, screenshot) speaks in named targets, conditions and actions. The legacy web
case is the implemented one: frames are first-class through frame paths, and there are no
DOM assumptions beyond what an operator sees. The semantic locator strategies map directly
onto desktop accessibility APIs:

| Artifact concept | Desktop equivalent |
|---|---|
| role + name | UI Automation ControlType + Name, or macOS AX role + title |
| label | LabeledBy |
| row anchor / grid cell | Grid / Table patterns |
| frame path | window → pane path |

So a desktop surface (stubbed in `surface/desktop.py`) needs no schema change. It would drop
the web-only fallbacks and use image-template matching as its last resort. Handoff on
desktop needs a shared RDP or VNC session; the lease model is unchanged.

**Multi-tenant (A+B).**
- Capabilities are written **per vendor product** and refer to elements by **name**.
- A tenant whose instance differs gets a small **overlay**. It prepends tenant locators to
  named targets (base locators stay as fallbacks, so drift is still detected), adds targets,
  or inserts steps; for example, one tenant requires a consent checkbox before submitting.
- Tenant-level profile overrides cover differently worded interstitials.
- The effective capability is re-validated and hashed separately, and results record the
  tenant and the detected app version.

Demonstrated on MockCore's variant b ("Harbor Valley", v4.3, renamed menu, labels, buttons
and column, plus an extra consent step):
- The base capability still succeeds there through fallbacks, with drift warnings.
- With the overlay it runs clean.
- The open-account flow completes only with the inserted step.
- A capability validated for `<4.3` is refused by the version gate.

At scale, drift warnings and version-gate refusals aggregated per tenant and version would
drive canary replays after vendor upgrades and flag which overlays need attention.
Re-recording per tenant is the last resort, not the model.

## 5. Escalation & handoff

**Detecting that a person is needed:**
- **Discovery:** stuck detection (the same action repeated with no page change, or repeated
  errors), budget exhaustion, or the agent's own `request_human` tool.
- **Replay:** with `--escalate`, any failure a person could fix (an unknown state, a missing
  target, a checkpoint that isn't reached).
- **Both:** irreversible steps under the approval policy (§6).

**The request** carries the capability or goal, run, step, reason, current URL and a masked
screenshot.

**Control model.** A **control lease** moves automation → paused → human → automation, and
every transfer records who made it and why. The web surface calls `assert_automation()`
before *every* action, so automation physically cannot act while a person holds the session.
While paused, the automation thread only waits and pumps browser events.

**Operator surface:** a local page listing requests with their context, offering
Approve / Reject, **Take control**, then **Hand back** with resume, skip step or abort. The
person works in **the same browser window**. An injected recorder captures their clicks and
changes in every frame, redacted and attributed only while they hold the lease; automated
typing commits its field immediately, so its change events can't be misattributed. On hand
back, automation re-observes the page and re-checks the current step's checkpoint, never
assuming where the person left it. The run report shows the whole exchange.

**Built vs. designed:** the lease, request queue, operator API/page and action capture are
real. Remote operation is designed only: streaming the session to an operator elsewhere
(CDP screencast or a VNC-backed browser), authenticated operators, routing to on-call staff
with SLAs, and approvals as signed audit records.

## 6. Safety

- **Allowlist enforced by the browser.**
  - Every request, including sub-frames and XHR, is routed through the policy: origins and
    path globs, and deny always wins. MockCore's `/__admin` test hooks are unreachable.
  - Action types are allowlisted too.
  - The model has no raw-navigation tool, so it can only move through the app's own UI.
- **Risk.**
  - Effective risk is the *higher* of the step's declared risk and the risk implied by the
    control's label (confirm, submit, transfer…). Typing never commits.
  - **Tiered approval:** during discovery, irreversible actions always need a person. On
    replay they need a person unless the capability is `approved` *and* a reviewer
    pre-approved that specific step. A rejection stops the run before the action and is
    recorded as `escalated`.
- **Secrets and personal data.**
  - Secrets are resolved from the environment at the moment of use and registered with the
    redactor, and never appear in artifacts.
  - The redactor masks known secrets, declared PII values (whole tokens, so e.g. `***45`),
    SSNs, Luhn-valid card numbers and e-mail addresses, plus any credential-named key.
  - Screenshots have sensitive targets and matching text painted over *before* the file is
    written.
  - The model sees the page through a model-side view: secrets and regulated patterns
    masked, task parameters visible.
  - Outputs go to the caller in full but are masked in all evidence.

**Limits:**
- Pattern redaction is best effort: undeclared PII that matches no pattern (e.g. a name on a
  screen) is not caught in text, and sending page content to a hosted model is itself a data
  flow. Production needs a zero-retention or private deployment and field-level
  classification from the app profile.
- Label-keyword risk can miss unusual wording. The declared risk plus human review of every
  capability is the backstop.
- The operator page is local and unauthenticated.

## 7. Cuts

**Cut deliberately:**
- A remote operator console (a local page is enough to prove the control-transfer model).
- The desktop surface (designed, §4).
- Bounded LLM repair of a failing replay step (it would blur "no model in replay").
- An agent-facing capability API.
- Multi-run stability scoring.
- A central capability registry, services and queues. The brief rewards the abstractions,
  not the plumbing.

**Next, in order:**
1. An agent-facing tool catalog (capabilities as typed function-calling or MCP tools; the
   contract already has what's needed).
2. Remote, authenticated operator sessions.
3. Replay canaries and stability scores gating `approved` status.
4. Idempotency checks for irreversible steps after a lost session (detect "already
   submitted" before restarting).
5. A vision fallback for surfaces with no accessibility tree.
6. Overlay suggestions generated from aggregated drift warnings.
