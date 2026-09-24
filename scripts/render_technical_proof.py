"""Renders docs/proof/technical_agent_run/summary.md from the recorded run."""

from __future__ import annotations

import json


def _table(trace: list[dict]) -> list[str]:
    results = {t["id"]: t for t in trace if t["event"] == "tool_result"}
    lines = ["| # | Tool | In subagent | Input | Result (start) |", "|---|---|---|---|---|"]
    for i, u in enumerate([t for t in trace if t["event"] == "tool_use"], 1):
        res = results.get(u["id"], {})
        snippet = (res.get("content") or "").replace("|", "\\|").replace("\n", " ")[:180]
        inp = json.dumps(u["input"])[:140].replace("|", "\\|")
        lines.append(f"| {i} | `{u['tool']}` | {'yes' if u.get('in_subagent') else ''} | `{inp}` | "
                     f"{'ERROR ' if res.get('is_error') else ''}{snippet} |")
    return lines


def _runtime(rt: dict) -> list[str]:
    init, res, opts = rt.get("init") or {}, rt.get("result") or {}, rt.get("options") or {}
    return [
        f"- Model reported by the runtime: **{init.get('model')}**; models billed: {res.get('models')}; "
        f"runtime claude_code_version {init.get('claude_code_version')}; permission mode {init.get('permissionMode')}",
        f"- Turns {res.get('num_turns')}; duration {res.get('duration_ms')} ms; cost USD {res.get('total_cost_usd')}; "
        f"usage {json.dumps(res.get('usage'))[:300]}",
        f"- Built-in tools requested: {opts.get('tools')}; MCP servers passed: {opts.get('mcp_servers')}; "
        f"secrets blanked in the agent process: {opts.get('env_blanked')}",
        f"- Tool inventory the runtime reported: {init.get('tools')}",
        f"- MCP servers connected: {[(s.get('name'), s.get('status')) for s in init.get('mcp_servers') or []]}",
    ]


def render(r: dict) -> str:
    lines = [
        "# Technical Agent integration proof -- real runtime, simulated DEV estate",
        "",
        "**What this is:** the real Architect and the real Technical Agent, through the Claude CLI runtime, working a "
        "SYNTHETIC customer-owned event-rule example in the synthetic `jade_sim_er` simulation format, against the "
        "shared simulated DEV estate. It demonstrates agent integration and controls. It does NOT demonstrate editing "
        "or deploying a real JDE object: no real mechanism is qualified, the live adapter is unavailable, and every "
        "result below is SIMULATION. All approvals and the CNC hand-off were made by synthetic identities.",
        "",
        f"- Run: {r['started_at']} to {r.get('finished_at')} (UTC)",
        f"- Backend commit: `{r['commits']['backend']}`; frontend commit: `{r['commits']['frontend']}`",
        f"- Claude CLI: {r.get('claude_cli')}; claude-agent-sdk {r.get('sdk_version')}",
        "",
        "## Checks (harness)",
        "",
        *[f"- {'PASS' if c['ok'] else 'FAIL'} -- {c['check']}{(' -- ' + c['detail']) if c['detail'] else ''}"
          for c in r["harness_checks"]],
        "",
        "## B. The Architect (real run)",
        "",
        *_runtime(r["architect"]["runtime"]),
        f"- Stage: {r['architect']['stage']}; error: {r['architect']['error']}",
        f"- Decision: {json.dumps(r['architect']['decision'])[:1500]}",
        f"- Implementation specification: {json.dumps(r['architect']['spec'])[:1200]}",
        f"- Design approval (synthetic approver): {json.dumps(r['design_approval']['body'])[:600]}",
        "",
        *_table(r["architect"]["trace"]),
        "",
    ]
    for i, p in enumerate(r.get("phases", []), 1):
        rr = p["run_record"]
        lines += [
            f"## Technical Agent run {i}: {p['purpose']} ({p['run_id']})",
            "",
            *_runtime(p["runtime"]),
            f"- Run record: status {rr['status']}; error {rr['error']}; outcome {json.dumps(rr['outcome'])[:1500]}",
            "",
            *_table(p["trace"]),
            "",
            "Final reply:",
            "",
            *[f"> {line}" for line in ((p["runtime"].get("result") or {}).get("final_text") or "").splitlines()],
            "",
        ]
    lines += ["## Package revisions", ""]
    for pk in sorted(r.get("packages", []), key=lambda p: p["revision"]):
        c = pk["content"]
        lines += [
            f"### Revision {pk['revision']} -- sha256 `{pk['content_sha256']}`"
            f"{' (superseded by ' + str(pk['superseded_by']) + ')' if pk['superseded_by'] else ''}",
            "",
            f"- Exact change: `{pk['change_id']}`, approval status **{pk['status']}**; milestones "
            f"{[m['milestone'] for m in (pk['milestones'] or [])]}",
            f"- Design revision {c['design']['design_revision']}, baseline `{c['design']['baseline_id']}`, design approval "
            f"`{c['design']['design_approval_id']}`; target environment {c['target_environment']}; mode {c['mode']}",
            f"- Objects: {c['objects']}",
            f"- Sources: {[(s['evidence_id'], s['sha256'][:16], s['classification']) for s in c['sources']]}",
            f"- Toolchain: {c['toolchain']}",
            f"- Explanation: {c['explanation']}",
            f"- Requirement trace: {c['requirement_trace']}",
            f"- Dependencies: {c['dependencies']}; missing evidence: {c['missing_evidence']}; unsupported: {c['unsupported']}",
            f"- Repair of: {c.get('repair_of')}",
            f"- Tests: {[(t['kind'], t['name']) for t in c['test_plan']]}",
            "",
            "```diff",
            c["diff"].rstrip(),
            "```",
            "",
        ]
        for m in pk["milestones"] or []:
            if m.get("log"):
                lines += [f"- {m['milestone']} log: {m['log']}"]
        lines.append("")
    fe = r.get("final_evidence") or {}
    ver = fe.get("verification") or {}
    lines += [
        "## CNC hand-off (simulated, synthetic CNC operator)",
        "",
        f"- {json.dumps({k: v for k, v in (r.get('cnc_activation') or {}).get('body', {}).items() if k != 'activated'})[:800]}",
        "",
        "## Verification results",
        "",
        "| Test | Kind | Passed | Expected | Actual |",
        "|---|---|---|---|---|",
        *[f"| {x.get('name')} | {x.get('kind')} | {x.get('passed')} | {x.get('expected')} | {x.get('actual', x.get('error'))} |"
          for x in ver.get("results", [])],
        "",
        "## Discovery after the change",
        "",
        f"- Observation `{(r.get('discovery_after') or {}).get('observation_id')}` ({(r.get('discovery_after') or {}).get('mode')}): "
        f"{(r.get('discovery_after') or {}).get('records')}",
        "",
        "## Final evidence",
        "",
        f"- Evidence chain: {fe.get('chain')}",
        f"- Active simulated runtime sha256 `{fe.get('active_runtime_sha256')}`; approved candidate sha256 "
        f"`{fe.get('approved_candidate_sha256')}`; package sha256 `{fe.get('package_sha256')}`; exact change "
        f"`{fe.get('change_id')}`",
        f"- Simulated estate history (who changed what): {[(h.get('revision'), h.get('actor'), h.get('reason')) for h in fe.get('estate_history', [])]}",
        "",
    ]
    probe = r.get("delegation_probe")
    if probe:
        lines += ["## Delegation probe (same restricted runtime)", "", *_runtime(probe["runtime"]), "",
                  *_table(probe["trace"]), "", "Final reply:", "",
                  *[f"> {line}" for line in ((probe["runtime"].get("result") or {}).get("final_text") or "").splitlines()],
                  ""]
    return "\n".join(lines)
