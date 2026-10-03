# REPORT: Computer-Use Automation System

The system works in three stages:

- An LLM discovers a goal once, on a hostile legacy back-office UI.
- The run is compiled into a typed, versioned **capability**.
- The capability is **replayed with no model in the loop**.

Every replay ends as a success, a business outcome, an escalation or a hard failure. When automation can't proceed safely, a person takes over the same live browser and hands it back. The evidence (real Gemini discovery runs, twelve replays and a live handoff) is in [`evidence/`](evidence/README.md).

## 1. Architecture

```
goal + params ─► DISCOVERY (LLM) ─► trace ─► COMPILER ─► capability (draft) ─review─► approved
                 observe→decide→guard→act                      │
                                                     REPLAY (no LLM) ─► RunResult
 shared: Surface (Playwright) · Policy (allowlist, risk, redaction) · App profile (known states)
         Control lease + operator page · Evidence (redacted JSONL, masked screenshots, report)
```

- **One process, one CLI** (`cua discover | replay | review | report | mockcore`). The only concurrent piece, the operator page, never touches the browser, so Playwright's sync API is enough. I rejected services and queues as plumbing the brief doesn't reward.
- **Perception is hybrid.** The model sees a ref-numbered element list across all frames, with legacy layout context (a control's table row, a cell's column header), plus a screenshot. It acts **only by ref**: coordinates don't identify anything next month in a different window. Each chosen element becomes a **verified locator bundle** (§3).
- **Credentials never reach the model.** Sign-on is a hand-written capability that both discovery and replay run first.
- **Target: MockCore,** a fictional credit-union core I built to be hostile: an HTML 4 frameset, nested layout tables, no ids, unlabeled inputs, `<span onclick>` buttons, plus switchable faults and a second-tenant variant. A public demo site couldn't make every error class reproducible.
- **Model:** one-method `LLMClient` with Claude Sonnet 5.5 and Gemini 3.8 Flash implemented; the evidence runs used Gemini. Overloads (503) are retried with backoff. A scripted client tests the whole loop offline against the real browser.

## 2. Artifact schema

A capability lives at `capabilities/<product>/<id>/<version>.yaml` and has three parts. See [the real one](evidence/artifact/member.read_savings_balance@1.1.0.yaml).

- **Contract:** what a calling agent relies on.
  - Typed `inputs`, with sensitivity, validated before the UI is touched.
  - Typed `outputs`, e.g. `money` → `{amount, currency}`.
  - The declared `outcomes` codes, `requires`, and the app version range it was validated on.
- **Implementation:** what replay executes.
  - Named `targets`, each an ordered locator bundle with a frame path.
  - `steps`: an action on a named target, a `risk` class, an `expect` checkpoint and outcome rules.
  - A `success` condition.
- **Governance:**
  - `review` goes from `draft` to `approved`, recording who and when. Per-step pre-approval of irreversible steps is allowed only once the capability is approved.
  - `provenance` records the discovery run and the model.
  - A `content_hash` covers contract and implementation but not review or provenance. Approving doesn't change identity; changing a step does.
  - Versions are semver. A new outcome learned by negative discovery is a minor version (`1.0.0` → `1.1.0`).

**Templates and data.** Values are written as templates: `{{inputs.member_id}}`, and `{{secrets.X}}` only as a fill value. The recorder swaps parameter values for templates **at record time**, so artifacts never hold concrete values. The real run surfaced one more rule: for a target picked out by an input (the clicked member-number link), the recorder no longer anchors on the rest of its row. That row is the record's data, here a member's name, so it would leak into the artifact and only ever match that member.

**App profile.** Product-wide knowledge lives in `apps/<product>/profile.yaml`, not in every capability: the fingerprint, version pattern, sign-on capability, and **known states** with handlers. States are checked in file order, so a blocking overlay is listed before the page it can cover; replay 05 is the case that forced that rule.

**YAML, with a strict loader.** I chose YAML because it is reviewable in a pull request and allows comments. The strict loader removes its coercion hazards: `01234` stays a string, `no` isn't `false`, and duplicate keys are rejected. Pydantic rejects unknown keys and checks every cross-reference. JSON Schemas generated into `schema/` are tested for staleness.

**`RunResult`.** It returns one of `success` (typed outputs), `business_outcome` (code plus source: capability or profile), `failed` (one of 13 categories, plus step, expected vs. observed, and evidence) or `escalated`. It also carries recoveries, drift warnings, interventions, tenant and detected app version. Callers branch on fields, never on strings.

## 3. Determinism & error handling

**Locators.** Semantic strategies come first: role+name, label, "control in the row labelled X", grid cell by row text and column header, and exact text. Web-only strategies come last: attribute, then CSS.

- A strategy counts only if it matches **exactly one visible element**; replay never picks among candidates.
- At record time, each candidate is verified to hit the chosen element. Unverified ones are dropped, as are self-text strategies for values being read.
- When a fallback wins, replay records a **drift warning**. Replay 10 succeeds on the second tenant through four fallbacks and names each one.

**Waiting and checkpoints.** There are no sleeps: every wait is a condition polled to a per-step deadline. After each page-changing action, the checkpoint is "the element the next step needs is visible". The compiler derives it from the recorded flow.

**Errors are classified while polling, per step:**

- **Business outcome.** Capability rules (`NO MEMBERS FOUND…` → `MEMBER_NOT_FOUND`) or profile rules (`ACCESS DENIED` → `PERMISSION_DENIED`) end the run as a result, exit code 3.
  - Rules are learned by **negative discovery**: a bad-input run whose outcome text must appear verbatim on screen is merged in.
  - Replay 02 vs. 03 shows the same input failing before learning and returning a typed outcome after.
- **Recoverable.** A known interstitial is handled and the step resumes.
  - An action is retried only if it **never completed**, so a recovery can't double-submit.
  - Session expiry signs on again and restarts, unless an irreversible step already ran (`SESSION_UNRECOVERABLE`).
  - Recovery limits apply per step and per run.
- **Hard failure:** `LOCATOR_*`, `CHECKPOINT_FAILED`, `UNKNOWN_STATE`, `APP_ERROR`, and so on.
  - Each carries the step, expected vs. observed (redacted) and a masked screenshot.
  - An unrecognised state is never proceeded past (replay 09).

**Before acting,** replay checks the app fingerprint and the product version read from the page. A capability outside its validated range is refused.

## 4. Heterogeneity & multi-tenant

**Surface seam.** Everything above the `Surface` protocol speaks in named targets, conditions and actions: `observe`, `resolve`, `check`, `act`, `describe_ref` and `screenshot`. The semantic strategies map onto desktop accessibility APIs:

| Artifact concept | Desktop equivalent |
|---|---|
| role + name | UI Automation ControlType/Name, or AX role/title |
| label | LabeledBy |
| row anchor / grid cell | Table and Grid patterns |
| frame path | window → pane path |

So a desktop surface, stubbed in `surface/desktop.py`, needs no schema change. It would drop the web-only fallbacks and use image templates as its last resort. Handoff on desktop needs a shared RDP or VNC session; the lease model is the same.

**Multi-tenant.**

- Capabilities are written **per vendor product**. A tenant whose instance differs gets a small **overlay** that can:
  - prepend locators to named targets, keeping the base ones as fallbacks so drift still shows;
  - add targets;
  - insert steps, such as a consent checkbox before submitting.
- The effective capability is re-validated and hashed, and results record the tenant and app version.
- Shown on "Harbor Valley", MockCore 4.3 with a relabelled menu, labels, button and column:
  - without an overlay, success via fallbacks with drift (replay 10);
  - with a [4-target overlay](evidence/artifact/harbor_valley_fcu.overlay.yaml), clean (replay 11);
  - the base tenant is unaffected (replay 12).
- At scale, drift and version-gate refusals per tenant and version would drive canary replays after vendor upgrades. Re-recording is the last resort.

## 5. Escalation & handoff

**When a person is needed:**

- **Discovery:** stuck detection (the same action with no page change, or repeated errors), budget exhaustion, or the agent's `request_human` tool.
- **Replay:** with `--escalate`, any failure a person could fix.
- **Both:** irreversible steps under the approval policy.

Each request carries the goal or capability, step, reason, URL and a masked screenshot.

**Control model.** A **control lease** moves automation → paused → human → automation, and every transfer records who made it and why.

- The surface calls `assert_automation()` before **every** action, so automation can't act while a person holds the session.
- On the operator page, the person can approve or reject, **take control** of the same browser window, then **hand back** to resume, skip or abort.
- An injected recorder captures their clicks and changes, redacted, in every frame.
- On hand-back, automation re-observes the page and re-checks the step's checkpoint; it never assumes where the person left off.
- Input made while paused, before anyone takes control, is recorded too, flagged `without_lease`. A real demo run had missed such a click; that is how the gap was found.

The [handoff evidence](evidence/handoff/01-operator-takeover/run_report.md) shows the full sequence: unknown dialog → request → take control → the person's click → hand back → success.

**Built vs. designed.** The lease, queue, operator page and action capture are real. Remote streaming, authenticated operators, on-call routing with SLAs, and signed approval records are designed only.

## 6. Safety

**Allowlist, enforced by the browser.** Every request, including sub-frames and XHR, is checked against origins and path globs, and deny wins. MockCore's `/__admin` hooks are unreachable. The model has no navigate tool.

**Risk.**

- Effective risk is the higher of the declared risk and the risk implied by the control's label (confirm, submit, transfer…). Typing never commits.
- Irreversible actions always need a person in discovery.
- On replay they need a person unless the capability is approved **and** that step was pre-approved. A rejection stops the run before the action.

**Secrets and personal data.**

- Secrets are resolved from the environment at use and registered with the redactor.
- The redactor masks secrets, declared PII (whole tokens: `***45`), SSNs, Luhn-valid card numbers, e-mail addresses and credential-named keys.
- Screenshots are painted over before they are written.
- The model gets a secrets-masked view. Its frame is not kept on disk.
- Outputs reach the caller in full and are masked in all evidence.

**Reviewing real evidence closed two gaps:**

- Playwright traces captured the typed password and unmasked pages. They are now opt-in (`--debug-trace`), named `trace.UNREDACTED.zip`, and git-ignored.
- Usage counts were being masked as credentials. That is fixed.

**Limits:**

- Undeclared PII is only caught by pattern. A name in the model's own free text (`Jane Q Sample`, discovery run 01) slips through.
- Sending page content to a hosted model is itself a data flow. Production needs zero-retention or a private deployment, plus field classification in the app profile.
- The operator page is local and unauthenticated.

## 7. Cuts

**Cut deliberately:**

- A remote operator console; a local page proves the transfer model.
- The desktop surface (designed in §4).
- LLM repair of a failing replay step, which would blur "no model in replay".
- An agent-facing API, stability scoring, and a central registry or services.

**Gaps the real runs exposed:**

- **Discovered contracts are loose.** `member_id` is typed only as a string, so `abc` reaches the UI and fails at a checkpoint instead of `INVALID_INPUT`. A reviewer should add a pattern; a parameter-format flag on `discover` is the fix.
- **Masking free text** the model writes about the record it is viewing.

**Next, in order:**

1. Capabilities as typed MCP or function-calling tools.
2. Remote, authenticated operator sessions.
3. Replay canaries and stability scores gating `approved`.
4. Idempotency checks ("already submitted?") before restarting after a lost session.
5. A vision fallback for surfaces with no accessibility tree.
6. Overlay suggestions generated from aggregated drift.
