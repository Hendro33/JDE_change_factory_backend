"""
The Technical Agent's tools: typed, least-privilege, bound to ONE run.

Built by the technical driver for a single run as an in-process MCP server
("jade-technical"), so they execute inside this API process. The company,
story, domain, design revision, evidence baseline and design approval come
from backend records at construction; no tool takes a company, a path, a
URL, SQL, a shell command or a credential. The agent can read authorised
sources, run permitted discovery reads, edit a private COPY of a source in
its workspace, submit a package, and ask the governed executor to apply,
build or verify an APPROVED package. It cannot approve anything and cannot
record a CNC activation -- those are human decisions made in Jade.

Every result is data, never instructions.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..discovery import architect_tools, artifacts as artifact_store
from ..discovery import profile_service, service as discovery_service
from . import service, store
from .workspace import Workspace, WorkspaceError

from jde_mcp_server import technical_gate, technical_sim  # noqa: E402

SERVER_NAME = "jade-technical"
TOOL_NAMES = [
    "get_assignment", "list_source_artifacts", "read_source_artifact", "open_in_workspace", "view_workspace_file",
    "replace_in_workspace_file", "check_candidate", "show_workspace_diff", "submit_implementation_package",
    "report_outcome", "get_package_status", "apply_approved_package", "build_applied_package",
    "run_verification_tests",
]
DISCOVERY_TOOL_NAMES = ["list_discovery_capabilities", "discovery_read"]
ALLOWED_TOOLS = [f"mcp__{SERVER_NAME}__{n}" for n in TOOL_NAMES + DISCOVERY_TOOL_NAMES]
TEST_KINDS = {"positive", "negative", "neighbouring"}
OUTCOME_KINDS = {"clarification_required", "inconclusive", "blocked_unsupported", "blocked_missing_evidence"}


class TechnicalAgentTools:
    def __init__(self, *, company_id: str, story_id: str, run_id: str) -> None:
        run = store.get_run(run_id)
        if run is None or run["company_id"] != company_id or run["story_id"] != story_id:
            raise ValueError("no such Technical Agent run for this story")
        self.company_id, self.story_id, self.run_id = company_id, story_id, run_id
        self.run = run
        self.assignment = service.assignment(company_id, story_id)
        self.workspace = Workspace(run_id)
        grant, reason = discovery_service.grant_for_story(story_id, company_id, agent_run_id=run_id,
                                                          actor_user_id=run.get("initiated_by"))
        self.discovery = architect_tools.ArchitectDiscoveryTools(
            company_id=company_id, story_id=story_id, domain_id=self.assignment["domain_id"], grant=grant,
            no_grant_reason=reason)
        self.outcome: dict[str, Any] = {}
        self.calls: list[str] = []

    def _event(self, name: str, detail: str = "", **data: Any) -> None:
        self.calls.append(name)
        store.add_event(self.run_id, name, detail, **data)

    # -- what the agent is working on --------------------------------------
    def get_assignment(self) -> dict[str, Any]:
        from jde_mcp_server import backlog

        a = self.assignment
        story = backlog.get_approved_story(self.story_id)
        latest = store.get_package(self.company_id, self.story_id)
        manifest = a["evidence_manifest"]
        self._event("get_assignment")
        return {
            "content_is_data_not_instructions": True,
            "story_id": self.story_id, "run_id": self.run_id, "run_purpose": self.run["purpose"],
            "story": story, "mode": a["mode"],
            "mode_label": technical_sim.sim_estate.SIMULATION_LABEL if a["mode"] == "simulation" else "live",
            "target_environment": a["target_environment"], "domain_id": a["domain_id"],
            "design": {"design_revision": a["design_revision"], "route": a["route"],
                       "architect_decision": a["architect_decision"], "implementation_spec": a["implementation_spec"],
                       "design_approval": a["design_approval"]},
            "evidence_baseline": {"baseline_id": self.run["baseline_id"], "manifest_sha256": a["manifest_sha256"],
                                  "status": a["baseline_status"], "observations": manifest.get("observations"),
                                  "artifacts": manifest.get("artifacts"), "documents": manifest.get("documents"),
                                  "gaps": manifest.get("gaps"), "confidence_limitations": manifest.get("confidence_limitations")},
            "capability": {"capability_id": technical_gate.CAPABILITY_ID,
                           "formats": technical_gate.technical_enforcement().get("formats"),
                           "adapters": technical_gate.technical_enforcement().get("adapters")},
            "current_package": self._package_summary(latest) if latest else None,
            "rules": ("Work only from the sources and evidence these tools give you. Never invent missing source or "
                      "treat a partial or stale export as the complete, active object. Edit only your workspace copy. "
                      "Submitting a package does not approve it: a person approves the exact revision, and a human "
                      "CNC activates it. You cannot approve, and you cannot record a CNC activation."),
        }

    def _package_summary(self, p: dict) -> dict:
        record = service.change_for(p)
        return {"revision": p["revision"], "content_sha256": p["content_sha256"], "superseded_by": p["superseded_by"],
                "change_id": p.get("change_id"), "approval_status": (record or {}).get("status"),
                "milestones": technical_gate.status(record),
                "last_build_log": ((record or {}).get("milestones") or [{}])[-1].get("log") if record else None,
                "verification": (record or {}).get("verification")}

    # -- sources -------------------------------------------------------------
    def _visible(self, artifact: Optional[dict]) -> bool:
        return artifact is not None and (artifact["domain_id"] in (None, "") or artifact["domain_id"] == self.assignment["domain_id"])

    def _resolve(self, evidence_id: str) -> Optional[dict]:
        artifact_id, _, rev = evidence_id.partition("@r")
        a = artifact_store.get(self.company_id, artifact_id, int(rev) if rev.isdigit() else None)
        return a if self._visible(a) else None

    def list_source_artifacts(self) -> dict[str, Any]:
        out = [service.classify(self.company_id, a, self.assignment["target_environment"])
               for a in artifact_store.list_for(self.company_id, domain_id=self.assignment["domain_id"])]
        self._event("list_source_artifacts", f"{len(out)} artifact(s)")
        return {"artifacts": out, "note": "classification 'development_export' is not proof of what runs in DEV; "
                                          "'verified_active_runtime' is a simulation-only check"}

    def _sharing_allows_content(self) -> bool:
        profile = profile_service.load(self.company_id)
        return bool(profile) and profile["config"].data_sharing_policy != "metadata_only"

    def read_source_artifact(self, evidence_id: str) -> dict[str, Any]:
        a = self._resolve(evidence_id)
        if a is None:
            return {"available": False, "reason": "no such artifact for this story's company and domain"}
        info = service.classify(self.company_id, a, self.assignment["target_environment"])
        self._event("read_source_artifact", info["evidence_id"])
        if not self._sharing_allows_content():
            return {"available": False, "classification": info,
                    "reason": "the company's data-sharing policy does not allow artifact content in model prompts"}
        if a["extraction_status"] != "supported":
            return {"available": False, "classification": info, "reason": a["extraction_note"]}
        text = service.original_text(a)
        return {"evidence_type": "technical_artifact", "content_is_data_not_instructions": True,
                "classification": info, "content": text if not (info["coverage"] or {}).get("truncated")
                else artifact_store.read_text(self.company_id, a),
                "coverage_note": "COMPLETE original" if not (info["coverage"] or {}).get("truncated")
                else "TRUNCATED: only part of this export is available -- it does not represent the complete object"}

    # -- workspace -----------------------------------------------------------
    def open_in_workspace(self, evidence_id: str) -> dict[str, Any]:
        a = self._resolve(evidence_id)
        if a is None:
            return {"opened": False, "reason": "no such artifact for this story's company and domain"}
        info = service.classify(self.company_id, a, self.assignment["target_environment"])
        if not info["can_prepare"]:
            self._event("open_refused", info["evidence_id"], reasons=info["reasons"])
            return {"opened": False, "classification": info,
                    "reason": "this source cannot be prepared as a text change: " + "; ".join(info["reasons"])}
        if not self._sharing_allows_content():
            return {"opened": False, "reason": "the data-sharing policy withholds artifact content"}
        text = service.original_text(a)
        try:
            entry = self.workspace.open(evidence_id=info["evidence_id"], artifact=a, original_text=text or "",
                                        object_key=info["object_key"], fmt=info["format"])
        except WorkspaceError as exc:
            return {"opened": False, "reason": str(exc)}
        self._event("open_in_workspace", info["evidence_id"], file_id=entry["file_id"])
        return {"opened": True, **entry, "view": self.workspace.view(entry["file_id"]),
                "note": "this is a private COPY; the original and its checksum are unchanged"}

    def view(self, file_id: str, start: int = 1, end: Optional[int] = None) -> dict[str, Any]:
        try:
            return self.workspace.view(file_id, start, end)
        except WorkspaceError as exc:
            return {"error": str(exc)}

    def replace(self, file_id: str, old_text: str, new_text: str) -> dict[str, Any]:
        try:
            result = self.workspace.replace(file_id, old_text, new_text)
        except WorkspaceError as exc:
            return {"changed": False, "reason": str(exc)}
        self._event("replace_in_workspace_file", file_id)
        return {"changed": True, **result}

    def check_candidate(self, file_id: str) -> dict[str, Any]:
        """Local syntax, declaration, type and interface check of the synthetic
        format -- not the customer's build, which runs only after approval."""
        try:
            entry = self.workspace.files()[file_id]
            text = self.workspace.text(file_id)
        except (KeyError, WorkspaceError):
            return {"error": f"no workspace file {file_id}"}
        if entry["format"] != technical_sim.FORMAT:
            return {"checked": False, "reason": f"no local checker for {entry['format']}"}
        try:
            program = technical_sim.parse(text)
            errors = technical_sim.check(program)
            original = technical_sim.parse(self.workspace.original(file_id))
            if technical_sim.interface(program) != technical_sim.interface(original):
                errors.append("the OBJECT/INPUT/OUTPUT interface changed -- that is a data-structure change, out of scope")
        except technical_sim.ErSyntaxError as exc:
            errors = [f"syntax error {exc}"]
        return {"checked": True, "errors": errors,
                "note": "local check only: the customer's build rules are applied by the build after approval"}

    def diff(self) -> dict[str, Any]:
        return {"files": {f: self.workspace.diff(f) for f in self.workspace.changed_files()}}

    # -- submitting ------------------------------------------------------------
    def submit(self, args: dict) -> dict[str, Any]:
        tests = args.get("test_plan") or []
        kinds = {t.get("kind") for t in tests}
        problems = []
        if not TEST_KINDS <= kinds:
            problems.append(f"the test plan needs positive, negative and neighbouring tests (has: {sorted(k for k in kinds if k)})")
        for t in tests:
            if not t.get("name") or not t.get("event") or not isinstance(t.get("inputs"), dict) or not isinstance(t.get("expected"), dict):
                problems.append(f"test {t.get('name')!r} needs name, event, inputs and expected")
        changed = self.workspace.changed_files()
        if not changed:
            problems.append("the workspace has no changes")
        if not (args.get("explanation") or "").strip():
            problems.append("explain how the change satisfies the Architect's requirements")
        if problems:
            return {"submitted": False, "problems": problems}
        env = self.assignment["target_environment"]
        objects, sources = [], []
        for f in changed:
            entry = self.workspace.files()[f]
            obj = technical_sim.get_object(self.company_id, env, entry["object_key"]) if env else None
            a = artifact_store.get(self.company_id, entry["artifact_id"], entry["revision"])
            meta = a["meta"]
            objects.append({"object_key": entry["object_key"], "object_name": meta.get("object_name"),
                            "object_type": meta.get("object_type"),
                            "system_code": (obj or {}).get("system_code") or "", "format": entry["format"]})
            info = service.classify(self.company_id, a, env)
            sources.append({"evidence_id": entry["evidence_id"], "artifact_id": entry["artifact_id"],
                            "revision": entry["revision"], "sha256": entry["original_sha256"],
                            "classification": info["classification"], "runtime_check": info["runtime_check"],
                            "provenance": info["provenance"]})
        latest = store.get_package(self.company_id, self.story_id)
        repair_of = None
        if latest:
            rec = service.change_for(latest)
            repair_of = {"revision": latest["revision"], "content_sha256": latest["content_sha256"],
                         "milestones": technical_gate.status(rec),
                         "reason": (args.get("repair_reason") or "").strip()[:500]}
        tests = [{"object_key": t.get("object_key") or objects[0]["object_key"], "name": str(t["name"])[:120],
                  "kind": t["kind"], "event": t["event"], "inputs": t["inputs"], "expected": t["expected"],
                  "rationale": str(t.get("rationale", ""))[:300]} for t in tests][:30]
        content = service.build_content(
            a=self.assignment, run=self.run, revision=(latest["revision"] if latest else 0) + 1, workspace=self.workspace,
            objects=objects, sources=sources, explanation=str(args.get("explanation"))[:4000],
            requirement_trace=[{"requirement": str(r.get("requirement", ""))[:300], "how": str(r.get("how", ""))[:500]}
                               for r in (args.get("requirement_trace") or [])][:20],
            dependencies=[str(d)[:300] for d in (args.get("dependencies") or [])][:20], test_plan=tests,
            missing_evidence=[str(m)[:500] for m in (args.get("missing_evidence") or [])][:20],
            unsupported=[str(u)[:500] for u in (args.get("unsupported") or [])][:20],
            recovery=str(args.get("recovery", ""))[:1000], repair_of=repair_of)
        try:
            package = service.submit(self.company_id, self.story_id, self.run_id, content)
        except (store.StaleSubmission, service.TechnicalRefused) as exc:
            self._event("submit_refused", str(exc))
            return {"submitted": False, "problems": [str(exc)]}
        except Exception as exc:  # noqa: BLE001 -- the gate's refusal, reported
            self._event("submit_refused", str(exc))
            return {"submitted": False, "problems": [str(exc)]}
        self.run["expected_package_revision"] = package["revision"]
        self.outcome = {"kind": "package_prepared", "revision": package["revision"],
                        "content_sha256": package["content_sha256"], "change_id": package["change_id"]}
        return {"submitted": True, "revision": package["revision"], "content_sha256": package["content_sha256"],
                "change_id": package["change_id"], "status": "prepared -- awaiting exact implementation approval by a "
                                                              "person; nothing has been applied"}

    def report_outcome(self, kind: str, explanation: str, questions: list[str]) -> dict[str, Any]:
        if kind not in OUTCOME_KINDS:
            return {"recorded": False, "reason": f"kind must be one of {sorted(OUTCOME_KINDS)}"}
        self.outcome = {"kind": kind, "explanation": explanation[:3000], "questions": [q[:500] for q in questions][:20]}
        self._event("report_outcome", kind)
        return {"recorded": True, **self.outcome}

    # -- execution through the governed executor -----------------------------
    def _latest_revision(self, revision: Optional[int]) -> Optional[int]:
        if revision:
            return int(revision)
        latest = store.get_package(self.company_id, self.story_id)
        return latest["revision"] if latest else None

    def status(self, revision: Optional[int]) -> dict[str, Any]:
        rev = self._latest_revision(revision)
        p = store.get_package(self.company_id, self.story_id, rev) if rev else None
        if p is None:
            return {"package": None}
        return {**self._package_summary(p), "eligibility": service.eligibility(p)}

    def milestone(self, name: str, revision: Optional[int]) -> dict[str, Any]:
        rev = self._latest_revision(revision)
        if rev is None:
            return {"done": False, "blocked": True, "reason": "no package"}
        try:
            result = service.run_milestone(self.company_id, self.story_id, rev, name, actor=f"technical-agent run {self.run_id}")
        except Exception as exc:  # noqa: BLE001 -- the executor's refusal or failure, reported
            self._event(f"{name}_blocked", str(exc), revision=rev)
            return {"done": False, "blocked": True, "reason": str(exc), "revision": rev}
        self._event(name, json.dumps(result, default=str)[:400], revision=rev)
        self.outcome = {"kind": "execution_progress", "last_milestone": name, "revision": rev,
                        "result": {k: v for k, v in result.items() if k in ("milestone", "passed", "log")}}
        return {"done": True, "revision": rev, **result}

    # -- the in-process MCP server ------------------------------------------
    def sdk_server(self):
        import claude_agent_sdk as sdk

        def reply(payload: dict) -> dict:
            return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}

        obj = {"type": "object"}
        tools = [
            sdk.tool("get_assignment", "The story, the Architect's approved design revision and its evidence baseline, "
                     "the design approval, the target DEV environment, the capability's supported formats and adapters, "
                     "and the current package revision and its milestones.", {})(lambda a: _async(reply(self.get_assignment()))),
            sdk.tool("list_source_artifacts", "Authorised source exports and documents for this story, each classified: "
                     "format support, completeness, development export vs attested vs verified active runtime, stale.",
                     {})(lambda a: _async(reply(self.list_source_artifacts()))),
            sdk.tool("read_source_artifact", "Read one authorised source (evidence id like ART-...@r1).",
                     {**obj, "properties": {"evidence_id": {"type": "string"}}, "required": ["evidence_id"]})(
                lambda a: _async(reply(self.read_source_artifact(a.get("evidence_id", ""))))),
            sdk.tool("open_in_workspace", "Copy one authorised, complete, supported source into your private workspace "
                     "for editing. Refused for unsupported, partial or stale sources.",
                     {**obj, "properties": {"evidence_id": {"type": "string"}}, "required": ["evidence_id"]})(
                lambda a: _async(reply(self.open_in_workspace(a.get("evidence_id", ""))))),
            sdk.tool("view_workspace_file", "Numbered lines of a workspace file.",
                     {**obj, "properties": {"file_id": {"type": "string"}, "start_line": {"type": "integer"},
                                            "end_line": {"type": "integer"}}, "required": ["file_id"]})(
                lambda a: _async(reply(self.view(a.get("file_id", ""), a.get("start_line") or 1, a.get("end_line"))))),
            sdk.tool("replace_in_workspace_file", "Replace exactly one occurrence of old_text with new_text in a "
                     "workspace file (your copy only). Returns the resulting diff.",
                     {**obj, "properties": {"file_id": {"type": "string"}, "old_text": {"type": "string"},
                                            "new_text": {"type": "string"}}, "required": ["file_id", "old_text", "new_text"]})(
                lambda a: _async(reply(self.replace(a.get("file_id", ""), a.get("old_text", ""), a.get("new_text", ""))))),
            sdk.tool("check_candidate", "Local syntax/type/interface check of a workspace file in the synthetic "
                     "simulation format. Not the customer's build.",
                     {**obj, "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]})(
                lambda a: _async(reply(self.check_candidate(a.get("file_id", ""))))),
            sdk.tool("show_workspace_diff", "The exact unified diff of every changed workspace file against its original.",
                     {})(lambda a: _async(reply(self.diff()))),
            sdk.tool("submit_implementation_package", "Submit the workspace changes as the next package revision for a "
                     "person to approve. Needs: explanation, requirement_trace [{requirement, how}], dependencies, "
                     "test_plan [{name, kind: positive|negative|neighbouring, event, inputs{}, expected{}, rationale}] "
                     "with all three kinds, missing_evidence, unsupported, recovery; repair_reason for a repair. Jade "
                     "computes the diff and checksums itself.",
                     {**obj, "properties": {
                         "explanation": {"type": "string"}, "repair_reason": {"type": "string"},
                         "requirement_trace": {"type": "array", "items": obj},
                         "dependencies": {"type": "array", "items": {"type": "string"}},
                         "test_plan": {"type": "array", "items": obj},
                         "missing_evidence": {"type": "array", "items": {"type": "string"}},
                         "unsupported": {"type": "array", "items": {"type": "string"}},
                         "recovery": {"type": "string"}}, "required": ["explanation", "test_plan"]})(
                lambda a: _async(reply(self.submit(a)))),
            sdk.tool("report_outcome", "Report that no package can safely be prepared: kind clarification_required | "
                     "inconclusive | blocked_unsupported | blocked_missing_evidence, with the explanation and the "
                     "questions a person must answer. This is a valid outcome, not a failure.",
                     {**obj, "properties": {"kind": {"type": "string"}, "explanation": {"type": "string"},
                                            "questions": {"type": "array", "items": {"type": "string"}}},
                      "required": ["kind", "explanation"]})(
                lambda a: _async(reply(self.report_outcome(a.get("kind", ""), a.get("explanation", ""),
                                                           a.get("questions") or [])))),
            sdk.tool("get_package_status", "A package revision's approval, milestones (apply, build, CNC, verify), last "
                     "build log and execution eligibility. Omit revision for the latest.",
                     {**obj, "properties": {"revision": {"type": "integer"}}})(
                lambda a: _async(reply(self.status(a.get("revision"))))),
            sdk.tool("apply_approved_package", "Ask the governed executor to apply an APPROVED package revision "
                     "(check in, not active). Refused unless every check passes.",
                     {**obj, "properties": {"revision": {"type": "integer"}}})(
                lambda a: _async(reply(self.milestone("apply", a.get("revision"))))),
            sdk.tool("build_applied_package", "Ask the governed executor to build an applied package revision.",
                     {**obj, "properties": {"revision": {"type": "integer"}}})(
                lambda a: _async(reply(self.milestone("build", a.get("revision"))))),
            sdk.tool("run_verification_tests", "Ask the governed executor to run the approved test plan against the "
                     "ACTIVE DEV runtime. Refused until a human CNC activation is recorded.",
                     {**obj, "properties": {"revision": {"type": "integer"}}})(
                lambda a: _async(reply(self.milestone("verify", a.get("revision"))))),
            sdk.tool("list_discovery_capabilities", "Permitted read-only discovery for this story (same governed "
                     "service as the Architect's).", {})(lambda a: _async(reply(self.discovery.list_capabilities()))),
            sdk.tool("discovery_read", "A read-only discovery read within the approved scope: capability_id, target, "
                     "fields, filters, max_records. Out-of-scope requests are refused before anything is sent.",
                     {**obj, "properties": {"capability_id": {"type": "string"}, "target": {"type": "string"},
                                            "fields": {"type": "array", "items": {"type": "string"}},
                                            "filters": {"type": "array", "items": obj},
                                            "max_records": {"type": "integer"}}, "required": ["capability_id"]})(
                lambda a: _async(reply(self.discovery.read(a.get("capability_id", ""), a.get("target", "") or "",
                                                           a.get("fields"), a.get("filters"),
                                                           int(a.get("max_records", 10) or 10))))),
        ]
        return sdk.create_sdk_mcp_server(SERVER_NAME, tools=tools)


async def _async(value):
    return value
