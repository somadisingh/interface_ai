"""Web surface against a live MockCore: observation, verified locator generation, strict
resolution with drift detection, conditions, actions, masking and the request guard."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

from cua.schema import load_capability
from cua.schema.conditions import Hidden, TextMatches, UrlMatches, Visible
from cua.schema.locators import CssLocator, FrameRef, TargetSpec, TextLocator
from cua.surface.base import Resolved, Unresolved
from cua.surface.web import MASK_COLOR, WebSurface
from mockcore import MockCoreConfig
from tests.conftest import PASSWORD, USERNAME, LiveServer

ROOT = Path(__file__).parents[2]
READ_BALANCE = load_capability(ROOT / "tests/fixtures/read_savings_balance.yaml")
SIGN_ON = load_capability(ROOT / "capabilities/mockcore/session.sign_on/1.0.0.yaml")
INPUTS = {"member_id": "12345"}


def resolved(surface: WebSurface, target: TargetSpec) -> Resolved:
    res = surface.resolve(target, INPUTS)
    assert isinstance(res, Resolved), res
    return res


def sign_on(surface: WebSurface) -> None:
    t = SIGN_ON.targets
    assert surface.navigate("/login").completed
    surface.fill(resolved(surface, t["user_id_field"]), USERNAME)
    surface.fill(resolved(surface, t["password_field"]), PASSWORD)
    surface.click(resolved(surface, t["sign_on_button"]))
    surface.page.wait_for_url("**/app")
    surface.page.wait_for_load_state("load")


def open_member(surface: WebSurface) -> None:
    t = READ_BALANCE.targets
    surface.click(resolved(surface, t["nav_member_search"]))
    surface.fill(_wait(surface, t["member_id_field"]), "12345")
    surface.click(resolved(surface, t["search_button"]))
    surface.click(_wait(surface, t["result_member_link"]))
    _wait(surface, t["savings_balance_cell"])


def _wait(surface: WebSurface, target: TargetSpec, timeout_ms: int = 5000) -> Resolved:
    for _ in range(timeout_ms // 100):
        res = surface.resolve(target, INPUTS)
        if isinstance(res, Resolved):
            return res
        surface.page.wait_for_timeout(100)
    raise AssertionError(f"{target.description} never resolved: {res}")


@pytest.fixture
def surface(mockcore_url: str) -> Iterator[WebSurface]:
    with WebSurface.launch(mockcore_url) as s:
        s.action_timeout_ms = 1000
        yield s


@pytest.fixture(scope="module")
def variant_b() -> Iterator[LiveServer]:
    server = LiveServer(MockCoreConfig(variant="b"))
    server.start()
    yield server
    server.stop()


# ------------------------------------------------------------------ observation


def test_observe_numbers_elements_in_every_frame(surface: WebSurface) -> None:
    sign_on(surface)
    obs = surface.observe(screenshot=True)
    assert {f.frame for f in obs.frames} >= {"(top)", "nav", "main"}
    nav_link = next(e for e in obs.elements if e.name == "Member Search")
    assert (nav_link.frame, nav_link.role) == ("nav", "link")
    assert len({e.ref for e in obs.elements}) == len(obs.elements)
    assert obs.screenshot_png and obs.screenshot_png.startswith(b"\x89PNG")


def test_observe_exposes_row_context_for_unlabelled_fields(surface: WebSurface) -> None:
    sign_on(surface)
    surface.click(resolved(surface, READ_BALANCE.targets["nav_member_search"]))
    _wait(surface, READ_BALANCE.targets["member_id_field"])
    obs = surface.observe()
    field = next(e for e in obs.elements if e.frame == "main" and e.role == "textbox")
    assert field.name == ""  # no accessible name: label is just a sibling cell
    assert field.row == "Member ID:"
    search = next(e for e in obs.elements if e.text == "Search")
    assert search.role == "clickable"  # a <span onclick>, not a button


# ------------------------------------------------------------------ locator generation


def test_describe_ref_keeps_only_strategies_that_uniquely_hit_the_element(
    surface: WebSurface,
) -> None:
    sign_on(surface)
    surface.click(resolved(surface, READ_BALANCE.targets["nav_member_search"]))
    _wait(surface, READ_BALANCE.targets["member_id_field"])
    obs = surface.observe()
    field = next(e for e in obs.elements if e.frame == "main" and e.role == "textbox")
    spec = surface.describe_ref(field.ref, purpose="act")
    assert [loc.strategy for loc in spec.locators][:2] == ["anchor", "attribute"]
    assert spec.frame == [FrameRef(name="main")]
    for loc in spec.locators:
        single = TargetSpec(description="x", frame=spec.frame, locators=[loc])
        res = surface.resolve(single, {})
        assert isinstance(res, Resolved)
        assert res.handle.get_attribute("name") == "mid"


def test_describe_ref_for_reading_never_keys_on_the_value(surface: WebSurface) -> None:
    sign_on(surface)
    open_member(surface)
    obs = surface.observe()
    cell = next(e for e in obs.elements if e.text == "$1,520.33")
    spec = surface.describe_ref(cell.ref, purpose="read")
    assert spec.locators[0].strategy == "table_cell"
    assert "1,520.33" not in spec.model_dump_json()


# ------------------------------------------------------------------ resolution


def test_reference_flow_resolves_with_preferred_strategies(surface: WebSurface) -> None:
    sign_on(surface)
    open_member(surface)
    cell = resolved(surface, READ_BALANCE.targets["savings_balance_cell"])
    assert cell.strategy_index == 0
    assert surface.read_text(cell) == "$1,520.33"


def test_second_tenant_variant_resolves_through_fallbacks(variant_b: LiveServer) -> None:
    """Variant b renames 'Member ID:' and 'Current Balance'. The same artifact still runs,
    via fallback strategies, and the fallback index is visible (drift signal)."""
    variant_b.reset()
    with WebSurface.launch(variant_b.url) as s:
        sign_on(s)
        t = READ_BALANCE.targets
        s.click(
            resolved(
                s,
                t["nav_member_search"].model_copy(
                    update={"locators": [TextLocator(text="Find Member")]}
                ),
            )
        )
        field = _wait(s, t["member_id_field"])
        assert (field.strategy_index, field.strategy) == (1, "attribute")
        assert field.attempts[0].matches == 0
        s.fill(field, "12345")
        s.click(
            resolved(
                s, t["search_button"].model_copy(update={"locators": [TextLocator(text="Find")]})
            )
        )
        s.click(_wait(s, t["result_member_link"]))
        cell = _wait(s, t["savings_balance_cell"])
        assert (cell.strategy_index, cell.strategy) == (1, "css")
        assert s.read_text(cell) == "$1,520.33"


def test_ambiguous_and_missing_targets_are_distinguished(surface: WebSurface) -> None:
    sign_on(surface)
    main = [FrameRef(name="main")]
    surface.click(resolved(surface, READ_BALANCE.targets["nav_member_search"]))
    _wait(surface, READ_BALANCE.targets["member_id_field"])
    ambiguous = surface.resolve(
        TargetSpec(description="x", frame=main, locators=[CssLocator(selector="input")]), {}
    )
    assert isinstance(ambiguous, Unresolved) and ambiguous.reason == "ambiguous"
    missing = surface.resolve(
        TargetSpec(description="x", frame=main, locators=[TextLocator(text="Nope")]), {}
    )
    assert isinstance(missing, Unresolved) and missing.reason == "not_found"
    no_frame = surface.resolve(
        TargetSpec(
            description="x", frame=[FrameRef(name="ghost")], locators=[TextLocator(text="x")]
        ),
        {},
    )
    assert isinstance(no_frame, Unresolved) and no_frame.reason == "frame_not_found"


# ------------------------------------------------------------------ conditions & actions


def test_conditions(surface: WebSurface) -> None:
    sign_on(surface)
    t = READ_BALANCE.targets
    main = [FrameRef(name="main")]
    assert surface.check(UrlMatches(pattern="/app$"), t, INPUTS)
    assert surface.check(TextMatches(pattern="Welcome", frame=main), t, INPUTS)
    assert not surface.check(
        TextMatches(pattern="Welcome", frame=[FrameRef(name="nav")]), t, INPUTS
    )
    assert surface.check(Visible(target="nav_member_search"), t, INPUTS)
    assert surface.check(Hidden(target="savings_balance_cell"), t, INPUTS)


def test_click_under_overlay_is_reported_not_actionable(
    live_mockcore: LiveServer, surface: WebSurface
) -> None:
    live_mockcore.set_faults("modal")
    sign_on(surface)
    menu_link = resolved(surface, READ_BALANCE.targets["nav_member_search"])
    assert surface.click(menu_link).completed  # nav frame is not covered
    home = TargetSpec(
        description="x",
        frame=[FrameRef(name="main")],
        locators=[TextLocator(text="SECURITY NOTICE")],
    )
    _wait(surface, home)
    blocked = TargetSpec(
        description="Clear", frame=[FrameRef(name="main")], locators=[TextLocator(text="Clear")]
    )
    result = surface.click(resolved(surface, blocked))
    assert not result.completed and result.kind == "not_actionable"


# ------------------------------------------------------------------ evidence & guard


def test_screenshot_paints_over_sensitive_targets_and_patterns(
    surface: WebSurface, tmp_path: Path
) -> None:
    sign_on(surface)
    open_member(surface)
    cell = resolved(surface, READ_BALANCE.targets["savings_balance_cell"])
    box = cell.handle.bounding_box()
    assert box is not None
    shot = surface.screenshot(
        tmp_path / "s.png", mask=[cell], mask_text_patterns=[r"\*\*\*-\*\*-\d{4}"]
    )
    img = Image.open(io.BytesIO(shot.read_bytes())).convert("RGB")
    center = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
    expected = tuple(int(MASK_COLOR[i : i + 2], 16) for i in (1, 3, 5))
    assert img.getpixel(center) == expected


def test_request_guard_blocks_navigation_in_the_browser(mockcore_url: str) -> None:
    def guard(url: str) -> str | None:
        return "admin endpoints are never allowed" if "/__admin" in url else None

    with WebSurface.launch(mockcore_url, request_guard=guard) as s:
        assert s.navigate("/login").completed
        result = s.navigate("/__admin/state")
        assert not result.completed
        assert s.blocked and s.blocked[0].reason == "admin endpoints are never allowed"
