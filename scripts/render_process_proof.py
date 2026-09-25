"""Renders docs/proof/process_context_run/summary.md from trace.json (see prove_process_context.py)."""

from __future__ import annotations

import json


def _tools(trace: list) -> list[str]:
    return [t["tool"] for t in trace if t["event"] == "tool_use"]


def _run_block(title: str, rt: dict, trace: list) -> list[str]:
    init, res = rt.get("init") or {}, rt.get("result") or {}
    mcp = sorted(t for t in (init.get("tools") or []) if t.startswith("mcp__"))
    return [f"## {title}", "",
            f"- Model (reported by the runtime): **{init.get('model')}**; billed models {res.get('models')}; "
            f"turns {res.get('num_turns')}; cost USD {res.get('total_cost_usd')}; error {res.get('is_error')}",
            f"- Claude Code runtime {init.get('claude_code_version')}; allowed tools {(rt.get('options') or {}).get('allowed_tools')}",
            f"- MCP tools in the runtime's inventory: {mcp}",
            f"- Tool calls, in order: {_tools(trace)}", ""]


def render(r: dict) -> str:
    L = ["# Real-model demonstration: process context in refinement and design", "",
         "REAL model runs through the intended runtime (claude_agent_sdk -> Claude CLI). The framework is the SYNTHETIC "
         "fixture (SYN- ids, not APQC content). JDE is SIMULATED. Approvals and review decisions are made by a synthetic "
         "test identity (\"Proof Reviewer (synthetic)\"), never the owner.", "",
         f"- Run {r.get('started_at')} to {r.get('finished_at')} (UTC); story `{r.get('story')}`",
         f"- Backend `{(r.get('commits') or {}).get('backend')}`, frontend `{(r.get('commits') or {}).get('frontend')}`; "
         f"{r.get('claude_cli')}; claude-agent-sdk {r.get('sdk_version')}", ""]
    a = r.get("analysis") or {}
    run = a.get("run") or {}
    if a:
        L += _run_block("1. Refinement process analysis (real model)", a.get("runtime") or {}, a.get("trace") or [])
        res = run.get("result") or {}
        L += [f"Status: **{run.get('status')}** {run.get('error') or ''}", "", f"Summary: {res.get('summary', '')}", "",
              "Suggested processes (validated against the exact framework version):", ""]
        L += [f"- `{s['node_key']}` {' > '.join(p['name'] for p in s['path'])} ({s.get('confidence')}): {s.get('rationale')}"
              for s in res.get("suggested_processes") or []] or ["- none"]
        if res.get("rejected_suggestions"):
            L += ["", f"Rejected suggestions (not in the framework): {res['rejected_suggestions']}"]
        for key, title in (("missing_requirements", "Missing requirements"), ("missing_controls", "Missing controls"),
                           ("missing_acceptance_criteria", "Missing acceptance criteria")):
            L += ["", f"{title}:", ""] + ([f"- {x}" for x in res.get(key) or []] or ["- none"])
        L.append("")
    m = r.get("mapping")
    if m:
        L += ["## 2. Reviewer decision (synthetic identity, through the API)", "",
              f"Mapping revision {m['revision']} ({m['status']}) by {m['reviewer_name']}:", ""]
        L += [f"- `{x['framework_id']}@v{x['version']}:{x['node_key']}` sha256 {x['node_sha256'][:12]}... {x['name']}"
              for x in m.get("refs") or []] or [f"- no mapping: {m.get('no_mapping_reason')}"]
        L.append("")
    if r.get("diff"):
        L += ["Proposed story change (diff shown to the reviewer):", "", "```diff"]
        L += [f"{d['op']} [{d['section']}] {d['line']}" for d in r["diff"]] + ["```", ""]
    for rev in (r.get("story_revisions") or [])[:1]:
        L += [f"Story revision {rev['revision']} saved by {rev['author_name']} ({rev['created_at']}), applying "
              f"{len(rev['applied_findings'])} finding(s); process references: mapping revision "
              f"{rev['process_refs'].get('mapping_revision')} {[x['node_key'] + '@v' + str(x['version']) for x in rev['process_refs'].get('refs', [])]}", ""]
    d = r.get("architect") or {}
    if d:
        L += _run_block("3. Architect design (real model)", d.get("runtime") or {}, d.get("trace") or [])
        dec = d.get("decision") or {}
        b = d.get("baseline") or {}
        pc = b.get("process_context") or {}
        L += [f"Stage: **{d.get('stage')}** {d.get('error') or ''}", "",
              f"- Route: **{dec.get('recommended_route')}** (confidence {dec.get('confidence')}); objects {dec.get('objects_affected')}",
              f"- Existing functionality: {dec.get('existing_functionality_found')}",
              f"- get_process_context called: {'mcp__jade-discovery__get_process_context' in _tools(d.get('trace') or [])}; "
              f"recorded in the baseline as consulted: {pc.get('consulted')}",
              f"- Design baseline `{b.get('baseline_id')}` ({b.get('status')}) rests on process fingerprint "
              f"{(pc.get('fingerprint') or {}).get('sha256', '')[:16]}... (mapping revision "
              f"{(pc.get('fingerprint') or {}).get('mapping_revision')}, map versions {(pc.get('fingerprint') or {}).get('map_versions')}); "
              f"the story's fingerprint now: {(r.get('process_fingerprint_now') or {}).get('sha256', '')[:16]}...",
              "", "Architect's process findings:", ""]
        L += [f"- {k.replace('_', ' ')}: {x}" for k, v in (pc.get("findings") or {}).items() for x in v] or ["- none"]
        L += ["", "Gaps recorded in the baseline:", ""] + [f"- {g.get('kind')}: {g.get('description')}" for g in b.get("gaps") or []]
    return "\n".join(L) + "\n"
