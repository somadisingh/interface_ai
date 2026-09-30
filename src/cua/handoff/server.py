"""Minimal operator surface: a local web page listing intervention requests, with the controls
to take over the live session and hand it back.

Scope (deliberate): this is a local page next to a visible browser window. It shows the
request context and drives the control lease; the person operates the browser window itself.
Streaming the session to a remote operator is the production path (see REPORT.md).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from cua.handoff.control import InvalidTransition, SessionControl
from cua.handoff.queue import InterventionQueue, Ticket


class Decision(BaseModel):
    by: str = "operator"
    note: str | None = None


class HandBack(Decision):
    resolution: str = "resume"


def _ticket_json(t: Ticket) -> dict[str, Any]:
    return {
        "id": t.id,
        "kind": t.kind,
        "open": t.open,
        "taken_by": t.taken_by,
        "created_at": t.created_at.isoformat(timespec="seconds"),
        "request": t.request.__dict__,
        "resolution": t.resolution,
    }


def create_operator_app(
    control: SessionControl, queue: InterventionQueue, run_dir: Path
) -> FastAPI:
    app = FastAPI(title="cua operator", docs_url=None, redoc_url=None)

    def ticket(tid: str, kind: str) -> Ticket:
        t = queue.get(tid)
        if t is None:
            raise HTTPException(404, "no such request")
        if t.kind != kind:
            raise HTTPException(409, f"this is an {t.kind} request")
        if not t.open:
            raise HTTPException(409, "request already resolved")
        return t

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return _PAGE

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return {
            "controller": control.state,
            "holder": control.holder,
            "history": [
                {
                    "at": h.at.isoformat(timespec="seconds"),
                    "from": h.frm,
                    "to": h.to,
                    "by": h.by,
                    "reason": h.reason,
                }
                for h in control.history[-20:]
            ],
            "requests": [_ticket_json(t) for t in queue.all()],
        }

    @app.post("/api/requests/{tid}/approve")
    def approve(tid: str, body: Decision) -> dict[str, Any]:
        t = ticket(tid, "approval")
        queue.resolve(t.id, {"approved": True, "by": body.by, "note": body.note})
        return {"ok": True}

    @app.post("/api/requests/{tid}/reject")
    def reject(tid: str, body: Decision) -> dict[str, Any]:
        t = ticket(tid, "approval")
        queue.resolve(t.id, {"approved": False, "by": body.by, "note": body.note})
        return {"ok": True}

    @app.post("/api/requests/{tid}/take")
    def take(tid: str, body: Decision) -> dict[str, Any]:
        t = ticket(tid, "assist")
        try:
            control.transfer("human", by=body.by, reason="operator took control", request_id=t.id)
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc
        queue.mark_taken(t.id, body.by)
        return {"ok": True}

    @app.post("/api/requests/{tid}/handback")
    def handback(tid: str, body: HandBack) -> dict[str, Any]:
        t = ticket(tid, "assist")
        if body.resolution not in ("resume", "skip_step", "abort"):
            raise HTTPException(422, "resolution must be resume, skip_step or abort")
        if control.state == "human" and control.holder != body.by:
            raise HTTPException(409, f"session is held by {control.holder}")
        queue.resolve(t.id, {"resolution": body.resolution, "by": body.by, "note": body.note})
        return {"ok": True}

    @app.get("/evidence/{path:path}")
    def evidence(path: str) -> FileResponse:
        root = run_dir.resolve()
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target)

    return app


class OperatorServer:
    def __init__(self, app: FastAPI, host: str = "127.0.0.1", port: int = 8766) -> None:
        self.url = f"http://{host}:{port}"
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cua operator</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --ink:#1d2330; --muted:#667085; --line:#e4e7ec;
          --auto:#1f7a4d; --paused:#b54708; --human:#175cd3; --bad:#b42318; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
         background:var(--bg); color:var(--ink); }
  header { display:flex; gap:16px; align-items:center; padding:14px 20px;
           background:var(--card); border-bottom:1px solid var(--line); flex-wrap:wrap; }
  h1 { font-size:16px; margin:0; }
  .badge { padding:3px 10px; border-radius:999px; color:#fff; font-weight:600; }
  .automation { background:var(--auto); } .paused { background:var(--paused); }
  .human { background:var(--human); }
  main { max-width:1100px; margin:0 auto; padding:16px; display:grid; gap:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }
  .card.closed { opacity:.6; }
  .row { display:flex; gap:14px; flex-wrap:wrap; }
  .row img { max-width:520px; width:100%; border:1px solid var(--line); border-radius:6px; }
  .meta { color:var(--muted); font-size:12px; }
  dl { margin:0; display:grid; grid-template-columns:auto 1fr; gap:4px 10px; }
  dt { color:var(--muted); } dd { margin:0; word-break:break-word; }
  button { font:inherit; padding:6px 12px; border-radius:6px; border:1px solid var(--line);
           background:#fff; cursor:pointer; }
  button.primary { background:var(--human); color:#fff; border-color:var(--human); }
  button.danger { color:var(--bad); }
  .actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
  input { font:inherit; padding:5px 8px; border:1px solid var(--line); border-radius:6px; }
  table { border-collapse:collapse; width:100%; font-size:12px; }
  td { border-top:1px solid var(--line); padding:4px 6px; vertical-align:top; }
</style></head>
<body>
<header>
  <h1>cua operator</h1>
  <span>Session: <span id="ctl" class="badge">…</span> <span id="holder" class="meta"></span></span>
  <label class="meta">Operator name <input id="me" value="operator" size="12"></label>
</header>
<main>
  <section id="requests"></section>
  <section class="card"><strong>Control history</strong><table id="history"></table></section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const me = () => $("me").value || "operator";
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
                               body: JSON.stringify(Object.assign({by: me()}, body || {}))});
  if (!r.ok) alert((await r.json()).detail || r.statusText);
  refresh();
}
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function card(t, ctl) {
  const r = t.request, shot = r.screenshot ? `<img src="/evidence/${esc(r.screenshot)}" alt="screenshot at request time">` : "";
  let actions = "";
  if (t.open && t.kind === "approval") {
    actions = `<button class="primary" onclick="post('/api/requests/${t.id}/approve')">Approve</button>
               <button class="danger" onclick="post('/api/requests/${t.id}/reject', {note: prompt('Reason?') || ''})">Reject</button>`;
  } else if (t.open && t.kind === "assist") {
    actions = ctl.controller === "human"
      ? `<span class="meta">You control the browser window now. When done, hand back:</span>
         <button class="primary" onclick="post('/api/requests/${t.id}/handback', {resolution: 'resume', note: prompt('What did you do?') || ''})">Hand back &amp; resume</button>
         <button onclick="post('/api/requests/${t.id}/handback', {resolution: 'skip_step'})">Hand back &amp; skip step</button>
         <button class="danger" onclick="post('/api/requests/${t.id}/handback', {resolution: 'abort'})">Abort run</button>`
      : `<button class="primary" onclick="post('/api/requests/${t.id}/take')">Take control</button>
         <button class="danger" onclick="post('/api/requests/${t.id}/handback', {resolution: 'abort'})">Abort run</button>`;
  }
  const detail = t.kind === "approval"
    ? `<dt>Action</dt><dd>${esc(r.action)}</dd><dt>Risk</dt><dd>${esc(r.risk)}</dd><dt>Why</dt><dd>${esc(r.reason)}</dd>`
    : `<dt>Reason</dt><dd>${esc(r.reason)}</dd><dt>Message</dt><dd>${esc(r.message)}</dd>`;
  return `<div class="card ${t.open ? "" : "closed"}">
    <div><strong>${t.kind === "approval" ? "Approval needed" : "Help needed"}</strong>
      <span class="meta"> · ${esc(t.id)} · ${esc(t.created_at)} ${t.open ? "" : "· resolved " + esc(JSON.stringify(t.resolution))}</span></div>
    <div class="row"><dl><dt>Run</dt><dd>${esc(r.run_id)}</dd><dt>Capability / goal</dt><dd>${esc(r.subject)}</dd>
      <dt>Step</dt><dd>${esc(r.step)}</dd>${detail}<dt>Page</dt><dd>${esc(r.url)}</dd></dl>${shot}</div>
    <div class="actions">${actions}</div></div>`;
}
async function refresh() {
  const s = await (await fetch("/api/state")).json();
  $("ctl").textContent = s.controller; $("ctl").className = "badge " + s.controller;
  $("holder").textContent = s.controller === "human" ? "held by " + s.holder : "";
  $("requests").innerHTML = s.requests.length ? s.requests.map((t) => card(t, s)).join("")
    : '<div class="card meta">No intervention requests. Automation is running on its own.</div>';
  $("history").innerHTML = s.history.slice().reverse().map((h) =>
    `<tr><td>${esc(h.at)}</td><td>${esc(h.from)} → <b>${esc(h.to)}</b></td><td>${esc(h.by)}</td><td>${esc(h.reason)}</td></tr>`).join("");
}
refresh(); setInterval(refresh, 1500);
</script></body></html>
"""
