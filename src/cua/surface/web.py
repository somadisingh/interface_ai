"""Playwright implementation of the surface, for modern and legacy web apps.

Design notes:

* Uses Playwright's **sync** API. The whole system is single-process and step-by-step; the
  one concurrent piece (the operator page during a handoff) never touches the browser, so
  async would add complexity without buying anything.
* Frames are first-class: every target carries a frame path, and observation walks every
  frame, because legacy apps (framesets, iframes) are the common case.
* Resolution is **single-shot and strict**: a strategy counts only if it matches exactly one
  visible element. Waiting/retrying is the replay engine's job.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Frame,
    Locator,
    Page,
    Route,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from cua.handoff.control import SessionControl
from cua.schema import templates
from cua.schema.conditions import (
    AllOf,
    AnyOf,
    Condition,
    Hidden,
    OutputPresent,
    TextMatches,
    UrlMatches,
    Visible,
)
from cua.schema.locators import (
    AnchorLocator,
    AttributeLocator,
    CssLocator,
    FrameRef,
    LabelLocator,
    RoleLocator,
    TableCellLocator,
    TargetSpec,
    TextLocator,
)
from cua.schema.locators import (
    Locator as LocatorSpec,
)
from cua.surface import dom_scripts
from cua.surface.base import (
    ActionResult,
    ElementInfo,
    FrameText,
    Observation,
    Resolution,
    Resolved,
    StrategyAttempt,
    Unresolved,
)

TOP = "(top)"
_ROW = "tr:not(:has(tr))"  # innermost table row
_CONTROLS = "input:not([type=hidden]), select, textarea, button, a[href], [onclick]"
_ARIA_ROLES = {"textbox", "combobox", "link", "button", "checkbox", "radio", "cell", "tab"}
MASK_COLOR = "#2b2b2b"


@dataclass(frozen=True)
class BlockedRequest:
    url: str
    reason: str


class WebSurface:
    def __init__(self, page: Page, base_url: str, *, action_timeout_ms: int = 3000) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/") + "/"
        self.action_timeout_ms = action_timeout_ms
        self.blocked: list[BlockedRequest] = []
        self._ref_frames: dict[str, Frame] = {}
        self.control: SessionControl | None = None
        """Control lease; when set, every action requires automation to hold it."""

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    @contextmanager
    def launch(
        cls,
        base_url: str,
        *,
        headless: bool = True,
        request_guard: Callable[[str], str | None] | None = None,
        trace: bool = False,
        viewport: tuple[int, int] = (1280, 800),
    ) -> Iterator[WebSurface]:
        """Start a browser and yield a surface. ``request_guard(url)`` returns a reason to
        block a request (enforced by the browser for every request, not by agent logic)."""
        with sync_playwright() as pw:
            browser: Browser = pw.chromium.launch(headless=headless)
            context: BrowserContext = browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]}
            )
            if trace:
                context.tracing.start(screenshots=True, snapshots=True)
            surface = cls(context.new_page(), base_url)
            if request_guard is not None:
                surface.install_request_guard(request_guard)
            try:
                yield surface
            finally:
                browser.close()

    def install_request_guard(self, guard: Callable[[str], str | None]) -> None:
        def handle(route: Route) -> None:
            url = route.request.url
            reason = guard(url)
            if reason is None:
                route.continue_()
            else:
                self.blocked.append(BlockedRequest(url, reason))
                route.abort("blockedbyclient")

        self.page.context.route("**/*", handle)

    def install_human_recorder(self, on_action: Callable[[dict[str, Any]], None]) -> None:
        """Report clicks and changes made in any frame of the page to ``on_action``."""

        def binding(source: dict[str, Any], payload: dict[str, Any]) -> None:
            frame = source.get("frame")
            payload["frame"] = self.frame_label(frame) if frame is not None else "?"
            on_action(payload)

        context = self.page.context
        context.expose_binding("__cuaRecord", binding)
        context.add_init_script(script=dom_scripts.HUMAN_RECORDER)
        for frame in self._live_frames():  # frames already loaded
            try:
                frame.evaluate(dom_scripts.HUMAN_RECORDER)
            except PlaywrightError:
                continue

    def stop_trace(self, path: Path | None) -> Path | None:
        """Stop Playwright tracing; keep the trace only if a path is given."""
        try:
            if path is None:
                self.page.context.tracing.stop()
                return None
            self.page.context.tracing.stop(path=str(path))
            return path
        except PlaywrightError:
            return None

    # ------------------------------------------------------------------ frames

    @staticmethod
    def frame_label(frame: Frame) -> str:
        names: list[str] = []
        cur: Frame | None = frame
        while cur is not None and cur.parent_frame is not None:
            names.append(cur.name or f"frame@{cur.url}")
            cur = cur.parent_frame
        return "/".join(reversed(names)) or TOP

    def _frame(self, path: Sequence[FrameRef]) -> Frame | None:
        frame = self.page.main_frame
        for hop in path:
            nxt = None
            for child in frame.child_frames:
                if child.is_detached():  # stale frames linger after a reload
                    continue
                if hop.name and child.name == hop.name:
                    nxt = child
                    break
                if hop.url_pattern and re.search(hop.url_pattern, child.url):
                    nxt = child
                    break
            if nxt is None:
                return None
            frame = nxt
        return frame

    def _live_frames(self) -> list[Frame]:
        return [f for f in self.page.frames if not f.is_detached()]

    # ------------------------------------------------------------------ perception

    def current_url(self) -> str:
        return self.page.url

    def settle(self, timeout_ms: int = 5000) -> None:
        """Let navigations triggered by the last action finish in every frame."""
        for frame in self._live_frames():
            try:
                frame.wait_for_load_state("load", timeout=timeout_ms)
            except PlaywrightError:
                continue
        self.page.wait_for_timeout(100)

    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    def label_of(self, element: Resolved) -> str:
        """Visible label of a control (text, value of input buttons, aria-label)."""
        try:
            text = element.handle.inner_text(timeout=500).strip()
            if text:
                return text
            for attr in ("value", "aria-label", "title"):
                value = element.handle.get_attribute(attr, timeout=500)
                if value:
                    return value.strip()
        except PlaywrightError:
            pass
        return ""

    def text_excerpt(self, limit: int = 600) -> str:
        """Visible text of every frame, compacted (for 'observed' in failure reports)."""
        parts = []
        for frame in self._live_frames():
            try:
                text = " ".join(str(frame.evaluate(dom_scripts.BODY_TEXT)).split())
            except PlaywrightError:
                continue
            if text:
                parts.append(f"[{self.frame_label(frame)}] {text}")
        return " | ".join(parts)[:limit]

    def observe(self, *, screenshot: bool = False, max_per_frame: int = 150) -> Observation:
        elements: list[ElementInfo] = []
        frames: list[FrameText] = []
        self._ref_frames = {}
        for frame in self._live_frames():
            label = self.frame_label(frame)
            try:
                raw = frame.evaluate(
                    dom_scripts.OBSERVE, {"start": len(elements), "max": max_per_frame}
                )
                text = frame.evaluate(dom_scripts.BODY_TEXT)
            except PlaywrightError:
                continue  # frame navigated/detached mid-observation
            frames.append(FrameText(label, frame.url, str(text)[:4000]))
            for item in raw:
                info = ElementInfo(
                    ref=item["ref"],
                    frame=label,
                    tag=item["tag"],
                    role=item["role"],
                    name=item["name"],
                    text=item["text"],
                    value=item["value"],
                    row=item["row"],
                    column=item["column"],
                    disabled=item["disabled"],
                    checked=item["checked"],
                    options=tuple(item["options"]),
                )
                elements.append(info)
                self._ref_frames[info.ref] = frame
        shot = self.page.screenshot() if screenshot else None
        return Observation(
            url=self.page.url,
            title=self._safe_title(),
            frames=tuple(frames),
            elements=tuple(elements),
            screenshot_png=shot,
        )

    def _safe_title(self) -> str:
        try:
            return self.page.title()
        except PlaywrightError:
            return ""

    def ref_locator(self, ref: str) -> Resolved | None:
        """The element an observation numbered ``ref`` (valid until the next observe)."""
        frame = self._ref_frames.get(ref)
        if frame is None:
            return None
        loc = frame.locator(f'[data-cua-ref="{ref}"]')
        try:
            if loc.count() != 1:
                return None
        except PlaywrightError:
            return None
        return Resolved(handle=loc, strategy_index=0, strategy="ref")

    def resolve(self, target: TargetSpec, inputs: Mapping[str, str]) -> Resolution:
        frame = self._frame(target.frame)
        if frame is None:
            return Unresolved("frame_not_found")
        attempts: list[StrategyAttempt] = []
        for index, spec in enumerate(target.locators):
            try:
                loc = self._build(frame, spec, inputs)
                matches = loc.count() if loc is not None else -1
            except PlaywrightError:
                loc, matches = None, -1
            attempts.append(StrategyAttempt(spec.strategy, matches))
            if loc is not None and matches == 1:
                return Resolved(
                    handle=loc, strategy_index=index, strategy=spec.strategy, attempts=attempts
                )
        reason: Literal["not_found", "ambiguous"] = (
            "ambiguous" if any(a.matches > 1 for a in attempts) else "not_found"
        )
        return Unresolved(reason, attempts)

    def _build(self, frame: Frame, spec: LocatorSpec, inputs: Mapping[str, str]) -> Locator | None:
        def t(text: str) -> str:
            return templates.render(text, inputs, {})

        loc: Locator
        if isinstance(spec, RoleLocator):
            loc = frame.get_by_role(
                spec.role,  # type: ignore[arg-type]
                name=t(spec.name) if spec.name is not None else None,
                exact=spec.exact,
            )
        elif isinstance(spec, LabelLocator):
            loc = frame.get_by_label(t(spec.text), exact=spec.exact)
        elif isinstance(spec, AnchorLocator):
            anchor = frame.get_by_text(t(spec.anchor_text), exact=True)
            row = frame.locator(_ROW).filter(has=anchor)
            if spec.role == "cell":  # the value cell beside a label cell
                loc = row.locator("td, th").filter(has_not=anchor)
            elif spec.role:
                loc = row.get_by_role(spec.role)  # type: ignore[arg-type]
            else:
                loc = row.locator(_CONTROLS)
        elif isinstance(spec, TableCellLocator):
            token = secrets.token_hex(4)
            frame.evaluate(dom_scripts.CLEAR_MARKS, "data-cua-cell")
            frame.evaluate(
                dom_scripts.MARK_TABLE_CELL,
                {
                    "rowText": t(spec.row_text),
                    "columnHeader": t(spec.column_header),
                    "token": token,
                },
            )
            loc = frame.locator(f'[data-cua-cell="{token}"]')
        elif isinstance(spec, TextLocator):
            loc = frame.get_by_text(t(spec.text), exact=spec.exact)
        elif isinstance(spec, AttributeLocator):
            value = t(spec.value).replace("\\", "\\\\").replace('"', '\\"')
            loc = frame.locator(f'{spec.tag}[{spec.attribute}="{value}"]')
        elif isinstance(spec, CssLocator):
            loc = frame.locator(spec.selector)
        else:  # pragma: no cover - exhaustive
            return None
        return loc.filter(visible=True)

    def check(
        self,
        condition: Condition,
        targets: Mapping[str, TargetSpec],
        inputs: Mapping[str, str],
    ) -> bool:
        if isinstance(condition, AllOf):
            return all(self.check(c, targets, inputs) for c in condition.conditions)
        if isinstance(condition, AnyOf):
            return any(self.check(c, targets, inputs) for c in condition.conditions)
        if isinstance(condition, Visible):
            return isinstance(self.resolve(targets[condition.target], inputs), Resolved)
        if isinstance(condition, Hidden):
            res = self.resolve(targets[condition.target], inputs)
            return isinstance(res, Unresolved) and all(a.matches <= 0 for a in res.attempts)
        if isinstance(condition, TextMatches):
            pattern = re.compile(condition.pattern)
            return any(pattern.search(text) for text in self._texts(condition.frame))
        if isinstance(condition, UrlMatches):
            frame = self.page.main_frame if not condition.frame else self._frame(condition.frame)
            return frame is not None and re.search(condition.pattern, frame.url) is not None
        if isinstance(condition, OutputPresent):
            raise TypeError("output conditions are evaluated by the replay engine")
        raise TypeError(f"unsupported condition {condition!r}")  # pragma: no cover

    def _texts(self, path: Sequence[FrameRef] | None) -> list[str]:
        frames = self._live_frames() if path is None else [f for f in [self._frame(path)] if f]
        out: list[str] = []
        for frame in frames:
            try:
                out.append(str(frame.evaluate(dom_scripts.BODY_TEXT)))
            except PlaywrightError:
                continue
        return out

    # ------------------------------------------------------------------ actions

    def _act(self, fn: Callable[[], Any]) -> ActionResult:
        if self.control is not None:
            self.control.assert_automation()  # never act while a human holds the session
        try:
            fn()
            return ActionResult(True)
        except PlaywrightTimeout as exc:
            return ActionResult(False, "not_actionable", str(exc).splitlines()[0])
        except PlaywrightError as exc:
            message = str(exc).splitlines()[0]
            kind: Literal["detached", "error"] = (
                "detached" if "detached" in message or "Target closed" in message else "error"
            )
            return ActionResult(False, kind, message)

    def navigate(self, path_or_url: str) -> ActionResult:
        url = urljoin(self.base_url, path_or_url.lstrip("/"))
        return self._act(lambda: self.page.goto(url, wait_until="load"))

    def click(self, element: Resolved) -> ActionResult:
        return self._act(lambda: element.handle.click(timeout=self.action_timeout_ms))

    def fill(self, element: Resolved, value: str) -> ActionResult:
        def do() -> None:
            element.handle.fill(value, timeout=self.action_timeout_ms)
            # Commit the field (like a person tabbing out) so its change event fires now,
            # while automation still holds the lease, not later during a human's turn.
            element.handle.evaluate("el => el.blur()")

        return self._act(do)

    def select(self, element: Resolved, option: str) -> ActionResult:
        def do() -> None:
            try:
                element.handle.select_option(label=option, timeout=self.action_timeout_ms)
            except PlaywrightError:
                element.handle.select_option(value=option, timeout=self.action_timeout_ms)

        return self._act(do)

    def set_checked(self, element: Resolved, checked: bool) -> ActionResult:
        return self._act(
            lambda: element.handle.set_checked(checked, timeout=self.action_timeout_ms)
        )

    def press(self, key: str, element: Resolved | None = None) -> ActionResult:
        if element is None:
            return self._act(lambda: self.page.keyboard.press(key))
        return self._act(lambda: element.handle.press(key, timeout=self.action_timeout_ms))

    def read_text(self, element: Resolved) -> str:
        return str(element.handle.inner_text(timeout=self.action_timeout_ms)).strip()

    # ------------------------------------------------------------------ recording

    def describe_ref(self, ref: str, *, purpose: Literal["act", "read"] = "act") -> TargetSpec:
        """Turn an element the agent chose (by ref) into a locator bundle.

        Candidate strategies are generated most-semantic first, then **each is verified**
        against the live page: a strategy is kept only if it resolves to exactly this
        element. For ``purpose="read"`` (data to extract), strategies that key on the
        element's own text are skipped — that text is the data, and it will differ next
        time."""
        frame = self._ref_frames.get(ref)
        if frame is None:
            raise KeyError(f"unknown ref {ref!r} (observe first)")
        facts = frame.evaluate(dom_scripts.DESCRIBE, ref)
        if facts is None:
            raise KeyError(f"ref {ref!r} is no longer on the page")

        role, name, text = facts["role"], facts["name"], facts["text"]
        attrs = facts["attrs"]
        candidates: list[LocatorSpec] = []
        if purpose == "act" and role in _ARIA_ROLES and name and role != "cell":
            candidates.append(RoleLocator(role=role, name=name))
        if facts["label"]:
            candidates.append(LabelLocator(text=facts["label"]))
        if facts["anchor"]:
            anchor_role = role if role in _ARIA_ROLES else None
            candidates.append(AnchorLocator(anchor_text=facts["anchor"], role=anchor_role))
        if facts["table_cell"]:
            candidates.append(TableCellLocator(**facts["table_cell"]))
        if purpose == "act" and text:
            candidates.append(TextLocator(text=text))
        if attrs.get("name"):
            candidates.append(
                AttributeLocator(tag=facts["tag"], attribute="name", value=attrs["name"])
            )
        href = attrs.get("href")
        if href and not href.startswith("javascript:"):
            candidates.append(AttributeLocator(tag="a", attribute="href", value=href))
        if attrs.get("value"):
            candidates.append(
                AttributeLocator(tag="input", attribute="value", value=attrs["value"])
            )
        candidates.append(CssLocator(selector=facts["css"]))

        path = self._frame_path(frame)
        verified: list[LocatorSpec] = []
        for spec in candidates:
            probe = TargetSpec(description="probe", frame=path, locators=[spec])
            res = self.resolve(probe, {})
            if isinstance(res, Resolved) and self._is_ref(res.handle, ref):
                verified.append(spec)
        if not verified:
            raise LookupError(f"no locator strategy uniquely identifies {ref!r}")
        return TargetSpec(description=self._describe(facts, purpose), frame=path, locators=verified)

    @staticmethod
    def _describe(facts: dict[str, Any], purpose: Literal["act", "read"]) -> str:
        """Human-readable description that never embeds the data being read."""
        role, name, anchor, cell = facts["role"], facts["name"], facts["anchor"], None
        cell = facts["table_cell"]
        if purpose == "read":
            if cell:
                return f'"{cell["column_header"]}" value in the row for "{cell["row_text"]}"'
            if anchor:
                return f'value next to "{anchor}"'
            return f"{role} value"
        if name:
            return f'{role} "{name}"'
        if anchor:
            return f'{role} in the row labelled "{anchor}"'
        return f"{role} element"

    def _frame_path(self, frame: Frame) -> list[FrameRef]:
        hops: list[FrameRef] = []
        cur: Frame | None = frame
        while cur is not None and cur.parent_frame is not None:
            hops.append(
                FrameRef(name=cur.name) if cur.name else FrameRef(url_pattern=re.escape(cur.url))
            )
            cur = cur.parent_frame
        return list(reversed(hops))

    @staticmethod
    def _is_ref(handle: Locator, ref: str) -> bool:
        try:
            return handle.get_attribute("data-cua-ref") == ref
        except PlaywrightError:
            return False

    # ------------------------------------------------------------------ evidence

    def screenshot(
        self,
        path: Path,
        *,
        mask: Sequence[Resolved] = (),
        mask_text_patterns: Sequence[str] = (),
    ) -> Path:
        """Screenshot with sensitive regions painted over before the image is written.

        ``mask`` covers targets declared sensitive; ``mask_text_patterns`` (JS regex
        sources) covers any text that looks sensitive (SSNs, known PII values) wherever it
        appears, in every frame."""
        locators: list[Locator] = [m.handle for m in mask]
        if mask_text_patterns:
            for frame in self._live_frames():
                try:
                    if frame.evaluate(dom_scripts.MARK_TEXT_MATCHES, list(mask_text_patterns)):
                        locators.append(frame.locator("[data-cua-mask]"))
                except PlaywrightError:
                    continue
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), mask=locators, mask_color=MASK_COLOR)
        return path
