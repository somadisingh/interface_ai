"""MockCore FastAPI application (server-rendered, legacy-style HTML)."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mockcore.config import Faults, MockCoreConfig
from mockcore.data import (
    MAX_NICKNAME_LEN,
    MIN_OPENING_DEPOSIT,
    SUBACCOUNT_TYPES,
    Member,
    Store,
    fmt_money,
)

SESSION_COOKIE = "MCSESSID"
_MEMBER_ID_RE = re.compile(r"^\d{5}$")
_MONEY_RE = re.compile(r"^\$?\s*(\d{1,7}(?:,\d{3})*|\d+)(?:\.(\d{1,2}))?$")


@dataclass
class Session:
    token: str
    user: str
    last_seen: float
    requests: int = 0
    notice_acknowledged: bool = False
    modal_dismissed: bool = False
    announcement_dismissed: bool = False
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)


def parse_money(raw: str) -> Decimal | None:
    m = _MONEY_RE.match(raw.strip())
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", "") + "." + (m.group(2) or "00"))
    except InvalidOperation:
        return None


def create_app(config: MockCoreConfig | None = None) -> FastAPI:
    cfg = config or MockCoreConfig.from_env()
    app = FastAPI(title="MockCore", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["money"] = fmt_money
    store = Store()
    sessions: dict[str, Session] = {}
    app.state.config = cfg
    app.state.store = store
    app.state.sessions = sessions
    app.state.timeout_fired = False

    # ------------------------------------------------------------------ helpers

    def render(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        session = _current_session(request)
        ctx.update(
            L=cfg.labels,
            product_version=cfg.labels["product_version"],
            show_modal=bool(
                cfg.faults.password_modal and session is not None and not session.modal_dismissed
            ),
            show_announcement=bool(
                cfg.faults.announcement
                and session is not None
                and not session.announcement_dismissed
                and request.url.path.startswith("/members")
            ),
        )
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def _current_session(request: Request) -> Session | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        s = sessions.get(token)
        if s is None:
            return None
        if time.monotonic() - s.last_seen > cfg.idle_timeout_s:
            sessions.pop(token, None)
            return None
        return s

    def _expired() -> RedirectResponse:
        return RedirectResponse("/login?expired=1", status_code=303)

    async def guard(request: Request, *, main_page: bool = True) -> Session | Response:
        """Common checks for authenticated pages. Returns the session, or a response to
        send instead (login redirect, maintenance interstitial, injected error)."""
        faults = cfg.faults
        if main_page and faults.slow_ms:
            await asyncio.sleep(faults.slow_ms / 1000)
        session = _current_session(request)
        if session is None:
            return _expired()
        session.last_seen = time.monotonic()
        if main_page:
            session.requests += 1
            if (
                faults.session_timeout_after is not None
                and not app.state.timeout_fired
                and session.requests > faults.session_timeout_after
            ):
                app.state.timeout_fired = True  # fires once per arming of the fault
                sessions.pop(session.token, None)
                return _expired()
            path = request.url.path
            if any(path.startswith(p) for p in faults.error_500_paths):
                ref = secrets.token_hex(4).upper()
                return render(request, "error.html", status=500, reference=ref)
            if faults.maintenance_notice and not session.notice_acknowledged:
                return RedirectResponse(f"/notice?next={path}", status_code=303)
        return session

    def _member_or_error(request: Request, member_id: str) -> Member | Response:
        member = store.members.get(member_id)
        if member is None:
            return render(
                request,
                "message.html",
                title="MEMBER INQUIRY",
                error="E404 MEMBER RECORD NOT FOUND",
            )
        if member.restricted or cfg.faults.permission_denied:
            return render(
                request,
                "message.html",
                title="MEMBER INQUIRY",
                error="ACCESS DENIED - INSUFFICIENT PRIVILEGES (E403-17). CONTACT YOUR SUPERVISOR.",
            )
        return member

    # ------------------------------------------------------------------ auth

    @app.get("/", include_in_schema=False)
    async def root(request: Request) -> Response:
        return RedirectResponse("/app" if _current_session(request) else "/login", 303)

    @app.get("/login")
    async def login_form(request: Request, expired: int = 0) -> Response:
        msg = "YOUR SESSION HAS EXPIRED. PLEASE SIGN ON AGAIN." if expired else None
        return render(request, "login.html", message=msg)

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await request.form()
        user, pwd = str(form.get("uid", "")), str(form.get("pwd", ""))
        if not (
            secrets.compare_digest(user, cfg.username) and secrets.compare_digest(pwd, cfg.password)
        ):
            return render(request, "login.html", message="E001 INVALID USER ID OR PASSWORD")
        token = secrets.token_urlsafe(24)
        sessions[token] = Session(token=token, user=user, last_seen=time.monotonic())
        resp = RedirectResponse("/app", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
        return resp

    @app.get("/logout")
    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            sessions.pop(token, None)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ------------------------------------------------------------------ shell

    @app.get("/app")
    async def frameset(request: Request) -> Response:
        if _current_session(request) is None:
            return _expired()
        return render(request, "frameset.html")

    @app.get("/nav")
    async def nav(request: Request) -> Response:
        s = await guard(request, main_page=False)
        if isinstance(s, Response):
            return s
        return render(request, "nav.html", user=s.user)

    @app.get("/home")
    async def home(request: Request) -> Response:
        s = await guard(request)
        if isinstance(s, Response):
            return s
        return render(request, "home.html", user=s.user)

    @app.get("/notice")
    async def notice(request: Request, next: str = "/home") -> Response:
        if _current_session(request) is None:
            return _expired()
        return render(request, "notice.html", next=next if next.startswith("/") else "/home")

    @app.post("/notice/ack")
    async def notice_ack(request: Request) -> Response:
        s = _current_session(request)
        if s is None:
            return _expired()
        s.notice_acknowledged = True
        nxt = str((await request.form()).get("next", "/home"))
        return RedirectResponse(nxt if nxt.startswith("/") else "/home", status_code=303)

    @app.post("/modal/dismiss")
    async def modal_dismiss(request: Request) -> Response:
        s = _current_session(request)
        if s is not None:
            s.modal_dismissed = True
        return Response(status_code=204)

    @app.post("/announcement/dismiss")
    async def announcement_dismiss(request: Request) -> Response:
        s = _current_session(request)
        if s is not None:
            s.announcement_dismissed = True
        return Response(status_code=204)

    # ------------------------------------------------------------------ members

    @app.get("/members/search")
    async def search_form(request: Request) -> Response:
        s = await guard(request)
        if isinstance(s, Response):
            return s
        return render(request, "search.html", mid="", lname="", error=None, results=None)

    @app.post("/members/search")
    async def search(request: Request) -> Response:
        s = await guard(request)
        if isinstance(s, Response):
            return s
        form = await request.form()
        mid, lname = str(form.get("mid", "")).strip(), str(form.get("lname", "")).strip()
        error = None
        results: list[Member] | None = None
        if not mid and not lname:
            error = "E100 ENTER AT LEAST ONE SEARCH CRITERION"
        elif mid and not _MEMBER_ID_RE.match(mid):
            error = "E102 INVALID MEMBER NUMBER FORMAT - MUST BE 5 DIGITS"
        else:
            results = store.search(mid, lname)
            if not results:
                error = "NO MEMBERS FOUND MATCHING SEARCH CRITERIA"
        return render(request, "search.html", mid=mid, lname=lname, error=error, results=results)

    @app.get("/members/{member_id}")
    async def member_detail(request: Request, member_id: str) -> Response:
        s = await guard(request)
        if isinstance(s, Response):
            return s
        m = _member_or_error(request, member_id)
        if isinstance(m, Response):
            return m
        return render(request, "member.html", m=m)

    def _render_open_form(
        request: Request, m: Member, values: dict[str, str], errors: list[str]
    ) -> Response:
        return render(
            request,
            "open_subaccount.html",
            m=m,
            types=SUBACCOUNT_TYPES,
            values=values,
            errors=errors,
        )

    @app.get("/members/{member_id}/subaccounts/new")
    async def open_form(request: Request, member_id: str) -> Response:
        s = await guard(request)
        if isinstance(s, Response):
            return s
        m = _member_or_error(request, member_id)
        if isinstance(m, Response):
            return m
        return _render_open_form(request, m, {}, [])

    @app.post("/members/{member_id}/subaccounts/new")
    async def open_review(request: Request, member_id: str) -> Response:
        s = await guard(request)
        if isinstance(s, Response):
            return s
        m = _member_or_error(request, member_id)
        if isinstance(m, Response):
            return m
        form = await request.form()
        values = {k: str(form.get(k, "")).strip() for k in ("type", "nickname", "deposit", "fund")}
        errors: list[str] = []
        if values["type"] not in SUBACCOUNT_TYPES:
            errors.append("E210 SELECT AN ACCOUNT TYPE")
        if len(values["nickname"]) > MAX_NICKNAME_LEN:
            errors.append(f"E211 NICKNAME MAX {MAX_NICKNAME_LEN} CHARACTERS")
        funding = next((a for a in m.accounts if a.suffix == values["fund"]), None)
        if funding is None:
            errors.append("E214 SELECT A FUNDING ACCOUNT")
        deposit = parse_money(values["deposit"])
        if deposit is None:
            errors.append("E215 OPENING DEPOSIT MUST BE A DOLLAR AMOUNT")
        elif deposit < MIN_OPENING_DEPOSIT:
            errors.append(f"E212 MINIMUM OPENING DEPOSIT IS {fmt_money(MIN_OPENING_DEPOSIT)}")
        elif funding is not None and deposit > funding.balance:
            errors.append("E213 INSUFFICIENT FUNDS IN FUNDING ACCOUNT")
        if errors or deposit is None or funding is None:
            return _render_open_form(request, m, values, errors)
        token = secrets.token_hex(8)
        s.pending[token] = {
            "member_id": m.member_id,
            "type": values["type"],
            "nickname": values["nickname"],
            "deposit": deposit,
            "fund": funding.suffix,
        }
        return render(
            request,
            "review.html",
            m=m,
            p=s.pending[token],
            token=token,
            type_name=SUBACCOUNT_TYPES[values["type"]],
            funding=funding,
            require_consent=cfg.variant == "b",
            error=None,
        )

    @app.post("/members/{member_id}/subaccounts/confirm")
    async def open_confirm(request: Request, member_id: str) -> Response:
        s = await guard(request)
        if isinstance(s, Response):
            return s
        m = _member_or_error(request, member_id)
        if isinstance(m, Response):
            return m
        form = await request.form()
        token = str(form.get("txn", ""))
        pending = s.pending.get(token)
        if pending is None or pending["member_id"] != m.member_id:
            return render(
                request,
                "message.html",
                title="OPEN SHARE ACCOUNT",
                error="E299 DUPLICATE OR EXPIRED SUBMISSION - TRANSACTION NOT PROCESSED",
            )
        if cfg.variant == "b" and not form.get("consent"):
            funding = next(a for a in m.accounts if a.suffix == pending["fund"])
            return render(
                request,
                "review.html",
                m=m,
                p=pending,
                token=token,
                type_name=SUBACCOUNT_TYPES[pending["type"]],
                funding=funding,
                require_consent=True,
                error="E220 MEMBER CONSENT REQUIRED",
            )
        del s.pending[token]
        suffix, confirmation = store.open_subaccount(
            m, pending["type"], pending["nickname"], pending["deposit"], pending["fund"]
        )
        return render(
            request,
            "confirmation.html",
            m=m,
            suffix=suffix,
            confirmation=confirmation,
            type_name=SUBACCOUNT_TYPES[pending["type"]],
            deposit=pending["deposit"],
        )

    # ------------------------------------------------------------------ test/admin hooks
    # Not part of the "real" app surface: used by tests and demos to inject faults.
    # The automation's allowlist must never permit these paths.

    @app.get("/__admin/faults")
    async def get_faults() -> JSONResponse:
        return JSONResponse(cfg.faults.describe())

    @app.post("/__admin/faults")
    async def set_faults(request: Request) -> JSONResponse:
        body = await request.json()
        cfg.faults = Faults.parse(body.get("spec", ""))
        app.state.timeout_fired = False
        return JSONResponse(cfg.faults.describe())

    @app.post("/__admin/reset")
    async def reset() -> JSONResponse:
        store.reset()
        sessions.clear()
        cfg.faults = Faults()
        app.state.timeout_fired = False
        return JSONResponse({"ok": True})

    @app.get("/__admin/state")
    async def state() -> JSONResponse:
        return JSONResponse(
            {
                "variant": cfg.variant,
                "opened": store.opened,
                "balances": {
                    mid: {a.suffix: str(a.balance) for a in m.accounts}
                    for mid, m in store.members.items()
                },
            }
        )

    return app
