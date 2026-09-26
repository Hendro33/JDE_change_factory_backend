#!/usr/bin/env python3
"""
A FAKE Anthropic Messages API on 127.0.0.1 for demonstrations and tests.
Nothing it returns comes from a model: it scripts a refinement so the REAL
Jade backend and the REAL Claude Code CLI can be exercised end to end
without contacting Anthropic or spending money.

    python3 scripts/fake_anthropic_provider.py --port 8765 --log /tmp/fake-provider.jsonl
    JADE_AI_TEST_PROVIDER_URL=http://127.0.0.1:8765  (for the backend)

Script (per agent run):
  main agent, first turn   -> delegate to the improve-agent (Agent/Task tool)
  improve-agent            -> list_documents, then read_document on the first
                              readable one, then answer with the cite labels read
  main agent, after that   -> the fenced JSON summary Jade expects, citing
                              exactly what was read (and nothing if nothing was)

Every request is logged with the first characters of its key only, the
model and which tools were offered -- so a demo can show which customer key
and which model each request carried.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
LOG_PATH = None
MARK = "fixed Jade policy"


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "tool_result":
                out.extend(_texts(b.get("content")))
    return out


def _tool_results(messages) -> list[str]:
    out = []
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out.append("\n".join(_texts(b.get("content"))))
    return out


def _json_in(text: str):
    """The tool's JSON at the start of a result (the runtime may append its own notes after it)."""
    try:
        return json.JSONDecoder().raw_decode((text or "").lstrip())[0]
    except (ValueError, TypeError):
        return None


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            return self._json({"input_tokens": 50})
        if not path.startswith("/v1/messages"):
            return self._json({"type": "error", "error": {"type": "not_found_error", "message": "not found"}}, 404)
        tools = [t.get("name") for t in body.get("tools", []) if isinstance(t, dict)]
        agent_tool = next((t for t in body.get("tools", []) if isinstance(t, dict) and t.get("name") in ("Agent", "Task")), None)
        system = json.dumps(body.get("system", ""))
        messages = body.get("messages", [])
        subagent = MARK in system or MARK in json.dumps(messages[:1])
        key = self.headers.get("x-api-key") or ""
        entry = {"key_prefix": key[:24], "bearer": bool(self.headers.get("authorization")), "model": body.get("model"),
                 "subagent": subagent, "tools": tools,
                 "background_agents_offered": bool(agent_tool and "run_in_background" in
                                                   json.dumps(agent_tool.get("input_schema", {})))}
        blocks, stop = self._script(subagent, tools, messages)
        entry["n_tool_results"] = len(_tool_results(messages))
        entry["reply"] = [b.get("name") or "text" for b in blocks]
        if LOG_PATH:
            with LOCK, open(LOG_PATH, "a") as f:
                f.write(json.dumps(entry) + "\n")
        if not body.get("stream"):
            return self._json({"id": "msg_fake", "type": "message", "role": "assistant", "model": body.get("model"),
                               "content": blocks, "stop_reason": stop, "stop_sequence": None,
                               "usage": {"input_tokens": 500, "output_tokens": 80}})
        return self._stream(body.get("model"), blocks, stop)

    # -- the script -----------------------------------------------------------------
    def _script(self, subagent, tools, messages):
        results = _tool_results(messages)
        if subagent:
            if "mcp__jade-knowledge__list_documents" not in tools:
                return [{"type": "text", "text": "No document tools were available; nothing was read. CITES:"}], "end_turn"
            if not results:
                return [self._tool("mcp__jade-knowledge__list_documents", {})], "tool_use"
            listing = next((j for j in map(_json_in, results) if isinstance(j, dict) and "documents" in j), None)
            read = [j for j in map(_json_in, results) if isinstance(j, dict) and j.get("untrusted_evidence")]
            if not read and listing and "mcp__jade-knowledge__read_document" in tools:
                doc = next((d for d in listing["documents"] if d.get("readable")), None)
                if doc and len(results) == 1:
                    return [self._tool("mcp__jade-knowledge__read_document", {"document_id": doc["document_id"]})], "tool_use"
            cites = [s["cite"] for r in read for s in r.get("sections", [])]
            names = [d["filename"] for d in (listing or {}).get("documents", [])]
            unreadable = [d["filename"] + " (" + d.get("why_not", "") + ")" for d in (listing or {}).get("documents", [])
                          if not d.get("readable")]
            return [{"type": "text", "text": f"Documents listed: {names}. Not readable: {unreadable}. "
                                             f"CITES: {json.dumps(cites)}"}], "end_turn"
        # main agent
        task = next((n for n in ("Agent", "Task") if n in tools), None)
        if task and not results:
            return [self._tool(task, {"description": "refine with documents", "subagent_type": "improve-agent",
                                      "prompt": "Refine the request. Use list_documents and read what is readable."})], "tool_use"
        joined = "\n".join(results)
        m = re.search(r"CITES: (\[.*\])", joined)  # one line; the labels themselves contain brackets
        try:
            cites = json.loads(m.group(1)) if m else []
        except ValueError:
            cites = []
        citations = [{"claim": f"Stated in the attached document ({c})", "source": c} for c in cites[:4]]
        summary = {
            "story_id": "", "user_story": {
                "statement": "As a sales planner I want promised delivery dates to count working days only so that "
                             "customers get dates we can keep",
                "business_context": "SCRIPTED BY THE FAKE TEST PROVIDER -- not written by a model. "
                                    + ("It used the attached document." if cites else "No document text was read."),
                "acceptance_criteria": [{"id": "AC1", "text": "Weekends are not counted in the promised date",
                                         "verified_by": "T1"}],
                "test_script": [{"id": "T1", "action": "Enter an order on a Friday", "expected": "Date skips the weekend"}],
                "business_rules": [], "assumptions": [], "open_questions": ["Which holiday calendar applies?"],
                "quality_status": "needs_human_input", "revision_count": 0, "document_citations": citations},
            "business_impact": {"financial_impact": "", "operational_reach": "", "risk_compliance": "",
                                "strategic_alignment": "", "urgency": ""},
            "rough_complexity_signal": "Low", "check_outcome": "needs_human_input",
            "failed_criteria": ["Scripted demo run: a person must review"]}
        return [{"type": "text", "text": "```json\n" + json.dumps(summary) + "\n```"}], "end_turn"

    @staticmethod
    def _tool(name, args):
        return {"type": "tool_use", "id": f"toolu_{abs(hash((name, json.dumps(args)))) % 10**12}", "name": name,
                "input": args}

    # -- transport --------------------------------------------------------------------
    def _json(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _stream(self, model, blocks, stop):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        ev = [("message_start", {"type": "message_start", "message": {
            "id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 500, "output_tokens": 1}}})]
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                ev += [("content_block_start", {"type": "content_block_start", "index": i,
                                                "content_block": {"type": "text", "text": ""}}),
                       ("content_block_delta", {"type": "content_block_delta", "index": i,
                                                "delta": {"type": "text_delta", "text": b["text"]}})]
            else:
                ev += [("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}),
                       ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                           "type": "input_json_delta", "partial_json": json.dumps(b["input"])}})]
            ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
        ev += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                  "usage": {"output_tokens": 80}}),
               ("message_stop", {"type": "message_stop"})]
        for name, data in ev:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--log")
    a = ap.parse_args()
    LOG_PATH = a.log
    print(f"FAKE Anthropic provider (scripted, no model) on http://127.0.0.1:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Fake).serve_forever()
