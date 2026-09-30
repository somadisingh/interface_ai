"""In-memory fake member data for MockCore. Every name, number and SSN here is fictional
(900-series SSNs are never issued)."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Account:
    suffix: str  # credit-union style share suffix, e.g. "S00"
    type: str
    nickname: str
    balance: Decimal


@dataclass
class Member:
    member_id: str
    last_name: str
    first_name: str
    middle_initial: str
    ssn: str
    dob: str
    address: str
    restricted: bool = False  # employee / sensitive record: requires elevated access
    accounts: list[Account] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return f"{self.last_name.upper()}, {self.first_name.upper()} {self.middle_initial}"

    @property
    def masked_ssn(self) -> str:
        return f"***-**-{self.ssn[-4:]}"


SUBACCOUNT_TYPES: dict[str, str] = {
    "S01": "Money Market Share",
    "S05": "Holiday Club",
    "S20": "Share Certificate 12M",
    "S30": "Vacation Club",
}

MIN_OPENING_DEPOSIT = Decimal("5.00")
MAX_NICKNAME_LEN = 20


def _seed() -> dict[str, Member]:
    d = Decimal
    members = [
        Member(
            "12345",
            "Sample",
            "Jane",
            "Q",
            "900-00-1234",
            "1984-03-02",
            "12 Elm St, Springfield",
            accounts=[
                Account("S00", "Share Savings", "PRIMARY SAVINGS", d("1520.33")),
                Account("S10", "Share Draft Checking", "CHECKING", d("845.10")),
            ],
        ),
        Member(
            "23456",
            "Tester",
            "John",
            "A",
            "900-00-5678",
            "1990-11-17",
            "4 Oak Ave, Shelbyville",
            accounts=[
                Account("S00", "Share Savings", "PRIMARY SAVINGS", d("50.00")),
                Account("S10", "Share Draft Checking", "CHECKING", d("12.75")),
            ],
        ),
        Member(
            "34567",
            "Example",
            "Maria",
            "L",
            "900-00-9012",
            "1976-07-29",
            "88 Pine Rd, Capital City",
            accounts=[
                Account("S00", "Share Savings", "PRIMARY SAVINGS", d("10250.00")),
                Account("S01", "Money Market Share", "RAINY DAY", d("25000.00")),
            ],
        ),
        Member(
            "45678",
            "Sample",
            "Robert",
            "K",
            "900-00-3456",
            "1968-01-05",
            "3 Birch Ln, Ogdenville",
            accounts=[Account("S00", "Share Savings", "PRIMARY SAVINGS", d("0.00"))],
        ),
        Member(
            "55555",
            "Staff",
            "Employee",
            "R",
            "900-00-7777",
            "1988-05-20",
            "1 Branch Plaza, Springfield",
            restricted=True,
            accounts=[Account("S00", "Share Savings", "PRIMARY SAVINGS", d("3100.00"))],
        ),
    ]
    return {m.member_id: m for m in members}


_SEED = _seed()


class Store:
    """Mutable per-process data store. ``reset()`` restores the seed."""

    def __init__(self) -> None:
        self.members: dict[str, Member] = {}
        self.next_confirmation = 100001
        self.opened: list[dict[str, str]] = []
        self.reset()

    def reset(self) -> None:
        self.members = copy.deepcopy(_SEED)
        self.next_confirmation = 100001
        self.opened = []

    def search(self, member_id: str = "", last_name: str = "") -> list[Member]:
        results = list(self.members.values())
        if member_id:
            results = [m for m in results if m.member_id == member_id]
        if last_name:
            results = [m for m in results if m.last_name.lower().startswith(last_name.lower())]
        return results

    def open_subaccount(
        self, member: Member, type_code: str, nickname: str, deposit: Decimal, funding_suffix: str
    ) -> tuple[str, str]:
        """Irreversible: moves money and creates an account. Returns (suffix, confirmation #)."""
        funding = next(a for a in member.accounts if a.suffix == funding_suffix)
        funding.balance -= deposit
        existing = {a.suffix for a in member.accounts}
        suffix = type_code
        n = int(type_code[1:])
        while suffix in existing:
            n += 1
            suffix = f"S{n:02d}"
        member.accounts.append(
            Account(suffix, SUBACCOUNT_TYPES[type_code], nickname.upper(), deposit)
        )
        confirmation = f"CNF-{self.next_confirmation}"
        self.next_confirmation += 1
        self.opened.append(
            {"member_id": member.member_id, "suffix": suffix, "confirmation": confirmation}
        )
        return suffix, confirmation


def fmt_money(value: Decimal) -> str:
    return f"${value:,.2f}"
