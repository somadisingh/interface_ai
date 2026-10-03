# Run report — `run-20261003T034845-aa65`

| | |
|---|---|
| Mode | replay |
| Capability | mockcore/member.read_savings_balance@1.1.0 |
| Content hash | `sha256:e68a02aabedfb5bc8efe00fc675018e079028fabab1dfdbc2459bef4f0df174e` |
| Review status | draft |
| Status | **success** |
| Outputs | `{"savings_balance": {"amount": "*****33", "currency": "USD"}}` |
| Duration | 3.0 s |
| Evidence | [`events.jsonl`](events.jsonl) · `screenshots/` · [`result.json`](result.json) |

## Recoveries, drift and interventions

- Drift at step `open_member_search_link`: `member_search_link` resolved by fallback `attribute`
- Drift at step `enter_member_id_field`: `member_id_field` resolved by fallback `attribute`
- Drift at step `open_search_button`: `search_button` resolved by fallback `css`
- Drift at step `read_current_balance_value`: `current_balance_value` resolved by fallback `css`

## Timeline

| # | t | who | what | step | detail | screen |
|---|---|---|---|---|---|---|
| 1 | 0.0s | system | Run started (replay) |  | mockcore/member.read_savings_balance@1.1.0 inputs: {'member_id': '***45'} |  |
| 2 | 0.0s | automation | Required capability started |  | mockcore/session.sign_on@1.0.0 |  |
| 3 | 0.3s | automation | Step completed | open_sign_on | Open the sign-on page | <img src="screenshots/003-open_sign_on.png" width="160"> |
| 4 | 0.6s | automation | Step completed | enter_user_id | Enter the service-account user id (located via anchor) | <img src="screenshots/007-enter_user_id.png" width="160"> |
| 5 | 0.9s | automation | Step completed | enter_password | Enter the service-account password (located via anchor) | <img src="screenshots/011-enter_password.png" width="160"> |
| 6 | 1.3s | automation | Step completed | submit | Submit the sign-on form (located via role) | <img src="screenshots/015-submit.png" width="160"> |
| 7 | 1.3s | automation | Required capability success |  | mockcore/session.sign_on@1.0.0 |  |
| 8 | 1.4s | automation | Drift: fallback locator used | open_member_search_link | member_search_link via attribute (strategy #3) |  |
| 9 | 1.6s | automation | Step completed | open_member_search_link | Click Member Search to look up member {{inputs.member_id}} (located via attribute) | <img src="screenshots/022-open_member_search_link.png" width="160"> |
| 10 | 1.6s | automation | Drift: fallback locator used | enter_member_id_field | member_id_field via attribute (strategy #2) |  |
| 11 | 2.0s | automation | Step completed | enter_member_id_field | Enter member ID {{inputs.member_id}} in Member ID field (located via attribute) | <img src="screenshots/027-enter_member_id_field.png" width="160"> |
| 12 | 2.0s | automation | Drift: fallback locator used | open_search_button | search_button via css (strategy #2) |  |
| 13 | 2.3s | automation | Step completed | open_search_button | Click Search button to find member {{inputs.member_id}} (located via css) | <img src="screenshots/032-open_search_button.png" width="160"> |
| 14 | 2.6s | automation | Step completed | open_member_id_link | Click member link {{inputs.member_id}} to view details (located via role) | <img src="screenshots/036-open_member_id_link.png" width="160"> |
| 15 | 2.7s | automation | Drift: fallback locator used | read_current_balance_value | current_balance_value via css (strategy #2) |  |
| 16 | 2.7s | automation | Extracted `savings_balance` | read_current_balance_value | {'amount': '*****33', 'currency': 'USD'} |  |
| 17 | 3.0s | automation | Step completed | read_current_balance_value | Extract current balance of Share Savings account (located via css) | <img src="screenshots/041-read_current_balance_value.png" width="160"> |
| 18 | 3.0s | system | Run finished: **success** |  |  |  |

_Generated from `events.jsonl` by `cua report`; the log is the source of truth._
