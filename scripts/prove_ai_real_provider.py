#!/usr/bin/env python3
"""
REAL-provider test (billable; run only with approval): one synthetic
requirement with a synthetic PDF, refined by Jade's real agents on
Anthropic's API with a customer AI connection configured through the API
exactly as an Admin would. No JDE is contacted.

    JADE_PROOF_ANTHROPIC_API_KEY=... [JADE_PROOF_MODEL=claude-haiku-4-5] \\
        python3 scripts/prove_ai_real_provider.py [--out DIR]

Spend is capped by the connection's own limits: USD 0.50 per run and USD 1.00
for the month (the runtime stops at the per-run budget). Expected cost with
Claude Haiku 4.5: roughly USD 0.05-0.30 (one 1-token connection test plus one
Receive/Improve/Check refinement). The key is read from the environment,
removed from it at once, stored encrypted in a throw-away database and never
printed.

Pass criteria (all from the backend's own records, not from the model's text):
  * the connection test answered, from the configured model;
  * the run completed; the runtime reported ANTHROPIC_API_KEY as its key
    source and the configured model; provider = anthropic (not a test provider);
  * the run record names the assigned pack revisions and the document read;
  * the story's citations name sections Jade actually returned (verified).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _proof_ai  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--out", default=os.path.join(ROOT, "docs", "proof", "ai_real_provider"))
parser.add_argument("--rehearse", action="store_true",
                    help="run against JADE_AI_TEST_PROVIDER_URL (the loopback fake) to check the procedure; "
                         "the result is labelled a rehearsal and is NOT evidence about Anthropic")
args = parser.parse_args()

KEY = _proof_ai.require_key()
MODEL = os.environ.get(_proof_ai.MODEL_ENV, "claude-haiku-4-5")
if bool(os.environ.get("JADE_AI_TEST_PROVIDER_URL")) != args.rehearse:
    sys.exit("A real run must reach Anthropic (unset JADE_AI_TEST_PROVIDER_URL); a --rehearse run needs it set.")
EXPECTED_PROVIDER = "anthropic-test-provider" if args.rehearse else "anthropic"

DATA = tempfile.mkdtemp(prefix="jade-ai-real-")
for name, sub in (("JDE_API_DATA_DIR", "api"), ("JDE_BACKLOG_DIR", "backlog"), ("JDE_CHANGE_DIR", "changes"),
                  ("JDE_EVIDENCE_DIR", "evidence")):
    os.environ[name] = os.path.join(DATA, sub)
os.environ["JDE_CREDENTIAL_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
os.environ["JDE_MCP_MOCK_MODE"] = "true"
os.environ["JDE_COOKIE_SECURE"] = "false"
os.environ["JDE_BOOTSTRAP_ADMIN_EMAIL"] = "real-provider-proof@jade.invalid"
os.environ["JDE_BOOTSTRAP_ADMIN_NAME"] = "Proof Operator (synthetic)"
PW = base64.b64encode(os.urandom(18)).decode()
os.environ["JDE_BOOTSTRAP_ADMIN_PASSWORD"] = PW
sys.path.insert(0, os.path.join(ROOT, "api_service"))
sys.path.insert(0, os.path.join(ROOT, "api_service", "tests"))

from fastapi.testclient import TestClient  # noqa: E402

import _docs  # noqa: E402  (synthetic PDF builder)
from jde_api_service.main import app  # noqa: E402

PDF = _docs.pdf(["SYNTHETIC delivery standard, page 1: sales orders ship within two working days.",
                 "SYNTHETIC delivery standard, page 2: public holidays are not working days."])
report: dict = {"model": MODEL, "checks": {},
                "kind": "REHEARSAL against the loopback test provider -- not evidence about Anthropic" if args.rehearse
                else "REAL provider run (Anthropic API)"}


def check(name, ok):
    report["checks"][name] = bool(ok)
    print(("PASS " if ok else "FAIL ") + name, flush=True)


with TestClient(app) as c:
    c.post("/auth/login", json={"email": "real-provider-proof@jade.invalid", "password": PW}).raise_for_status()
    c.headers["X-CSRF-Token"] = c.cookies.get("jde_csrf")
    c.headers["X-Customer-Id"] = c.get("/session").json()["activeCustomerId"]
    r = c.post("/admin/customers", json={"name": "Real Provider Proof BV (synthetic)", "toolsRelease": "9.2.8",
                                         "environment": "JDV920"})
    r.raise_for_status()
    cid = r.json()["id"]
    c.headers["X-Customer-Id"] = cid

    c.put("/admin/ai/connection", json={"model": MODEL, "enabled": True, "documentPolicy": "permitted_content",
                                        "limits": {"max_usd_per_run": 0.5, "monthly_usd": 1.0, "max_turns": 30}}
          ).raise_for_status()
    c.put("/admin/ai/connection/credential", json={"apiKey": KEY}).raise_for_status()
    del KEY
    packs = c.get("/admin/ai/packs").json()["packs"]
    for role in ("receive-agent", "improve-agent", "check-agent"):
        rev = next(p for p in packs if p["packId"] == f"tpl-{role}")["revisions"][0]["revision"]
        c.put(f"/admin/ai/assignments/{role}", json={"packId": f"tpl-{role}", "revision": rev}).raise_for_status()

    t = c.post("/admin/ai/connection/test", json={"confirmBillable": True}).json()
    report["connection_test"] = {"outcome": t["outcome"], "detail": t["detail"]}
    check("connection test answered from the configured model", t["outcome"] == "ok" and MODEL in t["detail"])

    up = c.post("/change-requests/attachments", json={"filename": "delivery-standard.pdf",
                                                      "contentBase64": base64.b64encode(PDF).decode()}).json()
    for _ in range(60):
        if c.get(f"/change-requests/attachments/{up['id']}").json()["extractionStatus"] in ("ready", "failed"):
            break
        time.sleep(0.5)
    req = c.post("/change-requests", json={
        "title": "SYNTHETIC: promised delivery dates", "businessSource": "Business",
        "rawContent": "SYNTHETIC request: promised delivery dates are wrong around holidays. The attached delivery "
                      "standard describes the rule.", "attachmentIds": [up["id"]]}).json()
    started = time.time()
    c.post(f"/changes/{req['id']}/enhance").raise_for_status()
    change = {}
    while time.time() - started < 900:
        time.sleep(5)
        change = c.get(f"/changes/{req['id']}").json()
        if change.get("processingStage") in ("done", "failed"):
            break
    run = next(r for r in c.get("/admin/ai/runs").json() if r["driver"] == "orchestration_driver")
    report["run"] = {k: run[k] for k in ("run_id", "status", "provider", "configured_model", "reported_model",
                                         "credential_source", "connection_revision", "credential_revision", "packs",
                                         "knowledge", "usage", "cost_usd", "cost_basis", "error")}
    report["story"] = change.get("userStory")
    report["processing"] = {"stage": change.get("processingStage"), "error": change.get("processingError")}
    check("the refinement run completed", run["status"] == "completed")
    check("the runtime used this customer's API key (reported ANTHROPIC_API_KEY)",
          run["credential_source"] == "ANTHROPIC_API_KEY" and run["provider"] == EXPECTED_PROVIDER)
    check("the runtime reported the configured model", (run["reported_model"] or "").startswith(MODEL))
    check("the run record names the pack revisions", {p["role"] for p in run["packs"]} >= {"receive-agent", "improve-agent", "check-agent"})
    check("the document was read by the run", any(k.get("action") == "read" for k in run["knowledge"] or []))
    cites = (change.get("userStory") or {}).get("documentCitations") or []
    check("the story cites the document, and Jade verified the cited sections",
          bool(cites) and all(c["verified"] for c in cites))
    check("spend stayed within the per-run cap", (run["cost_usd"] or 0) <= 0.5)

report["all_passed"] = all(report["checks"].values())
os.makedirs(args.out, exist_ok=True)
with open(os.path.join(args.out, "result.json"), "w") as f:
    json.dump(report, f, indent=2, default=str)
print("ALL PASSED" if report["all_passed"] else "SOME CHECKS FAILED", "->", os.path.join(args.out, "result.json"))
sys.exit(0 if report["all_passed"] else 1)
