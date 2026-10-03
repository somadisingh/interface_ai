# Evidence

Every folder here is an unedited run directory produced by the tool. Each contains:

- `events.jsonl`: the redacted event log, which is the source of truth
- `run_report.md`: a timeline generated from that log, with thumbnails
- `screenshots/`: masked screenshots
- `result.json` (replay) or `trace.json` (discovery)

Inputs, extracted outputs and secrets appear masked throughout (`***45`, `*****33`, `[SECRET]`).

The target app is **MockCore**, the fictional core-banking system in `src/mockcore/`. All of its member data is invented.

## The artifact

- [`artifact/member.read_savings_balance@1.1.0.yaml`](artifact/member.read_savings_balance@1.1.0.yaml): the capability compiled from the discovery run below, with the `MEMBER_NOT_FOUND` outcome merged in from the negative run. It is identical to `capabilities/mockcore/member.read_savings_balance/1.1.0.yaml`.
- [`artifact/harbor_valley_fcu.overlay.yaml`](artifact/harbor_valley_fcu.overlay.yaml): the second tenant's overlay for it, identical to `tenants/harbor_valley_fcu/overrides/member.read_savings_balance.yaml`.

## Discovery: real LLM runs (Gemini `gemini-3.8-flash`)

Each run was started with `uv run cua discover ...` as shown in the README, against MockCore on a Mac. Sign-on ran as the deterministic `session.sign_on` capability first, so the model never saw credentials.

| Run | Goal | Result |
|---|---|---|
| [`01-gemini-discovery-success`](discovery/01-gemini-discovery-success/run_report.md) | Look up member 12345, read the savings balance | **succeeded** in 6 model turns. Compiled to `1.0.0` with 5 steps, 5 named targets and derived checkpoints. The 138 s gap before the `extract` turn is the client retrying Gemini `503 UNAVAILABLE` (overload) responses with backoff. |
| [`02-gemini-negative-discovery`](discovery/02-gemini-negative-discovery/run_report.md) | Look up member 99999 (does not exist) | **outcome** `MEMBER_NOT_FOUND` in 4 turns. Merged into `1.1.0` as a rule on the search step. |

Known blemishes in run 01, kept as recorded rather than edited:

- **Token counts show `[SECRET]`.** At the time, the redactor treated any key containing `token` as a credential, which hid `input_tokens`. This is fixed, and run 02 shows real counts.
- **The fictional member's name appears.** It is in the model's free-text summary (`Jane Q Sample`). It is also in one recorded fallback locator, which anchored on the clicked row's name. The recorder now drops anchors on record data for targets picked out by an input. The compiled artifact never had it: `1.0.0` was recompiled from this trace with that rule. Masking free text the model writes is listed under Cuts in `REPORT.md`.
- **No `model-view.png`.** The frame the model sees is unmasked by design, so it was removed from both runs here. Discovery now deletes it as soon as it has been sent.

## Replay: deterministic, no model in the loop

Every run used `uv run cua replay mockcore/member.read_savings_balance --input member_id=<id>` (version `1.1.0` unless noted). Faults were switched on through MockCore's admin hook before each run.

| Run | Fault / tenant | Result | What it shows |
|---|---|---|---|
| [`01-success-34567`](replay/01-success-34567/run_report.md) | | `success`, exit 0 | The recorded flow generalises to a member other than the one used in discovery |
| [`02-not-found-before-learning`](replay/02-not-found-before-learning/run_report.md) | version `1.0.0` | `failed` `CHECKPOINT_FAILED`, exit 1 | Before the negative run, an unknown screen is a hard failure, never a guess |
| [`03-not-found-99999`](replay/03-not-found-99999/run_report.md) | | `business_outcome` `MEMBER_NOT_FOUND`, exit 3 | ...and after it, a typed result for the caller |
| [`04-permission-denied-55555`](replay/04-permission-denied-55555/run_report.md) | restricted member | `business_outcome` `PERMISSION_DENIED`, exit 3 | Outcome known from the shared app profile, not the capability |
| [`05-recovery-maintenance-and-modal`](replay/05-recovery-maintenance-and-modal/run_report.md) | `maintenance,modal` | `success`, 2 recoveries | Overlay dismissed first, then the interstitial, then the step resumes |
| [`06-recovery-session-timeout`](replay/06-recovery-session-timeout/run_report.md) | `session_timeout=3` | `success`, 1 recovery | Session expiry mid-flow: re-sign-on and restart (safe because no irreversible step had run) |
| [`07-slow-pages`](replay/07-slow-pages/run_report.md) | `slow=1500` | `success` | Polling waits for checkpoints instead of fixed sleeps |
| [`08-error-app-500`](replay/08-error-app-500/run_report.md) | `error500=/members/1` | `failed` `APP_ERROR`, exit 1 | Error replay: known failure state, failure screenshot, step id |
| [`09-error-unknown-dialog`](replay/09-error-unknown-dialog/run_report.md) | `announcement` | `failed` `UNKNOWN_STATE`, exit 1 | Error replay: an unrecognised blocking dialog stops the run with expected vs observed text. With `--escalate` it goes to a human instead (see the handoff run). |
| [`10-harbor-valley-no-overlay`](replay/10-harbor-valley-no-overlay/run_report.md) | tenant `harbor_valley_fcu`, MockCore 4.3.0 with relabelled UI, no overlay | `success` with 4 drift warnings | The base capability survives a second bank on fallback locators, and says which ones it fell back to |
| [`11-harbor-valley-with-overlay`](replay/11-harbor-valley-with-overlay/run_report.md) | same tenant, with the overlay | `success`, no drift | A small tenant overlay restores primary locators; the base artifact is unchanged |
| [`12-first-example-unaffected`](replay/12-first-example-unaffected/run_report.md) | tenant `first_example_cu` | `success`, no drift | The overlay applies only to its own tenant |

Not included: raw Playwright traces. They record typed credentials and unmasked pages, so they are now opt-in (`--debug-trace`), named `trace.UNREDACTED.zip`, and git-ignored.
