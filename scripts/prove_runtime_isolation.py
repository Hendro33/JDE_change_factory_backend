#!/usr/bin/env python3
"""
Runtime proof (NO provider contact, NO cost): the REAL Claude Code CLI,
started through Jade's agent runtime (ai/runtime.py), uses each customer's
own key and model -- also when two customers' runs execute concurrently and
the host machine carries its own ANTHROPIC_API_KEY and OAuth token.

    python3 scripts/prove_runtime_isolation.py [--out DIR]

The Anthropic API is replaced by a local fake endpoint on 127.0.0.1 that
records every request's key header and model and answers "OK". The CLI,
the SDK, the per-run environment and configuration directory, the pack
snapshots and the run records are all real. What this does NOT show: a
request to api.anthropic.com (that is scripts/prove_ai_real_provider.py,
which needs an approved key and spends money).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
parser = argparse.ArgumentParser()
parser.add_argument("--out", default=os.path.join(ROOT, "docs", "proof", "ai_runtime_isolation"))
args = parser.parse_args()

DATA = tempfile.mkdtemp(prefix="jade-ai-isolation-")
os.environ["JDE_API_DATA_DIR"] = os.path.join(DATA, "api")
os.environ["JDE_CREDENTIAL_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
# The host machine's own credentials -- which must never be used.
HOST_KEY = "sk-ant-api03-HOST-MACHINE-KEY-must-never-be-used-0000"
os.environ["ANTHROPIC_API_KEY"] = HOST_KEY
os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "host-oauth-token-must-never-be-used"
os.environ["ANTHROPIC_MODEL"] = "claude-fable-5-1"
sys.path.insert(0, os.path.join(ROOT, "api_service"))

from jde_api_service.ai import connection as ai_connection  # noqa: E402
from jde_api_service.ai import packs as ai_packs  # noqa: E402
from jde_api_service.ai import runtime  # noqa: E402
from jde_api_service.persistence.db import connection as db, ensure_schema  # noqa: E402

REQUESTS: list[dict] = []
LOCK = threading.Lock()


class FakeAnthropic(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        with LOCK:
            REQUESTS.append({"path": self.path.split("?")[0], "x_api_key": self.headers.get("x-api-key"),
                             "authorization": self.headers.get("authorization"), "model": body.get("model"),
                             "stream": bool(body.get("stream")),
                             "system_has_pack_policy": "fixed Jade policy" in json.dumps(body.get("system", ""))
                             or "fixed Jade policy" in json.dumps(body.get("messages", ""))})
        if self.path.startswith("/v1/messages/count_tokens"):
            return self._json({"input_tokens": 12})
        if not self.path.startswith("/v1/messages"):
            return self._json({"error": "not found"}, 404)
        model = body.get("model")
        blob = json.dumps(body.get("messages", ""))
        tool_names = [t.get("name") for t in body.get("tools", []) if isinstance(t, dict)]
        subagent_request = "fixed Jade policy" in json.dumps(body.get("system", "")) + blob
        task_tool = next((n for n in ("Task", "Agent") if n in tool_names), None)
        with LOCK:
            REQUESTS[-1]["tools"] = tool_names
            REQUESTS[-1]["subagent_request"] = subagent_request
        if task_tool and not subagent_request and "tool_result" not in blob and "PROOF" in blob:
            # Main agent, first turn: delegate to the improve-agent (its pack).
            return self._stream(model, [{"type": "tool_use", "id": "toolu_fake1", "name": task_tool, "input": {
                "description": "proof", "prompt": "Reply with the single word OK.",
                "subagent_type": "improve-agent"}}], "tool_use")
        return self._stream(model, [{"type": "text", "text": "OK"}], "end_turn") if body.get("stream") else \
            self._json({"id": "msg_fake", "type": "message", "role": "assistant", "model": model,
                        "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
                        "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1}})

    def _stream(self, model, blocks, stop_reason):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        events = [("message_start", {"type": "message_start", "message": {
            "id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1}}})]
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                events += [("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "text", "text": ""}}),
                           ("content_block_delta", {"type": "content_block_delta", "index": i,
                                                    "delta": {"type": "text_delta", "text": b["text"]}})]
            else:
                events += [("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}),
                           ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                               "type": "input_json_delta", "partial_json": json.dumps(b["input"])}})]
            events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
        events += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason,
                                                                          "stop_sequence": None},
                                      "usage": {"output_tokens": 1}}),
                   ("message_stop", {"type": "message_stop"})]
        for name, data in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

    def _unused(self, body, model):
        if not body.get("stream"):
            return self._json({"id": "msg_fake", "type": "message", "role": "assistant", "model": model,
                               "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
                               "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1}})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "OK"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 1}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        for name, data in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

    def _json(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def configure(company_id: str, key: str, model: str) -> None:
    ai_connection.save(company_id, model=model, enabled=True, document_policy="metadata_only", limits=None,
                       expected_revision=None, actor="proof")
    ai_connection.save_credential(company_id, key, actor="proof")
    for role in ai_packs.ROLES:
        revs = [r["revision"] for p in ai_packs.list_packs(company_id) if p["packId"] == ai_packs.template_pack_id(role)
                for r in p["revisions"] if r["status"] == "published"]
        ai_packs.assign(company_id, role, ai_packs.template_pack_id(role), revs[0], actor="proof")


async def one_run(company_id: str) -> dict:
    seen: dict = {"company": company_id}
    async with runtime.agent_run(company_id=company_id, driver="isolation_proof", roles=["improve-agent"],
                                 story_id=f"PROOF-{company_id}") as run:
        seen["config_dir"] = run.config_dir
        opts = run.options(cwd=ROOT, permission_mode="dontAsk", allowed_tools=["Task"], max_turns=3,
                           subagents=["improve-agent"])
        async for ev in run.stream(f"PROOF run for {company_id}: delegate to the improve-agent, then reply OK.", opts):
            if ev.kind == "init":
                raw = ev.raw.data
                seen["init"] = {"apiKeySource": raw.get("apiKeySource"), "model": raw.get("model"),
                                "agents": raw.get("agents"), "mcp_servers": raw.get("mcp_servers"),
                                "tools": raw.get("tools")}
            elif ev.kind == "result":
                seen["result"] = {k: ev.data.get(k) for k in ("is_error", "text", "num_turns")}
        seen["run_id"] = run.run_id
    seen["config_dir_removed"] = not os.path.exists(seen["config_dir"])
    return seen


async def main() -> dict:
    ensure_schema()
    ai_packs.ensure_templates(ROOT)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ[ai_connection.TEST_PROVIDER_ENV] = f"http://127.0.0.1:{server.server_address[1]}"

    keys = {"cust-a": "sk-ant-api03-CUSTOMER-A-synthetic-key-aaaaaaaaaaaa",
            "cust-b": "sk-ant-api03-CUSTOMER-B-synthetic-key-bbbbbbbbbbbb"}
    models = {"cust-a": "claude-sonnet-5", "cust-b": "claude-haiku-4-5"}
    for c in keys:
        configure(c, keys[c], models[c])

    env_before = dict(os.environ)  # includes the host key and token set above
    runs = await asyncio.gather(one_run("cust-a"), one_run("cust-b"))
    env_unchanged = dict(os.environ) == env_before

    by_key: dict[str, set] = {}
    for r in REQUESTS:
        by_key.setdefault(r["x_api_key"], set()).add(r["model"])
    checks = {
        "two concurrent runs completed": all(r.get("result", {}).get("is_error") is False for r in runs),
        "runtime reported key source ANTHROPIC_API_KEY for both": all(
            r["init"]["apiKeySource"] == "ANTHROPIC_API_KEY" for r in runs),
        "runtime reported each customer's own model": all(r["init"]["model"] == models[r["company"]] for r in runs),
        "provider saw only the two customer keys": set(by_key) == set(keys.values()),
        "customer A's key was only ever sent with A's model": by_key.get(keys["cust-a"], set()) <= {models["cust-a"]},
        "customer B's key was only ever sent with B's model": by_key.get(keys["cust-b"], set()) <= {models["cust-b"]},
        "host machine key never sent": all(r["x_api_key"] != HOST_KEY for r in REQUESTS),
        "no OAuth bearer token sent": all(not r["authorization"] for r in REQUESTS),
        "separate per-run config directories": runs[0]["config_dir"] != runs[1]["config_dir"],
        "per-run config directories removed afterwards": all(r["config_dir_removed"] for r in runs),
        "backend process environment unchanged": env_unchanged,
        "the improve-agent ran with its published pack's instructions (pack footer seen by the provider)": any(
            r.get("subagent_request") for r in REQUESTS),
        "metadata-only document policy: no document-reading tool was offered to the model": all(
            "mcp__jade-knowledge__read_document" not in (r.get("tools") or []) for r in REQUESTS),
        "the subagent requests also used only the run's own customer key": all(
            r["x_api_key"] in keys.values() for r in REQUESTS if r.get("subagent_request")),
    }

    # Blocking: an unconfigured customer and a revoked key never start the runtime.
    count = len(REQUESTS)
    blocked = {}
    try:
        await one_run("cust-unconfigured")
    except runtime.AiNotConfigured as exc:
        blocked["unconfigured"] = str(exc)
    ai_connection.revoke_credential("cust-a", actor="proof")
    try:
        await one_run("cust-a")
    except runtime.AiNotConfigured as exc:
        blocked["revoked"] = str(exc)
    checks["unconfigured customer blocked before any request"] = "unconfigured" in blocked
    checks["revoked key blocked before any request"] = "revoked" in blocked
    checks["no request made by blocked runs"] = len(REQUESTS) == count

    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT run_id, company_id, status, configured_model, reported_model, "
                                              "credential_source, connection_revision, credential_revision, packs, "
                                              "cost_usd, cost_basis, error FROM ai_runs ORDER BY started_at")]
    server.shutdown()
    redacted = [{**r, "x_api_key": (r["x_api_key"] or "")[:22] + "…" if r["x_api_key"] else None,
                 "authorization": "PRESENT (redacted)" if r["authorization"] else None} for r in REQUESTS]
    return {"checks": checks, "all_passed": all(checks.values()), "runs": runs, "blocked": blocked,
            "provider_requests": redacted, "ai_runs": rows}


if __name__ == "__main__":
    result = asyncio.run(main())
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "result.json"), "w") as f:
        json.dump(result, f, indent=2, default=str)
    for name, ok in result["checks"].items():
        print(("PASS " if ok else "FAIL ") + name)
    print("ALL PASSED" if result["all_passed"] else "SOME CHECKS FAILED", "->", os.path.join(args.out, "result.json"))
    sys.exit(0 if result["all_passed"] else 1)
