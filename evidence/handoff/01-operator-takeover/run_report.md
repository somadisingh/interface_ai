# Run report — `run-20261003T151536-80e8`

| | |
|---|---|
| Mode | replay |
| Capability | mockcore/member.read_savings_balance@1.1.0 |
| Content hash | `sha256:22e1679fe00d0bc13cff00020e282ab80526b44c1fbce6d42ede5585d6e74afb` |
| Review status | draft |
| Status | **success** |
| Outputs | `{"savings_balance": {"amount": "*****33", "currency": "USD"}}` |
| Duration | 33.6 s |
| Evidence | [`events.jsonl`](events.jsonl) · `screenshots/` · [`result.json`](result.json) |

## Recoveries, drift and interventions

- Human intervention `hlp-20261003T151551-9af6`

## Timeline

| # | t | who | what | step | detail | screen |
|---|---|---|---|---|---|---|
| 1 | 0.0s | system | Run started (replay) |  | mockcore/member.read_savings_balance@1.1.0 inputs: {'member_id': '***45'} |  |
| 2 | 0.0s | automation | Required capability started |  | mockcore/session.sign_on@1.0.0 |  |
| 3 | 0.2s | automation | Step completed | open_sign_on | Open the sign-on page | <img src="screenshots/003-open_sign_on.png" width="160"> |
| 4 | 0.4s | automation | Step completed | enter_user_id | Enter the service-account user id (located via anchor) | <img src="screenshots/007-enter_user_id.png" width="160"> |
| 5 | 0.5s | automation | Step completed | enter_password | Enter the service-account password (located via anchor) | <img src="screenshots/011-enter_password.png" width="160"> |
| 6 | 0.7s | automation | Step completed | submit | Submit the sign-on form (located via role) | <img src="screenshots/015-submit.png" width="160"> |
| 7 | 0.7s | automation | Required capability success |  | mockcore/session.sign_on@1.0.0 |  |
| 8 | 0.9s | automation | Step completed | open_member_search_link | Click Member Search to find member {{inputs.member_id}}. (located via role) | <img src="screenshots/021-open_member_search_link.png" width="160"> |
| 9 | 1.1s | automation | Step completed | enter_member_id_field | Enter member ID {{inputs.member_id}} in the Member ID field. (located via anchor) | <img src="screenshots/025-enter_member_id_field.png" width="160"> |
| 10 | 13.8s | automation | Help requested | open_search_button | unknown_state: click could not be performed (not_actionable): Locator.click: Timeout 3000ms exceeded. | <img src="screenshots/028-open_search_button-failure.png" width="160"> |
| 11 | 13.8s | automation | Control: automation → paused |  | click could not be performed (not_actionable): Locator.click: Timeout 3000ms exceeded. |  |
| 12 | 17.6s | operator | Control: paused → human |  | operator took control |  |
| 13 | 21.3s | operator | Person clicked |  | 'Close' in main |  |
| 14 | 33.0s | operator | Control: human → automation |  | handed back: resume |  |
| 15 | 33.0s | operator | Handed back: resume |  | just closed the popup (1 recorded actions) |  |
| 16 | 33.3s | automation | Step completed | open_search_button | Click Search button to find the member. (located via text) | <img src="screenshots/036-open_search_button.png" width="160"> |
| 17 | 33.5s | automation | Step completed | open_member_id_link | Click on the member ID to view details. (located via role) | <img src="screenshots/040-open_member_id_link.png" width="160"> |
| 18 | 33.5s | automation | Extracted `savings_balance` | read_current_balance_value | {'amount': '*****33', 'currency': 'USD'} |  |
| 19 | 33.6s | automation | Step completed | read_current_balance_value | Extract the current balance of the member's Share Savings account. (located via table_cell) | <img src="screenshots/044-read_current_balance_value.png" width="160"> |
| 20 | 33.6s | system | Run finished: **success** |  |  |  |

_Generated from `events.jsonl` by `cua report`; the log is the source of truth._
