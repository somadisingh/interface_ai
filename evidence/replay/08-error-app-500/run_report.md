# Run report — `run-20261003T151847-582c`

| | |
|---|---|
| Mode | replay |
| Capability | mockcore/member.read_savings_balance@1.1.0 |
| Content hash | `sha256:22e1679fe00d0bc13cff00020e282ab80526b44c1fbce6d42ede5585d6e74afb` |
| Review status | draft |
| Status | **failed** |
| Error | `APP_ERROR` at step `open_member_id_link` — Generic application error page |
| Duration | 2.3 s |
| Evidence | [`events.jsonl`](events.jsonl) · `screenshots/` · [`result.json`](result.json) |

## Failure detail

- **Category:** `APP_ERROR`
- **Step:** `open_member_id_link`
- **Expected:** 
- **Observed:** [nav] MAIN MENU Home Member Search Teller Transactions Loan Servicing Reports User: [SECRET] Sign Off \| [main] First Example Credit Union \| ERROR CORE-E1203 UNEXPECTED APPLICATION ERROR. The requested function could not be completed. Reference ID: 53B96D28 Please contact the help desk and provide the reference ID. MockCore 4.2.1 (build 2211) Copyright (c) Fictional Core Systems Inc. Authorized use only.

<img src="screenshots/033-open_member_id_link-failure.png" width="480">

## Timeline

| # | t | who | what | step | detail | screen |
|---|---|---|---|---|---|---|
| 1 | 0.0s | system | Run started (replay) |  | mockcore/member.read_savings_balance@1.1.0 inputs: {'member_id': '***45'} |  |
| 2 | 0.0s | automation | Required capability started |  | mockcore/session.sign_on@1.0.0 |  |
| 3 | 0.3s | automation | Step completed | open_sign_on | Open the sign-on page | <img src="screenshots/003-open_sign_on.png" width="160"> |
| 4 | 0.5s | automation | Step completed | enter_user_id | Enter the service-account user id (located via anchor) | <img src="screenshots/007-enter_user_id.png" width="160"> |
| 5 | 0.7s | automation | Step completed | enter_password | Enter the service-account password (located via anchor) | <img src="screenshots/011-enter_password.png" width="160"> |
| 6 | 1.0s | automation | Step completed | submit | Submit the sign-on form (located via role) | <img src="screenshots/015-submit.png" width="160"> |
| 7 | 1.0s | automation | Required capability success |  | mockcore/session.sign_on@1.0.0 |  |
| 8 | 1.3s | automation | Step completed | open_member_search_link | Click Member Search to find member {{inputs.member_id}}. (located via role) | <img src="screenshots/021-open_member_search_link.png" width="160"> |
| 9 | 1.6s | automation | Step completed | enter_member_id_field | Enter member ID {{inputs.member_id}} in the Member ID field. (located via anchor) | <img src="screenshots/025-enter_member_id_field.png" width="160"> |
| 10 | 1.9s | automation | Step completed | open_search_button | Click Search button to find the member. (located via text) | <img src="screenshots/029-open_search_button.png" width="160"> |
| 11 | 2.3s | system | Run finished: **failed** |  | APP_ERROR |  |

_Generated from `events.jsonl` by `cua report`; the log is the source of truth._
