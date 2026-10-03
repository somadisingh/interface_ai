# Run report — `run-20261003T034617-363d`

| | |
|---|---|
| Mode | replay |
| Capability | mockcore/member.read_savings_balance@1.0.0 |
| Content hash | `sha256:5f14b90b3b51db882c2f52fbb3f64d5fe5ab47a438968d29e8ffb4431b86f114` |
| Review status | draft |
| Status | **failed** |
| Error | `CHECKPOINT_FAILED` at step `open_search_button` — step 'open_search_button' did not reach its expected state |
| Duration | 12.3 s |
| Evidence | [`events.jsonl`](events.jsonl) · `screenshots/` · [`result.json`](result.json) |

## Failure detail

- **Category:** `CHECKPOINT_FAILED`
- **Step:** `open_search_button`
- **Expected:** {"target":"member_id_link"}
- **Observed:** [nav] MAIN MENU Home Member Search Teller Transactions Loan Servicing Reports User: SVC-CUA Sign Off \| [main] First Example Credit Union \| MEMBER INQUIRY Member ID: Last Name: Search Clear NO MEMBERS FOUND MATCHING SEARCH CRITERIA MockCore 4.2.1 (build 2211) Copyright (c) Fictional Core Systems Inc. Authorized use only.

<img src="screenshots/029-open_search_button-failure.png" width="480">

## Timeline

| # | t | who | what | step | detail | screen |
|---|---|---|---|---|---|---|
| 1 | 0.0s | system | Run started (replay) |  | mockcore/member.read_savings_balance@1.0.0 inputs: {'member_id': '***99'} |  |
| 2 | 0.0s | automation | Required capability started |  | mockcore/session.sign_on@1.0.0 |  |
| 3 | 0.3s | automation | Step completed | open_sign_on | Open the sign-on page | <img src="screenshots/003-open_sign_on.png" width="160"> |
| 4 | 0.6s | automation | Step completed | enter_user_id | Enter the service-account user id (located via anchor) | <img src="screenshots/007-enter_user_id.png" width="160"> |
| 5 | 0.8s | automation | Step completed | enter_password | Enter the service-account password (located via anchor) | <img src="screenshots/011-enter_password.png" width="160"> |
| 6 | 1.2s | automation | Step completed | submit | Submit the sign-on form (located via role) | <img src="screenshots/015-submit.png" width="160"> |
| 7 | 1.2s | automation | Required capability success |  | mockcore/session.sign_on@1.0.0 |  |
| 8 | 1.5s | automation | Step completed | open_member_search_link | Click Member Search to look up member {{inputs.member_id}} (located via role) | <img src="screenshots/021-open_member_search_link.png" width="160"> |
| 9 | 1.9s | automation | Step completed | enter_member_id_field | Enter member ID {{inputs.member_id}} in Member ID field (located via anchor) | <img src="screenshots/025-enter_member_id_field.png" width="160"> |
| 10 | 12.3s | system | Run finished: **failed** |  | CHECKPOINT_FAILED |  |

_Generated from `events.jsonl` by `cua report`; the log is the source of truth._
