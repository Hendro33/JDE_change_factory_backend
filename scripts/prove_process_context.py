#!/usr/bin/env python3
"""
Real-model demonstration: the refinement process analysis and the Architect
consuming BicycleWorks' process context through the intended runtime
(claude_agent_sdk -> Claude CLI, restricted tool surface). No scripted model.

    python3 scripts/prove_process_context.py [--out DIR]

In a throwaway data directory:
  1. The API starts in-process (same startup as uvicorn); the preview seeds
     add the SYNTHETIC framework fixture and the approved, unmapped story
     S-BW-WRITEOFF (stock write-off). JDE is SIMULATED.
  2. The REAL refinement agent (process/agent.py) reads the story and the
     framework through its run-bound jade-process tools and submits findings.
  3. A synthetic reviewer ("Proof Reviewer (synthetic)", product manager),
     signed in through the real API, confirms the suggested processes and
     applies every proposed finding as a new story revision (diff recorded).
  4. The REAL Architect (architecture_driver, unchanged) designs the story,
     calling get_process_context; the design baseline records the process
     fingerprint it rested on and the Architect's own process findings.

Writes <out>/trace.json and <out>/summary.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
parser = argparse.ArgumentParser()
parser.add_argument("--out", default=os.path.join(ROOT, "docs", "proof", "process_context_run"))
args = parser.parse_args()

DATA = tempfile.mkdtemp(prefix="jade-process-proof-")
for name, sub in (("JDE_API_DATA_DIR", "api"), ("JDE_BACKLOG_DIR", "backlog"), ("JDE_CHANGE_DIR", "changes"),
                  ("JDE_EVIDENCE_DIR", "evidence")):
    os.environ[name] = os.path.join(DATA, sub)
os.environ["JDE_CREDENTIAL_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
os.environ["JDE_MCP_MOCK_MODE"] = "true"
os.environ["JDE_COOKIE_SECURE"] = "false"
admin_pw, reviewer_pw = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
os.environ.update({"JDE_BOOTSTRAP_ADMIN_EMAIL": "admin@e2e.local", "JDE_BOOTSTRAP_ADMIN_NAME": "E2E Admin",
                   "JDE_BOOTSTRAP_ADMIN_PASSWORD": admin_pw, "JADE_E2E_CNC_PASSWORD": secrets.token_urlsafe(16),
                   "JADE_E2E_DO_PASSWORD": secrets.token_urlsafe(16)})
sys.path.insert(0, os.path.join(ROOT, "api_service"))

import claude_agent_sdk as sdk  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from jde_api_service.config import settings  # noqa: E402
from jde_api_service.main import app  # noqa: E402

COMPANY, STORY = "bwm", "S-BW-WRITEOFF"


def _blocks(content):
    return content if isinstance(content, list) else []


def make_observer(trace: list, rt: dict, real_query):
    async def observed_query(*, prompt, options):
        rt["options"] = {"allowed_tools": list(options.allowed_tools), "disallowed_tools": list(options.disallowed_tools),
                         "max_turns": options.max_turns, "model": options.model or "(CLI default)",
                         "mcp_servers": sorted((options.mcp_servers or {}).keys())}
        async for message in real_query(prompt=prompt, options=options):
            kind = type(message).__name__
            if kind == "SystemMessage" and getattr(message, "subtype", "") == "init":
                d = message.data
                rt["init"] = {"model": d.get("model"), "claude_code_version": d.get("claude_code_version"),
                              "tools": d.get("tools"), "mcp_servers": d.get("mcp_servers")}
            elif kind == "AssistantMessage":
                for b in _blocks(message.content):
                    if type(b).__name__ == "ToolUseBlock":
                        trace.append({"event": "tool_use", "id": b.id, "tool": b.name, "input": b.input})
                    elif type(b).__name__ == "TextBlock" and b.text.strip():
                        trace.append({"event": "text", "text": b.text[:800]})
            elif kind == "UserMessage":
                for b in _blocks(message.content):
                    if type(b).__name__ == "ToolResultBlock":
                        c = b.content if isinstance(b.content, str) else json.dumps(b.content)
                        trace.append({"event": "tool_result", "id": b.tool_use_id, "is_error": b.is_error, "content": c[:2500]})
            elif kind == "ResultMessage":
                rt["result"] = {"is_error": message.is_error, "num_turns": message.num_turns,
                                "duration_ms": message.duration_ms, "total_cost_usd": message.total_cost_usd,
                                "models": list((getattr(message, "model_usage", None) or {}).keys())}
            yield message
    return observed_query


def commit(path: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", path, "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


record: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "story": STORY,
                "commits": {"backend": commit(ROOT), "frontend": commit(os.path.join(ROOT, "..", "JDE_change_factory_frontend"))},
                "claude_cli": subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip(),
                "sdk_version": getattr(sdk, "__version__", "?")}
try:
    with TestClient(app) as client:
        # 1. seeds: synthetic framework, scope, profile, stories (scripted stand-ins only for OTHER stories)
        env = {**os.environ}
        for seed in ("technical", "process"):
            subprocess.run([sys.executable, os.path.join(ROOT, "scripts", f"seed_demo_{seed}.py"), COMPANY],
                           check=True, env=env, capture_output=True, text=True)
        from jde_api_service.services import auth_service, membership_service

        auth_service.create_user("reviewer@proof.local", reviewer_pw, "Proof Reviewer (synthetic)", user_id="u-proof-reviewer")
        membership_service.create_membership("u-proof-reviewer", COMPANY, ["product_manager"], created_by="u-proof")
        assert client.post("/auth/login", json={"email": "reviewer@proof.local", "password": reviewer_pw}).status_code == 200
        H = {"X-Customer-Id": COMPANY, "X-CSRF-Token": client.cookies.get(auth_service.CSRF_COOKIE_NAME)}
        record["story_before"] = client.get(f"/changes/{STORY}", headers=H).json()["userStory"]

        # 2. REAL refinement agent
        from jde_api_service.process import agent, story as story_process
        from jde_api_service.services.registry import get_change_service

        run = story_process.start_analysis(COMPANY, STORY, initiated_by="u-proof-reviewer")
        a_trace, a_rt = [], {}
        asyncio.run(agent.run_process_analysis(company_id=COMPANY, story_id=STORY, run_id=run["run_id"],
                                               change=get_change_service().get_for_customer(STORY, COMPANY),
                                               repo_root=settings.repo_root,
                                               observer=make_observer(a_trace, a_rt, sdk.query)))
        done = story_process.get_run(run["run_id"])
        record["analysis"] = {"run": done, "trace": a_trace, "runtime": a_rt}
        if done["status"] != "completed":
            raise RuntimeError(f"refinement analysis did not complete: {done['error']}")

        # 3. synthetic reviewer: confirm processes, apply every proposed finding
        suggested = done["result"].get("suggested_processes") or []
        body = ({"status": "confirmed", "refs": [{k: s[k] for k in ("framework_id", "version", "node_key", "rationale")} for s in suggested]}
                if suggested else {"status": "no_mapping", "noMappingReason": done["result"].get("no_mapping_reason") or "none suggested"})
        r = client.post(f"/changes/{STORY}/process/mapping", headers=H,
                        json={**body, "analysisRunId": run["run_id"], "note": "proof reviewer (synthetic identity)",
                              "expectedRevision": 0})
        assert r.status_code == 200, r.text
        record["mapping"] = r.json()["mapping"]
        ref = client.get(f"/changes/{STORY}/process/refinement", headers=H).json()
        ids = [f["finding_id"] for f in ref["findings"] if f["status"] == "proposed"]
        record["findings"] = ref["findings"]
        if ids:
            record["diff"] = client.post(f"/changes/{STORY}/process/refinement/preview", headers=H,
                                         json={"findingIds": ids}).json()["diff"]
            r = client.post(f"/changes/{STORY}/process/refinement/apply", headers=H,
                            json={"findingIds": ids, "note": "proof reviewer (synthetic identity)", "expectedRevision": 0})
            assert r.status_code == 200, r.text
            record["story_revisions"] = r.json()["revisions"]

        # 4. REAL Architect
        from jde_api_service.discovery import baseline
        from jde_api_service.services import architecture_driver
        from jde_api_service.services.registry import get_architecture_review_service

        d_trace, d_rt = [], {}
        real_query = sdk.query
        sdk.query = make_observer(d_trace, d_rt, real_query)
        try:
            asyncio.run(architecture_driver.run_architecture_review(
                story_id=STORY, repo_root=settings.repo_root, run_service=get_architecture_review_service(),
                customer_id=COMPANY, initiated_by="u-proof-reviewer"))
        finally:
            sdk.query = real_query
        arun = get_architecture_review_service().get(STORY)
        b = baseline.current_for_story(COMPANY, STORY)
        record["architect"] = {
            "stage": arun.stage, "error": arun.error, "trace": d_trace, "runtime": d_rt,
            "decision": arun.architect_decision.model_dump(mode="json") if arun.architect_decision else None,
            "spec": arun.implementation_spec.model_dump(mode="json") if arun.implementation_spec else None,
            "baseline": None if b is None else {"baseline_id": b["baseline_id"], "status": b["status"],
                                                "process_context": b["manifest"].get("process_context"),
                                                "gaps": b["manifest"].get("gaps")}}
        record["process_fingerprint_now"] = story_process.fingerprint(COMPANY, STORY)
finally:
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "trace.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, default=str)
    from render_process_proof import render  # noqa: E402

    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(render(record))
    print(os.path.join(args.out, "summary.md"))
