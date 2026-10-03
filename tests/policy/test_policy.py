from __future__ import annotations

from pathlib import Path

import pytest

from cua.policy import MissingSecretError, Policy, PolicyEngine, Redactor, SecretStore, mask_value

ENGINE = PolicyEngine(Policy.load(Path(__file__).parents[2] / "policy.yaml"))


# ------------------------------------------------------------------ allowlist


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("http://127.0.0.1:8765/login", True),
        ("http://127.0.0.1:8765/members/12345/subaccounts/new", True),
        ("http://localhost:9000/notice?next=/home", True),
        ("http://127.0.0.1:8765/__admin/faults", False),  # denied path wins
        ("http://127.0.0.1:8765/reports/export", False),  # not allowlisted
        ("https://evil.example.com/login", False),  # foreign origin
        ("http://127.0.0.1.evil.example.com:8765/login", False),  # look-alike host
        ("about:blank", True),
    ],
)
def test_url_allowlist(url: str, allowed: bool) -> None:
    assert ENGINE.check_url(url).allowed is allowed
    assert (ENGINE.request_guard(url) is None) is allowed


# ------------------------------------------------------------------ risk classification


@pytest.mark.parametrize(
    ("action", "label", "risk"),
    [
        ("click", "Confirm & Open Account", "irreversible"),
        ("click", "Submit Account Opening", "irreversible"),  # tenant-b wording
        ("click", "Search", "safe"),
        ("click", "Sign On", "safe"),
        ("click", "Continue", "reversible_write"),
        ("click", "Add Share Account", "reversible_write"),
        ("fill", "Transfer amount", "safe"),  # typing never commits anything
        ("click", "Postal code lookup", "safe"),  # word boundary: not "post"
    ],
)
def test_risk_inferred_from_label(action: str, label: str, risk: str) -> None:
    assert ENGINE.classify(action, label) == risk  # type: ignore[arg-type]


def test_declared_risk_can_raise_but_never_lower_inferred_risk() -> None:
    assert ENGINE.classify("click", "Confirm & Open Account", "safe") == "irreversible"
    assert ENGINE.classify("click", "Go", "irreversible") == "irreversible"


# ------------------------------------------------------------------ tiered approval (D9)


def test_discovery_always_asks_before_irreversible_actions() -> None:
    d = ENGINE.check_action("click", label="Confirm & Open Account", mode="discovery")
    assert d.verdict == "require_approval" and d.risk == "irreversible"


def test_replay_of_draft_capability_asks_even_if_step_marked_auto_approve() -> None:
    d = ENGINE.check_action(
        "click",
        label="Confirm & Open Account",
        mode="replay",
        capability_approved=False,
        auto_approve=True,
    )
    assert d.verdict == "require_approval"


def test_replay_of_approved_capability_needs_step_level_pre_approval() -> None:
    no = ENGINE.check_action(
        "click", label="Confirm", mode="replay", capability_approved=True, auto_approve=False
    )
    yes = ENGINE.check_action(
        "click",
        label="Confirm",
        mode="replay",
        capability_approved=True,
        auto_approve=True,
        reviewer="sam",
    )
    assert no.verdict == "require_approval"
    assert yes.verdict == "allow" and "sam" in yes.reason


def test_disallowed_action_type_is_denied() -> None:
    strict = Policy.model_validate(
        ENGINE.policy.model_dump() | {"allowed_actions": ["click", "extract"]}
    )
    d = PolicyEngine(strict).check_action("fill", label="Member ID", mode="replay")
    assert d.verdict == "deny"


# ------------------------------------------------------------------ redaction


def test_redacts_secrets_pii_and_patterns() -> None:
    r = Redactor()
    r.add_secret("hunter2-pass")
    r.add_pii("12345")
    text = (
        "login pwd=hunter2-pass member 12345 ssn 900-00-1234 "
        "card 4111 1111 1111 1111 order 1234567890123 mail jane@example.com"
    )
    out = r.text(text)
    assert "hunter2-pass" not in out and "[SECRET]" in out
    assert "member ***45 " in out  # masked as a whole token only
    assert "900-00-1234" not in out
    assert "[CARD]" in out and "1234567890123" in out  # non-Luhn digit run left alone
    assert "jane@example.com" not in out


def test_redacts_nested_structures_and_credential_keys() -> None:
    r = Redactor()
    r.add_pii("SAMPLE, JANE Q")
    event = {
        "password": "anything",
        "api_key": "k",
        "access_token": "t",
        "usage": {"input_tokens": 1200, "output_tokens": 80},
        "detail": {"name": "SAMPLE, JANE Q"},
        "steps": ["ok", "ssn 900-00-9012"],
    }
    out = r.value(event)
    assert out["password"] == "[SECRET]" and out["api_key"] == "[SECRET]"
    assert out["access_token"] == "[SECRET]"
    assert out["usage"] == {"input_tokens": 1200, "output_tokens": 80}  # counts, not secrets
    assert out["detail"]["name"] == mask_value("SAMPLE, JANE Q")
    assert "900-00-9012" not in out["steps"][1]


def test_secret_store_registers_values_with_redactor() -> None:
    r = Redactor()
    store = SecretStore(r, {"MOCKCORE_PASSWORD": "s3cret-value"})
    assert store["MOCKCORE_PASSWORD"] == "s3cret-value"
    assert "s3cret-value" not in r.text("typed s3cret-value into the field")
    with pytest.raises(MissingSecretError):
        store["NOT_SET"]
    assert list(store) == []  # secrets can't be enumerated


def test_dom_patterns_cover_known_values_and_ssn() -> None:
    r = Redactor()
    r.add_pii("$1,520.33")
    patterns = r.dom_patterns()
    assert any(r"\$1,520\.33" in p for p in patterns)
    assert any("\\d{3}-\\d{2}-\\d{4}" in p for p in patterns)


# ------------------------------------------------------------------ enforced in the browser


def test_policy_is_enforced_by_the_browser(mockcore_url: str) -> None:
    from cua.surface.web import WebSurface

    with WebSurface.launch(mockcore_url, request_guard=ENGINE.request_guard) as s:
        assert s.navigate("/login").completed
        assert not s.navigate("/__admin/state").completed
        assert not s.navigate("http://example.com/").completed
        reasons = [b.reason for b in s.blocked]
        assert any("denied pattern" in r for r in reasons)
        assert any("not in the allowlist" in r for r in reasons)


def test_capability_refs_are_not_mistaken_for_email_addresses() -> None:
    assert Redactor().text("mockcore/session.sign_on@1.0.0") == "mockcore/session.sign_on@1.0.0"


def test_model_view_shares_secrets_but_not_pii() -> None:
    evidence = Redactor()
    model = evidence.model_view()
    evidence.add_pii("12345")
    evidence.add_secret("pw-added-later")
    assert model.text("member 12345") == "member 12345"
    assert "pw-added-later" not in model.text("typed pw-added-later")
    assert "900-00-1234" not in model.text("ssn 900-00-1234")
