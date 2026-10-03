# REPORT: Computer-Use Automation System

An LLM discovers a goal once, on a hostile legacy back-office UI. The run is compiled into a typed, versioned **capability**, which is then **replayed with no model in the loop**. Every replay ends as a success, a business outcome, an escalation or a hard failure. When automation can't proceed safely, a person takes over the same live browser and hands it back.

[`evidence/`](evidence/README.md) holds, all recorded on the final code: two real Gemini discovery runs, twelve replays, 84 repeated replays (every result identical) and a live handoff.

## 1. Architecture

```
goal + params ─► DISCOVERY (LLM) ─► trace ─► COMPILER ─► capability (draft) ─review─► approved
                 observe→decide→guard→act                      │
                                                     REPLAY (no LLM) ─► RunResult
 shared: Surface (Playwright) · Policy (allowlist, risk, redaction) · App profile (known states)
         Control lease + operator page · Evidence (redacted JSONL, masked screenshots, report)
```

- **One process, one CLI** (`cua discover | replay | review | report | mockcore`).
  - The only concurrent piece, the operator page, never touches the browser, so Playwright's sync API suffices.
  - I rejected services and queues as plumbing the brief doesn't reward.
- **Hybrid perception.** The model sees a ref-numbered element list across all frames, plus a screenshot.
  - The list carries legacy layout context: a control's table row, a cell's column header.
  - The model acts **only by ref**, because coordinates identify nothing next month in a different window.
  - Each chosen element becomes a verified locator bundle (§3).
- **Credentials never reach the model.** Sign-on is a hand-written capability that discovery and replay both run first.
- **Target: MockCore,** a fictional credit-union core I built to be hostile.
  - An HTML 4 frameset, nested tables, no ids, unlabeled inputs, `<span onclick>` buttons.
  - Switchable faults and a second-tenant variant, so every error class is reproducible, which a public demo site wouldn't allow.
- **Model:** behind a one-method `LLMClient`, with Claude and Gemini adapters. The evidence used `gemini-3.5-flash`.
  - Overloads are retried with backoff; a run that still fails ends cleanly as `aborted`.
  - A scripted client tests the whole loop offline against the real browser.

## 2. Artifact schema

A capability lives at `capabilities/<product>/<id>/<version>.yaml`. See [the real one](evidence/artifact/member.read_savings_balance@1.1.0.yaml).

- **Contract**, what a calling agent relies on:
  - typed `inputs` with sensitivity, validated before the UI is touched;
  - typed `outputs` (`money` → `{amount, currency}`);
  - `outcomes` codes, `requires`, and the app version range it was validated on.

  `cua review` also lists the outcomes inherited from the app profile.
- **Implementation**, what replay executes:
  - named `targets`, each an ordered locator bundle with a frame path;
  - `steps`, each an action on a target with a `risk` class, an `expect` checkpoint and outcome rules;
  - a `success` condition.
- **Governance:**
  - `review` goes `draft` → `approved`, with the reviewer. Per-step pre-approval of irreversible steps is allowed only once the capability is approved.
  - `provenance` records the discovery run, the model and any merged runs.
  - A `content_hash` covers contract and implementation but not governance. Approving doesn't change identity; changing a step does.
  - **Approval is bound to that hash:** edit an approved capability and replay treats it as a draft again.
  - Versions are semver: a learned outcome is a minor version (`1.0.0` → `1.1.0`), and versions on disk are never overwritten.
- **Templates.** Values are written as `{{inputs.member_id}}`, and `{{secrets.X}}` only as a fill value. They are applied **at record time**, so artifacts hold no concrete values.
- **What the compiler drops:** fallbacks that could hit the wrong element or carry data.
  - Positional CSS, for values being read and for targets picked out by an input.
  - Row anchors on input-picked targets, because that row is the member's data.
- **App profile** (`apps/<product>/profile.yaml`), for product-wide knowledge:
  - fingerprint, version pattern and sign-on capability;
  - `sensitive_fields`;
  - **known states** with handlers. They are checked in file order, so a blocking overlay comes before the page it covers; replay 05 is the case that forced this rule.
- **YAML with a strict loader,** chosen for reviewability.
  - `01234` stays a string, `no` isn't `false`, and duplicate keys fail.
  - Pydantic rejects unknown keys and checks cross-references.
  - The generated JSON Schemas are tested for staleness.
- **`RunResult`** is one of:
  - `success`, with outputs;
  - `business_outcome`, with a code and its source;
  - `failed`, with one of 14 categories, plus the step, expected vs. observed, and evidence;
  - `escalated`.

  It also carries recoveries, drift warnings, interventions, tenant and app version. An unexpected exception still yields a result (`INTERNAL_ERROR`/`CONFIGURATION_ERROR`), never just a traceback.

## 3. Determinism & error handling

**Locators.** Semantic strategies come first: role+name, label, "control in the row labelled X", grid cell by row text and column header, and exact text. Attribute and CSS come last.

- A strategy counts only if it matches **exactly one visible element**; replay never picks among candidates.
- Every candidate is verified against the chosen element at record time.
- A winning fallback is recorded as a **drift warning**.

**Waiting.** There are no sleeps. Every wait is a condition polled to a per-step deadline. After a page-changing action, the checkpoint is "the element the next step needs is visible", derived by the compiler.

**Each action is sent at most once.** A click first probes actionability without clicking; a failure there means nothing reached the app, so the click may be retried. The click is then dispatched without waiting on navigation. A timeout after dispatch is **uncertain**: it is never re-sent, and the checkpoint decides whether it worked. A slow server cannot cause a double submission.

**Errors are classified per step, while polling:**

- **Business outcome.** A capability rule (`NO MEMBERS FOUND…`) or a profile rule (`ACCESS DENIED` → `PERMISSION_DENIED`) ends the run as a result, exit code 3.
  - Rules are learned by **negative discovery**: a bad-input run whose outcome text must appear verbatim on screen.
  - Replay 02 vs. 03 shows the same input failing before learning and returning a typed outcome after.
- **Recoverable.** A known interstitial is handled and the step resumes. Session expiry signs on again and restarts, unless an irreversible step already ran (`SESSION_UNRECOVERABLE`). Recoveries are capped.
- **Hard failure:** `LOCATOR_*`, `CHECKPOINT_FAILED`, `UNKNOWN_STATE`, `APP_ERROR`… Each carries the step, expected vs. observed (redacted) and a masked screenshot. An unrecognised state is never proceeded past (replay 09).

**Before acting,** replay checks the app fingerprint and the version on the page, and refuses a capability outside its validated range. The 84 repeated replays, across ten scenarios, gave identical results every time.

## 4. Heterogeneity & multi-tenant

**Surface seam.** Everything above the `Surface` protocol (`observe`, `resolve`, `check`, `act`, `describe_ref`, `screenshot`) speaks in named targets, conditions and actions. The semantic strategies map onto desktop accessibility APIs:

| Artifact concept | Desktop equivalent |
|---|---|
| role + name | UI Automation ControlType/Name, or AX role/title |
| label | LabeledBy |
| row anchor / grid cell | Table and Grid patterns |
| frame path | window → pane path |

So a desktop surface, stubbed in `surface/desktop.py`, needs no schema change. It would swap the web-only fallbacks for image templates, and handoff would use a shared RDP or VNC session under the same lease.

**Multi-tenant.** Capabilities are written **once per vendor product**.

- A tenant whose instance differs gets a small **overlay**. It can prepend locators to named targets (the base ones stay as fallbacks, so drift still shows), add targets, or insert steps.
- The effective capability is re-validated and hashed. It does **not** inherit the base approval: overlays aren't reviewed objects yet (§7).
- Shown on "Harbor Valley" (MockCore 4.3: relabelled menu, label, button and balance column):
  - **Without the overlay (replay 10):** navigation works on fallbacks with drift warnings. Once the balance column is renamed, the run **stops rather than read a money value by position**. Degrading safely beats returning another account's balance.
  - **With a [4-target overlay](evidence/artifact/harbor_valley_fcu.overlay.yaml) (replay 11):** clean.
  - **The base tenant (replay 12):** unaffected.
- A tenant whose detected version differs from its configured one is logged. Aggregated per tenant and version, drift and version refusals would drive canary replays after vendor upgrades.

## 5. Escalation & handoff

**Detecting the need:**

- **Discovery:** stuck detection (the same action with no page change, or repeated errors), the time budget running out, or the agent's `request_human` tool. An exhausted step budget aborts.
- **Replay:** with `--escalate`, any failure a person could fix.
- **Both:** irreversible steps under the approval policy.

Each request carries the goal or capability, step, reason, URL, a masked screenshot and the recent events. An unclaimed request times out (`TIMEOUT`); a person who has taken control is never interrupted.

**Control model.** A **control lease** moves automation → paused → human → automation, and every transfer records who made it and why.

- The surface calls `assert_automation()` before **every** action.
- The operator page offers approve or reject, **take control** of the same browser window, and **hand back** to resume, skip or abort (`HUMAN_ABORTED`).
- An injected recorder captures the person's clicks and edits in every frame. Input made while paused, before anyone takes control, is still recorded and flagged `without_lease`.
- **On hand-back:** if the step's checkpoint already holds, the person did it, so it is **not repeated**. Otherwise the policy gate runs again first, because the page changed under a person's hands.
- Automation is denied the operator page's own origin.

The [handoff run](evidence/handoff/01-operator-takeover/run_report.md) shows the whole sequence.

**Built vs. designed.** The lease, queue, operator page and action capture are real. Remote streaming, operator authentication, on-call routing and signed approvals are designed only.

## 6. Safety

**Allowlist, enforced by the browser.** Every request, including sub-frames and XHR, is checked against origins and path globs, and deny wins. MockCore's `/__admin` hooks are unreachable. The model has no navigate tool.

**Risk.**

- Effective risk is the higher of the declared risk and the risk implied by the control's label (confirm, submit, transfer…). Typing never commits.
- A key press is judged by what it acts on: the focused element, plus the form that Enter would submit. Tab to Confirm then Enter needs approval, just like a click.
- Irreversible actions always need a person in discovery. On replay they need one unless the capability is approved (with a matching hash) **and** that step was pre-approved.

**Data.**

- Secrets come from the environment at the moment of use.
- Text redaction masks secrets in any letter case, declared PII as whole tokens, SSNs, Luhn-valid cards, e-mails and credential-named keys.
- Screenshots are painted over before they are written: matching text, typed values, password fields, and every field the profile marks sensitive (name, date of birth, address, balances…), whether or not the run declared it. Failure text in logs masks the same fields.
- Outputs reach the caller in full and are masked in evidence.
- The model's unmasked frame is never kept.

**Found by reviewing real evidence, now closed:**

- Playwright traces held the typed password; they are now opt-in (`--debug-trace`) and git-ignored.
- Screenshots and logs leaked undeclared member data.
- Approval could be bypassed via Enter, by editing a capability, or through an overlay.

**Limits:**

- Data outside classified fields is caught only by pattern, including the model's free text and what an operator types.
- Approval binding is a hash, not a signature.
- Sending pages to a hosted model is itself a data flow; production needs zero-retention or a private deployment.
- The operator page is local and unauthenticated.

## 7. Cuts

**Cut deliberately:**

- a remote operator console;
- the desktop surface;
- LLM repair of a failing replay step, which would blur "no model in replay";
- an agent-facing API;
- a registry or services.

**Gaps the real runs exposed:**

- **Loose contracts.** Discovered inputs are loosely typed: `member_id` is just a string, so `abc` fails at a checkpoint rather than as `INVALID_INPUT`. A parameter-format option on `discover` would fix this.
- **Model-named outcome codes,** such as `NO_MEMBERS_FOUND_MATCHING_SEARCH_CRITERIA`. Review should be able to rename them.
- **Overlay review.** Overlays need their own review before tenant-specific irreversible steps can run unattended.

**Next, in order:**

1. Capabilities as typed MCP or function-calling tools.
2. Overlay review and signed approvals.
3. Replay canaries, with stability scores gating `approved`.
4. Idempotency checks ("already submitted?") before restarting after a lost session.
5. Remote, authenticated operator sessions.
6. A vision fallback for surfaces with no accessibility tree.
