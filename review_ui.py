#!/usr/bin/env python3
"""
Local review console for the human decision points.

    python3 review_ui.py

...then open http://127.0.0.1:8765 in your browser.

WHAT THIS IS
This is a browser front-end for exactly the same functions
backlog_review.py calls -- backlog.approve/reject for stories. Exact
changes are shown read-only: approving or rejecting one needs an
authenticated approver whose role the company's approval policy
allows, which only Jade itself (api_service) can establish. It is a
nicer way to reach the same enforced code path, not a second path
around it. Anything the
gate refuses on the command line, it refuses here, for the same reason.

WHAT THIS IS DELIBERATELY NOT
- It does not run agents. That stays in Claude Code, where you can see
  what the agents actually did before you approve anything.
- It does not write to JDE. It has no JDE access of any kind.
- It cannot approve anything on its own, or on a schedule, or in bulk
  without a named person and (for rejections) a stated reason.
- It is not a workflow product. Section 18 of the design document puts
  role-based workflow at product-ready; this is the pilot-scale thing
  that makes the human review step usable by a non-technical approver.

SECURITY NOTE
It binds to 127.0.0.1 only -- reachable from your own machine, not
from the network. There is no login, because there is no remote
access. Do not change the bind address to 0.0.0.0 and expose this;
if you need multi-user access with real identity, that is the
product-ready workflow tool, not this script.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

EXACT_CHANGE_DECISIONS_MOVED = (
    "Exact-change decisions need an authenticated approver whose role the company's approval policy allows. This local tool has no login, so it cannot make them: approve or reject the change in Jade (Governance > Architecture Review)."
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "mcp_server"))

from jde_mcp_server import approval, backlog  # noqa: E402
from jde_mcp_server.evidence import verify_chain  # noqa: E402
from jde_mcp_server.config import settings  # noqa: E402

PORT = int(os.environ.get("JDE_REVIEW_UI_PORT", "8765"))


def _all_stories() -> list[dict]:
    if not os.path.isdir(backlog.BACKLOG_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(backlog.BACKLOG_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(backlog.BACKLOG_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
    return out


def _all_changes() -> list[dict]:
    if not os.path.isdir(approval.CHANGE_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(approval.CHANGE_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(approval.CHANGE_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
    return out


def _evidence_for(story_id: str) -> dict:
    path = os.path.join(settings.evidence_dir, f"{story_id}.json")
    entries = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
    return {"entries": entries, "chain": verify_chain(story_id)}


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JDE Change Factory — Review Console</title>
<style>
  :root{--ink:#1a1a1a;--muted:#666;--line:#e0e0e0;--bg:#fafafa;--brand:#FFCC00;
        --ok:#2e7d32;--okbg:#e8f5e9;--warn:#b26a00;--warnbg:#fff8e1;--stop:#c62828;--stopbg:#ffebee;}
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
       color:var(--ink);background:var(--bg)}
  header{background:var(--brand);padding:16px 24px;display:flex;align-items:center;
         justify-content:space-between;flex-wrap:wrap;gap:12px}
  header h1{margin:0;font-size:17px;letter-spacing:.2px}
  header .who{display:flex;align-items:center;gap:8px;font-size:13px}
  header input{padding:6px 10px;border:1px solid rgba(0,0,0,.25);border-radius:4px;font:inherit;font-size:13px}
  main{max-width:1000px;margin:0 auto;padding:24px}
  .tabs{display:flex;gap:4px;border-bottom:2px solid var(--line);margin-bottom:20px;flex-wrap:wrap}
  .tabs button{background:none;border:0;padding:10px 16px;font:inherit;cursor:pointer;
               border-bottom:3px solid transparent;color:var(--muted)}
  .tabs button.on{color:var(--ink);font-weight:600;border-bottom-color:var(--ink)}
  .tabs .count{background:var(--ink);color:#fff;border-radius:10px;padding:1px 7px;font-size:12px;margin-left:6px}
  .card{background:#fff;border:1px solid var(--line);border-radius:6px;padding:18px;margin-bottom:14px}
  .card h3{margin:0 0 4px;font-size:15px}
  .sid{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--muted)}
  .impact{margin:12px 0;border-collapse:collapse;width:100%;font-size:13.5px}
  .impact td{padding:5px 8px;border-bottom:1px solid #f0f0f0;vertical-align:top}
  .impact td:first-child{color:var(--muted);width:190px}
  .notstated{color:#999;font-style:italic}
  pre{background:#f6f6f6;border:1px solid var(--line);border-radius:4px;padding:12px;
      overflow-x:auto;font-size:12.5px;margin:10px 0}
  .pill{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;font-weight:600}
  .p-ok{background:var(--okbg);color:var(--ok)} .p-warn{background:var(--warnbg);color:var(--warn)}
  .p-stop{background:var(--stopbg);color:var(--stop)} .p-grey{background:#eee;color:var(--muted)}
  .actions{margin-top:14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .actions input{flex:1;min-width:220px;padding:8px 10px;border:1px solid #ccc;border-radius:4px;font:inherit;font-size:13.5px}
  button.go{background:var(--ok);color:#fff;border:0;padding:9px 16px;border-radius:4px;cursor:pointer;font:inherit;font-weight:600}
  button.no{background:#fff;color:var(--stop);border:1px solid var(--stop);padding:9px 16px;border-radius:4px;cursor:pointer;font:inherit;font-weight:600}
  button:disabled{opacity:.45;cursor:not-allowed}
  .empty{color:var(--muted);text-align:center;padding:40px;background:#fff;border:1px dashed var(--line);border-radius:6px}
  .note{background:var(--warnbg);border-left:3px solid var(--warn);padding:10px 14px;font-size:13.5px;margin-bottom:18px}
  .err{background:var(--stopbg);border-left:3px solid var(--stop);padding:10px 14px;font-size:13.5px;margin-bottom:14px}
  details{margin-top:10px} summary{cursor:pointer;font-size:13.5px;color:var(--muted)}
</style></head><body>
<header>
  <h1>JDE Change Factory — Review Console</h1>
  <div class="who"><label for="who">Approving as</label>
    <input id="who" placeholder="your name" autocomplete="name"></div>
</header>
<main>
  <div id="err"></div>
  <div class="note"><strong>You are the control here.</strong> Approving a story means it is worth
    doing. Approving a change means this exact operation is the right one. Nothing reaches JD Edwards
    without both, plus a final confirmation in Claude Code.</div>
  <div class="tabs">
    <button data-t="stories" class="on">Stories awaiting decision<span class="count" id="c-stories">0</span></button>
    <button data-t="changes">Changes awaiting approval<span class="count" id="c-changes">0</span></button>
    <button data-t="done">Decided &amp; closed</button>
  </div>
  <div id="view"></div>
</main>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s??"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let TAB = "stories", DATA = {stories:[], changes:[]};

$("#who").value = localStorage.getItem("jde_approver") || "";
$("#who").addEventListener("input", e => localStorage.setItem("jde_approver", e.target.value.trim()));

document.querySelectorAll(".tabs button").forEach(b =>
  b.onclick = () => { TAB = b.dataset.t;
    document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("on", x === b)); render(); });

async function load(){
  try{
    const r = await fetch("/api/state"); DATA = await r.json(); $("#err").innerHTML = "";
  }catch(e){ $("#err").innerHTML = '<div class="err">Lost contact with the review server. Is it still running in your terminal?</div>'; }
  $("#c-stories").textContent = DATA.stories.filter(s => s.status === "backlog").length;
  $("#c-changes").textContent = DATA.changes.filter(c => c.status === "pending").length;
  render();
}

async function act(url, body){
  const who = $("#who").value.trim();
  if(!who){ alert("Enter your name at the top first — every decision is recorded against a person."); return; }
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
                              body: JSON.stringify({...body, who})});
  const j = await r.json();
  if(!j.ok){ $("#err").innerHTML = '<div class="err"><strong>Refused:</strong> '+esc(j.error)+'</div>'; }
  else { $("#err").innerHTML = ""; }
  load();
}

function impactRows(bi){
  const keys = ["financial_impact","operational_reach","risk_compliance","strategic_alignment","urgency"];
  return keys.map(k => { const v = (bi||{})[k];
    const shown = (v && String(v).trim()) ? esc(v) : '<span class="notstated">not stated</span>';
    return `<tr><td>${k.replace(/_/g," ")}</td><td>${shown}</td></tr>`; }).join("");
}

function storyCard(s){
  return `<div class="card"><h3>${esc(s.user_story||"(no story text)")}</h3>
    <div class="sid">${esc(s.story_id)} &nbsp;·&nbsp; source: ${esc(s.source||"—")}
      &nbsp;·&nbsp; complexity signal: <strong>${esc(s.rough_complexity_signal||"Unknown")}</strong></div>
    <table class="impact">${impactRows(s.business_impact)}</table>
    <div class="actions">
      <input id="n-${esc(s.story_id)}" placeholder="reason (required to reject, optional to approve)">
      <button class="go" onclick="act('/api/approve-story',{story_id:'${esc(s.story_id)}',note:document.getElementById('n-${esc(s.story_id)}').value})">Approve</button>
      <button class="no" onclick="rej('${esc(s.story_id)}')">Reject</button>
    </div></div>`;
}
function rej(id){
  const n = document.getElementById("n-"+id).value.trim();
  if(!n){ alert("A rejection needs a reason — it's what tells the Improve Agent (or a person) what to fix."); return; }
  act("/api/reject-story", {story_id:id, note:n});
}

function changeCard(c){
  const op = c.operation||{};
  const rows = Object.entries(op).map(([k,v]) =>
    `<tr><td>${esc(k)}</td><td><strong>${esc(typeof v==="object"?JSON.stringify(v):v)}</strong></td></tr>`).join("");
  return `<div class="card"><h3>Exact change awaiting approval</h3>
    <div class="sid">${esc(c.change_id)} &nbsp;·&nbsp; story: ${esc(c.story_id)}
      &nbsp;·&nbsp; environment: <strong>${esc(c.environment)}</strong></div>
    <table class="impact">${rows}</table>
    <p style="font-size:13px;color:var(--muted);margin:8px 0 0">
      This is precisely what will run. If the operation differs from this by even one character
      when it executes, it will be refused automatically.</p>
    <p style="font-size:13px;margin:8px 0 0"><strong>Decide this in Jade</strong> (Governance &gt;
      Architecture Review): exact-change approval needs a signed-in approver whose role this
      company's approval policy allows.</p></div>`;
}

function pill(s){
  const m = {approved:"p-ok", resolved_without_change:"p-ok", rejected:"p-stop",
             backlog:"p-warn", pending:"p-warn"};
  return `<span class="pill ${m[s]||"p-grey"}">${esc(String(s).replace(/_/g," "))}</span>`;
}

async function showEvidence(id, el){
  const r = await fetch("/api/evidence?story_id="+encodeURIComponent(id));
  const j = await r.json();
  const ok = j.chain && j.chain.valid;
  el.innerHTML = `<p>${ok ? '<span class="pill p-ok">evidence chain intact</span>'
      : '<span class="pill p-stop">EVIDENCE CHAIN BROKEN — do not sign off</span>'}</p>
    <pre>${esc(JSON.stringify(j.entries, null, 2))}</pre>`;
}

function doneCard(s){
  const why = s.resolution_note || s.decision_note;
  return `<div class="card"><h3>${esc(s.user_story||s.story_id)}</h3>
    <div class="sid">${esc(s.story_id)} &nbsp;·&nbsp; ${pill(s.status)}
      ${s.decided_by||s.resolved_by ? "&nbsp;·&nbsp; by "+esc(s.decided_by||s.resolved_by) : ""}</div>
    ${why ? `<p style="font-size:13.5px;margin:10px 0 0">${esc(why)}</p>` : ""}
    <details><summary>Show evidence trail</summary><div id="ev-${esc(s.story_id)}">loading…</div></details>
    </div>`;
}

function render(){
  const v = $("#view");
  if(TAB === "stories"){
    const list = DATA.stories.filter(s => s.status === "backlog");
    v.innerHTML = list.length ? list.map(storyCard).join("")
      : '<div class="empty">No stories waiting. Run one through Receive → Improve → Check in Claude Code.</div>';
  } else if(TAB === "changes"){
    const list = DATA.changes.filter(c => c.status === "pending");
    v.innerHTML = list.length ? list.map(changeCard).join("")
      : '<div class="empty">No changes waiting. The Architect proposes these after a story is approved.</div>';
  } else {
    const list = DATA.stories.filter(s => s.status !== "backlog");
    v.innerHTML = list.length ? list.map(doneCard).join("")
      : '<div class="empty">Nothing decided yet.</div>';
    list.forEach(s => { const el = document.getElementById("ev-"+s.story_id);
      const d = el && el.closest("details");
      if(d) d.addEventListener("toggle", () => { if(d.open) showEvidence(s.story_id, el); }, {once:true}); });
  }
}
load(); setInterval(load, 5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._send(200, json.dumps({"stories": _all_stories(), "changes": _all_changes()}))
        if u.path == "/api/evidence":
            sid = (parse_qs(u.query).get("story_id") or [""])[0]
            return self._send(200, json.dumps(_evidence_for(sid)))
        return self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, json.dumps({"ok": False, "error": "bad request"}))

        who = (body.get("who") or "").strip()
        note = (body.get("note") or "").strip()
        if not who:
            return self._send(400, json.dumps({"ok": False, "error": "an approver name is required"}))

        try:
            p = urlparse(self.path).path
            if p == "/api/approve-story":
                backlog.approve(body["story_id"], who, note)
            elif p == "/api/reject-story":
                backlog.reject(body["story_id"], who, note)
            elif p in ("/api/approve-change", "/api/reject-change"):
                return self._send(200, json.dumps({"ok": False, "error": EXACT_CHANGE_DECISIONS_MOVED}))
            else:
                return self._send(404, json.dumps({"ok": False, "error": "not found"}))
            return self._send(200, json.dumps({"ok": True}))
        except (backlog.BacklogError, approval.ChangeApprovalError) as e:
            # The gate refused. Surface it verbatim -- exactly as the CLI would.
            return self._send(200, json.dumps({"ok": False, "error": str(e)}))
        except KeyError:
            return self._send(400, json.dumps({"ok": False, "error": "missing id"}))


if __name__ == "__main__":
    print(f"\n  Review console running at:  http://127.0.0.1:{PORT}")
    print("  (Your own machine only — not reachable from the network.)")
    print("  Press Ctrl+C to stop.\n")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
