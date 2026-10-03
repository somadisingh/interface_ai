# Evidence

Every folder here is a run directory written by the tool. Each contains:

- `events.jsonl`: the redacted event log, which is the source of truth
- `run_report.md`: a timeline generated from that log, with thumbnails
- `screenshots/`: masked screenshots
- `result.json` (replay and handoff) or `trace.json` and `capability.yaml` (discovery)

The replay folders also hold `exit_code.txt`, written by the script that ran them.

Redaction throughout:

- **Text:** inputs, extracted outputs and secrets are masked (`***45`, `*****33`, `[SECRET]`). In failure text, the values of fields the app profile classifies as sensitive show as `[REDACTED]`.
- **Screenshots:** the same fields are painted over, together with typed values and credentials.

The target is **MockCore**, the fictional core-banking app in `src/mockcore/`. All of its member data is invented.

Everything here was recorded with the final code: one capability (`1.1.0`, content hash `sha256:22e1679f…`), produced by the two discovery runs below.

## The artifact

- [`artifact/member.read_savings_balance@1.1.0.yaml`](artifact/member.read_savings_balance@1.1.0.yaml) is the capability compiled from discovery run 01, with the outcome learned in run 02 merged in. It is identical to `capabilities/mockcore/member.read_savings_balance/1.1.0.yaml`. `1.0.0`, the version before the merge, sits beside it in `capabilities/`.
- [`artifact/harbor_valley_fcu.overlay.yaml`](artifact/harbor_valley_fcu.overlay.yaml) is the second tenant's overlay for it, identical to `tenants/harbor_valley_fcu/overrides/member.read_savings_balance.yaml`.

The outcome code `NO_MEMBERS_FOUND_MATCHING_SEARCH_CRITERIA` is the name the model chose. It is kept as discovered rather than edited by hand, and a reviewer could rename it in a new version.

## Discovery: real LLM runs (Gemini `gemini-3.5-flash`)

Both runs were started with the README's `uv run cua discover ...` commands, against MockCore on a Mac.

Sign-on ran first as the deterministic `session.sign_on` capability, so the model never saw credentials. The model saw a ref-numbered element list plus a screenshot, and acted only by ref.

| Run | Goal | Result |
|---|---|---|
| [`01-gemini-discovery-success`](discovery/01-gemini-discovery-success/run_report.md) | Look up member 12345 and read the savings balance | **succeeded** in 6 model turns (12 s): click, fill, click, click, extract, finish. Compiled to `1.0.0`: 5 steps, 5 named targets, derived checkpoints. |
| [`02-gemini-negative-discovery`](discovery/02-gemini-negative-discovery/run_report.md) | Look up member 99999 (does not exist) | **outcome** in 4 turns. The model ended with `finish` and the on-screen text. `--merge-into` added the outcome as a rule on the search step and saved `1.1.0`. |

The frame the model sees is unmasked except for secrets, since it has to read the page. Discovery deletes it as soon as it has been sent. Only the masked evidence screenshots are kept.

## Replay: deterministic, no model in the loop

Every run used `uv run cua replay mockcore/member.read_savings_balance --input member_id=<id>`, on version `1.1.0` unless noted. Faults were switched on through MockCore's admin hook before each run.

| Run | Fault / tenant | Result | What it shows |
|---|---|---|---|
| [`01-success-34567`](replay/01-success-34567/run_report.md) | | `success`, exit 0 | The flow generalises to a member other than the one used in discovery |
| [`02-not-found-before-learning`](replay/02-not-found-before-learning/run_report.md) | version `1.0.0` | `failed` `CHECKPOINT_FAILED`, exit 1 | Before the negative run, an unrecognised result is a hard failure, never a guess |
| [`03-not-found-99999`](replay/03-not-found-99999/run_report.md) | | `business_outcome`, exit 3 | ...and after it, a typed result for the caller |
| [`04-permission-denied-55555`](replay/04-permission-denied-55555/run_report.md) | restricted member | `business_outcome` `PERMISSION_DENIED`, exit 3 | An outcome known from the shared app profile, not from the capability |
| [`05-recovery-maintenance-and-modal`](replay/05-recovery-maintenance-and-modal/run_report.md) | `maintenance,modal` | `success`, 2 recoveries | The blocking overlay is dismissed first, then the interstitial under it, then the step resumes |
| [`06-recovery-session-timeout`](replay/06-recovery-session-timeout/run_report.md) | `session_timeout=3` | `success`, 1 recovery | Session expiry mid-flow: re-sign-on and restart (safe because no irreversible step had run) |
| [`07-slow-pages`](replay/07-slow-pages/run_report.md) | `slow=1500` | `success` | Checkpoints are polled, never slept on |
| [`08-error-app-500`](replay/08-error-app-500/run_report.md) | `error500=/members/1` | `failed` `APP_ERROR`, exit 1 | Error replay: a known failure page, with step id and failure screenshot |
| [`09-error-unknown-dialog`](replay/09-error-unknown-dialog/run_report.md) | `announcement` | `failed` `UNKNOWN_STATE`, exit 1 | Error replay: an unrecognised blocking dialog stops the run, with expected vs observed. With `--escalate` it goes to a person instead (see the handoff run). |
| [`10-harbor-valley-no-overlay`](replay/10-harbor-valley-no-overlay/run_report.md) | tenant `harbor_valley_fcu` (MockCore 4.3.0, relabelled UI), overlay removed | `failed` `CHECKPOINT_FAILED` after 3 drift warnings | Navigation survives the second bank on fallback locators, and drift says which ones. But the balance column is renamed, and the capability will not read a money value by position, so it stops rather than risk returning the wrong number. |
| [`11-harbor-valley-with-overlay`](replay/11-harbor-valley-with-overlay/run_report.md) | same tenant, with its 4-target overlay | `success`, no drift | A small tenant overlay restores semantic locators; the base artifact is unchanged |
| [`12-first-example-unaffected`](replay/12-first-example-unaffected/run_report.md) | tenant `first_example_cu` | `success`, no drift | The overlay applies only to its own tenant |

### Stability: the same inputs give the same result

Each scenario was replayed repeatedly against the same capability, with a fresh MockCore state and faults re-armed before every run. A run counts as identical only if its status, outcome or error category, recoveries, drift count and outputs all match.

| Scenario | Runs | Identical results |
|---|---|---|
| success (member 34567) | 15 | 15 |
| not found (99999) | 10 | 10 |
| permission denied (55555) | 8 | 8 |
| maintenance + modal | 10 | 10 |
| session timeout | 10 | 10 |
| slow pages (1.5 s) | 6 | 6 |
| Harbor Valley with overlay | 10 | 10 |
| app error (HTTP 500) | 5 | 5 |
| unknown dialog | 5 | 5 |
| slow pages + modal | 5 | 5 |
| **Total** | **84** | **84** |

## Human handoff: a person takes over the live session

This run was performed by hand on a Mac, with MockCore started with `--faults announcement`. The command was:

```
uv run cua replay mockcore/member.read_savings_balance --input member_id=12345 --human operator --escalate
```

| Run | Result |
|---|---|
| [`01-operator-takeover`](handoff/01-operator-takeover/run_report.md) | `success`, 1 intervention |

What the log shows, in order:

1. An unknown blocking dialog stopped the search step. An assist request appeared on the operator page, carrying a masked screenshot and the recent events.
2. The control lease went from automation to paused.
3. The operator pressed **Take control**: paused to human.
4. The operator clicked *Close* in the same browser window. This was recorded as a `human_action` attributed to the lease holder.
5. The operator handed back with *resume*: human to automation.
6. Automation checked whether the step was already done (it wasn't). It then re-ran the policy gate, since the page had changed under a person's hands, clicked Search, and finished by extracting the balance.

## Not included

- **Raw Playwright traces.** They record typed credentials and unmasked pages, so they are opt-in (`--debug-trace`), named `trace.UNREDACTED.zip`, and git-ignored.
- **Superseded runs.** Earlier runs recorded before the hardening pass (see `REPORT.md` §6–7) were replaced by these.
