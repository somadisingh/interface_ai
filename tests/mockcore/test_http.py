"""MockCore behaviour over HTTP: flows, validation, business outcomes and injected faults."""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from mockcore import Faults, MockCoreConfig, create_app
from tests.conftest import PASSWORD, USERNAME, logged_in_client


def _review_token(html: str) -> str:
    m = re.search(r'name="txn" value="(\w+)"', html)
    assert m, "review page should carry a one-time transaction token"
    return m.group(1)


# ---------------------------------------------------------------- auth


def test_bad_credentials_are_rejected() -> None:
    c = TestClient(create_app(MockCoreConfig()))
    r = c.post("/login", data={"uid": USERNAME, "pwd": "wrong"})
    assert "E001 INVALID USER ID OR PASSWORD" in r.text


def test_unauthenticated_pages_redirect_to_login() -> None:
    c = TestClient(create_app(MockCoreConfig()))
    r = c.get("/members/search")
    assert r.url.path == "/login"
    assert "SESSION HAS EXPIRED" in r.text


def test_app_shell_is_a_frameset_with_nav_and_main(client: TestClient) -> None:
    html = client.get("/app").text
    assert "<frameset" in html
    assert 'name="nav"' in html and 'name="main"' in html


def test_pages_have_no_ids_or_test_ids(client: TestClient) -> None:
    html = client.post("/members/search", data={"mid": "12345"}).text
    assert " id=" not in html
    assert "data-testid" not in html


# ---------------------------------------------------------------- search & detail


@pytest.mark.parametrize(
    ("form", "expected"),
    [
        ({"mid": "12345"}, "SAMPLE, JANE Q"),
        ({"lname": "sam"}, "SAMPLE, ROBERT K"),
        ({"mid": "99999"}, "NO MEMBERS FOUND MATCHING SEARCH CRITERIA"),
        ({"mid": "12a45"}, "E102 INVALID MEMBER NUMBER FORMAT"),
        ({}, "E100 ENTER AT LEAST ONE SEARCH CRITERION"),
    ],
)
def test_search_outcomes(client: TestClient, form: dict[str, str], expected: str) -> None:
    assert expected in client.post("/members/search", data=form).text


def test_member_detail_shows_masked_ssn_and_balance(client: TestClient) -> None:
    html = client.get("/members/12345").text
    assert "***-**-1234" in html
    assert "900-00-1234" not in html
    assert "$1,520.33" in html


def test_restricted_member_is_access_denied(client: TestClient) -> None:
    assert "ACCESS DENIED" in client.get("/members/55555").text


def test_unknown_member_record(client: TestClient) -> None:
    assert "E404 MEMBER RECORD NOT FOUND" in client.get("/members/00000").text


# ---------------------------------------------------------------- open sub-account


def _open(client: TestClient, **overrides: str) -> str:
    data = {"type": "S01", "nickname": "Rainy day", "deposit": "100.00", "fund": "S00"}
    data.update(overrides)
    return client.post("/members/12345/subaccounts/new", data=data).text


def test_open_subaccount_happy_path_is_irreversible(client: TestClient) -> None:
    token = _review_token(_open(client))
    html = client.post("/members/12345/subaccounts/confirm", data={"txn": token}).text
    assert "SHARE ACCOUNT OPENED SUCCESSFULLY" in html
    assert "CNF-100001" in html
    state = client.get("/__admin/state").json()
    assert state["balances"]["12345"]["S00"] == "1420.33"
    assert state["opened"][0]["confirmation"] == "CNF-100001"


def test_confirm_token_is_single_use(client: TestClient) -> None:
    token = _review_token(_open(client))
    client.post("/members/12345/subaccounts/confirm", data={"txn": token})
    again = client.post("/members/12345/subaccounts/confirm", data={"txn": token}).text
    assert "E299 DUPLICATE OR EXPIRED SUBMISSION" in again
    assert len(client.get("/__admin/state").json()["opened"]) == 1


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"type": ""}, "E210 SELECT AN ACCOUNT TYPE"),
        ({"nickname": "x" * 21}, "E211 NICKNAME MAX 20 CHARACTERS"),
        ({"deposit": "1.00"}, "E212 MINIMUM OPENING DEPOSIT IS $5.00"),
        ({"deposit": "99999"}, "E213 INSUFFICIENT FUNDS"),
        ({"fund": ""}, "E214 SELECT A FUNDING ACCOUNT"),
        ({"deposit": "ten"}, "E215 OPENING DEPOSIT MUST BE A DOLLAR AMOUNT"),
    ],
)
def test_open_subaccount_validation_errors(
    client: TestClient, overrides: dict[str, str], error: str
) -> None:
    html = _open(client, **overrides)
    assert error in html
    assert 'name="txn"' not in html


# ---------------------------------------------------------------- tenant variant


def test_variant_b_renames_labels_and_requires_consent() -> None:
    c = logged_in_client(MockCoreConfig(variant="b"))
    assert "Account Number:" in c.get("/members/search").text
    assert "Harbor Valley" in c.get("/home").text
    token = _review_token(_open(c))
    assert (
        "E220 MEMBER CONSENT REQUIRED"
        in c.post("/members/12345/subaccounts/confirm", data={"txn": token}).text
    )
    ok = c.post("/members/12345/subaccounts/confirm", data={"txn": token, "consent": "1"})
    assert "SHARE ACCOUNT OPENED SUCCESSFULLY" in ok.text


# ---------------------------------------------------------------- faults


def test_fault_spec_parsing() -> None:
    f = Faults.parse("maintenance, modal, slow=250, session_timeout=4, error500=/members")
    assert f.maintenance_notice and f.password_modal
    assert f.slow_ms == 250
    assert f.session_timeout_after == 4
    assert f.error_500_paths == ("/members",)
    with pytest.raises(ValueError):
        Faults.parse("gremlins")


def test_maintenance_interstitial_until_acknowledged(client: TestClient) -> None:
    client.post("/__admin/faults", json={"spec": "maintenance"})
    r = client.get("/members/search")
    assert r.url.path == "/notice"
    assert "SCHEDULED MAINTENANCE NOTICE" in r.text
    r = client.post("/notice/ack", data={"next": "/members/search"})
    assert r.url.path == "/members/search"
    assert client.get("/members/search").url.path == "/members/search"


def test_password_modal_shows_once_per_session(client: TestClient) -> None:
    client.post("/__admin/faults", json={"spec": "modal"})
    assert "SECURITY NOTICE" in client.get("/home").text
    client.post("/modal/dismiss")
    assert "SECURITY NOTICE" not in client.get("/home").text


def test_session_timeout_mid_flow() -> None:
    c = logged_in_client(MockCoreConfig(faults=Faults(session_timeout_after=2)))
    assert c.get("/home").url.path == "/home"
    assert c.get("/members/search").url.path == "/members/search"
    r = c.get("/members/12345")
    assert r.url.path == "/login"
    assert "SESSION HAS EXPIRED" in r.text


def test_error_500_on_matching_paths(client: TestClient) -> None:
    client.post("/__admin/faults", json={"spec": "error500=/members/1"})
    assert client.get("/members/search").status_code == 200
    r = client.get("/members/12345")
    assert r.status_code == 500
    assert "CORE-E1203 UNEXPECTED APPLICATION ERROR" in r.text


def test_permission_denied_fault(client: TestClient) -> None:
    client.post("/__admin/faults", json={"spec": "permission_denied"})
    assert "ACCESS DENIED" in client.get("/members/12345").text


def test_slow_fault_delays_main_pages(client: TestClient) -> None:
    client.post("/__admin/faults", json={"spec": "slow=300"})
    start = time.monotonic()
    client.get("/home")
    assert time.monotonic() - start >= 0.3


def test_reset_restores_seed_and_clears_faults(client: TestClient) -> None:
    client.post("/__admin/faults", json={"spec": "maintenance"})
    client.post("/__admin/reset")
    assert client.get("/__admin/faults").json()["maintenance_notice"] is False
    fresh = TestClient(client.app)
    fresh.post("/login", data={"uid": USERNAME, "pwd": PASSWORD})
    assert fresh.get("/home").url.path == "/home"
