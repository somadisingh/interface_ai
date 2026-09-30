"""MockCore configuration: tenant variants and runtime fault switches."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

# Two "tenants" running the same vendor product, configured and branded differently.
# Variant "b" renames a few labels, changes branding and runs a newer product build —
# the stand-in for per-tenant configuration / version drift.
VARIANTS: dict[str, dict[str, str]] = {
    "a": {
        "institution": "First Example Credit Union",
        "product_version": "MockCore 4.2.1 (build 2211)",
        "header_color": "#003366",
        "nav_member_search": "Member Search",
        "member_id_label": "Member ID:",
        "last_name_label": "Last Name:",
        "search_button": "Search",
        "open_subaccount_link": "Open Sub-Account",
        "continue_button": "Continue",
        "confirm_button": "Confirm & Open Account",
        "balance_column": "Current Balance",
    },
    "b": {
        "institution": "Harbor Valley Federal Credit Union",
        "product_version": "MockCore 4.3.0 (build 2305)",
        "header_color": "#5a2d0c",
        "nav_member_search": "Find Member",
        "member_id_label": "Account Number:",
        "last_name_label": "Surname:",
        "search_button": "Find",
        "open_subaccount_link": "Add Share Account",
        "continue_button": "Next",
        "confirm_button": "Submit Account Opening",
        "balance_column": "Balance",
    },
}


@dataclass(frozen=True)
class Faults:
    """Runtime exceptional states that can be injected.

    Parsed from a comma-separated spec, e.g.
    ``"maintenance,modal,slow=2000,session_timeout=5,error500=/members,permission_denied"``.
    """

    maintenance_notice: bool = False
    """Show a 'scheduled maintenance' interstitial once per session before main pages."""
    password_modal: bool = False
    """Overlay a 'password expires soon' modal once per session."""
    slow_ms: int = 0
    """Delay every main-frame page by this many milliseconds."""
    session_timeout_after: int | None = None
    """Expire the session after this many authenticated main-frame requests. Fires once per
    arming (setting the fault again re-arms it), so recovery by signing on again can work."""
    error_500_paths: tuple[str, ...] = ()
    """Return an application error page for paths starting with any of these prefixes."""
    permission_denied: bool = False
    """Deny access to member detail pages regardless of the member."""

    @classmethod
    def parse(cls, spec: str | None) -> Faults:
        if not spec:
            return cls()
        values: dict[str, object] = {}
        error_paths: list[str] = []
        for raw in spec.split(","):
            item = raw.strip()
            if not item:
                continue
            key, _, value = item.partition("=")
            key = key.strip().lower()
            if key == "maintenance":
                values["maintenance_notice"] = True
            elif key == "modal":
                values["password_modal"] = True
            elif key == "slow":
                values["slow_ms"] = int(value or "3000")
            elif key == "session_timeout":
                values["session_timeout_after"] = int(value or "3")
            elif key == "error500":
                error_paths.append(value or "/")
            elif key == "permission_denied":
                values["permission_denied"] = True
            else:
                raise ValueError(f"Unknown MockCore fault: {key!r}")
        if error_paths:
            values["error_500_paths"] = tuple(error_paths)
        return replace(cls(), **values)  # type: ignore[arg-type]

    def describe(self) -> dict[str, object]:
        return {
            "maintenance_notice": self.maintenance_notice,
            "password_modal": self.password_modal,
            "slow_ms": self.slow_ms,
            "session_timeout_after": self.session_timeout_after,
            "error_500_paths": list(self.error_500_paths),
            "permission_denied": self.permission_denied,
        }


@dataclass
class MockCoreConfig:
    variant: str = "a"
    faults: Faults = field(default_factory=Faults)
    username: str = "operator"
    password: str = "mockcore-demo"  # fake, local-only credential for a fake app
    idle_timeout_s: int = 15 * 60

    @property
    def labels(self) -> dict[str, str]:
        return VARIANTS[self.variant]

    @classmethod
    def from_env(cls) -> MockCoreConfig:
        variant = os.environ.get("MOCKCORE_VARIANT", "a").lower()
        if variant not in VARIANTS:
            raise ValueError(
                f"Unknown MOCKCORE_VARIANT {variant!r}; expected one of {list(VARIANTS)}"
            )
        return cls(
            variant=variant,
            faults=Faults.parse(os.environ.get("MOCKCORE_FAULTS")),
            username=os.environ.get("MOCKCORE_USERNAME", "operator"),
            password=os.environ.get("MOCKCORE_PASSWORD") or "mockcore-demo",
        )
