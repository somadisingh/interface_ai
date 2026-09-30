"""End-to-end smoke test: a real (headless) Chromium drives MockCore through its frameset.

This proves the target is automatable at all — frames, span "buttons", javascript: links —
and is the same surface the discovery agent and replay engine will use.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from tests.conftest import PASSWORD, USERNAME, LiveServer


def test_read_savings_balance_through_frames(mockcore_url: str) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(mockcore_url + "/login")
        page.locator("tr:not(:has(tr))", has_text="User ID:").locator("input").fill(USERNAME)
        page.locator("tr:not(:has(tr))", has_text="Password:").locator("input").fill(PASSWORD)
        page.get_by_role("button", name="Sign On").click()

        nav = page.frame_locator("frame[name=nav]")
        main = page.frame_locator("frame[name=main]")
        nav.get_by_role("link", name="Member Search").click()
        main.locator("tr:not(:has(tr))", has_text="Member ID:").locator("input").fill("12345")
        main.get_by_text("Search", exact=True).click()
        main.get_by_role("link", name="12345").click()

        row = main.locator("tr:not(:has(tr))", has_text="Share Savings")
        assert row.locator("td").nth(3).inner_text() == "$1,520.33"
        browser.close()


def test_modal_overlay_blocks_clicks_until_dismissed(
    live_mockcore: LiveServer, mockcore_url: str
) -> None:
    live_mockcore.set_faults("modal")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(mockcore_url + "/login")
        page.locator("tr:not(:has(tr))", has_text="User ID:").locator("input").fill(USERNAME)
        page.locator("tr:not(:has(tr))", has_text="Password:").locator("input").fill(PASSWORD)
        page.get_by_role("button", name="Sign On").click()
        main = page.frame_locator("frame[name=main]")
        assert main.get_by_text("SECURITY NOTICE").is_visible()
        with pytest.raises(PlaywrightTimeout):  # overlay intercepts the pointer
            main.get_by_text("No new messages.").click(timeout=1000)
        main.get_by_text("OK", exact=True).click()
        main.get_by_text("SECURITY NOTICE").wait_for(state="detached")
        browser.close()
