"""
The one canonical business lifecycle of a story.

Every screen, list, dashboard and report shows where a story is from THIS
module -- never from its own reading of the underlying records. It is a
read-only projection: it never changes any gate, approval or execution
record, and it never decides whether anything may run (the gates still do
that, independently). It only answers three separate questions:

  * Phase        -- where the story is in its business lifecycle:
                    Understand -> Story Review -> Solutioning -> Solution Review
                    -> Delivery -> Validation -> Release -> Done
  * Health       -- whether it is progressing, waiting, blocked or failed
  * Next action  -- what has to happen next, and who owns it

The facts come from the authoritative records that already exist: the
change request and its enhancement run, backlog.py's Gate 2 record, the
domain review sidecar, the Architecture Review run, the design baseline and
design approvals, approval.py's exact-change record with its execution
states, the technical packages and their milestones, and the as-built
records.
"""

from __future__ import annotations

from typing import Any, Literal, Optional


from ..models.lifecycle import (  # noqa: E402 -- re-exported for callers
    HEALTH_LABELS, OWNER_LABELS, PHASE_LABELS, PHASES, TECHNICAL_ROUTES, Lifecycle, LifecycleStep, NextAction,
)


def _mk(phase: str, health: str, action: NextAction, **kw: Any) -> Lifecycle:
    action.owner_label = OWNER_LABELS.get(action.owner, "")
    return Lifecycle(phase=phase, phase_label=PHASE_LABELS[phase], phase_index=PHASES.index(phase),  # type: ignore[arg-type]
                     health=health, health_label=HEALTH_LABELS[health], next_action=action, **kw)  # type: ignore[arg-type]


def _jade(summary: str, tab: str = "overview") -> NextAction:
    return NextAction(kind="none", summary=summary, owner="jade", tab=tab)  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# Reading the records (each read is defensive: a missing sidecar simply
# means that stage has not happened yet)
# ---------------------------------------------------------------------
def _design_facts(company_id: str, story_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {"baseline": None, "design_approved": False}
    try:
        from ..discovery import baseline
        from ..technical import store as tstore

        b = baseline.current_for_story(company_id, story_id)
        out["baseline"] = b
        if b is not None:
            out["design_approved"] = tstore.design_approval_for(company_id, story_id, b["design_revision"]) is not None
    except Exception:  # noqa: BLE001 -- no baseline tables/records yet
        pass
    return out


def _technical_facts(company_id: str, story_id: str) -> dict[str, Any]:
    """The current (newest, not superseded) package and how far it got."""
    try:
        from jde_mcp_server import approval, execution, technical_gate

        from ..technical import store as tstore

        packages = tstore.packages_for(company_id, story_id)
        runs = tstore.runs_for(company_id, story_id)
    except Exception:  # noqa: BLE001
        return {"package": None, "record": None, "running": False, "last_run": None}
    running = any(r.get("status") == "running" for r in runs)
    current = next((p for p in packages if not p.get("superseded_by")), None)
    record = None
    if current and current.get("change_id"):
        try:
            record = approval._load(current["change_id"])  # noqa: SLF001 -- read-only
        except Exception:  # noqa: BLE001
            record = None
    states = technical_gate.status(record) if record else None
    return {"package": current, "record": record, "states": states, "running": running,
            "last_run": runs[0] if runs else None,
            "verify_state": execution.effective_state(record, technical_gate.VERIFY) if record else None}


def _asbuilt_final(company_id: str, story_id: str) -> Optional[dict]:
    try:
        from ..process import asbuilt

        return next((r for r in asbuilt.records(company_id, story_id) if r.get("status") == "final"), None)
    except Exception:  # noqa: BLE001
        return None


def _asbuilt_any(company_id: str, story_id: str) -> Optional[dict]:
    try:
        from ..process import asbuilt

        rows = asbuilt.records(company_id, story_id)
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


def _process_decided(company_id: str, story_id: str) -> Optional[bool]:
    """True/False once a framework applies; None when no framework is active."""
    try:
        from ..process import framework, story as story_process

        if framework.selected_active(company_id) is None:
            return None
        return story_process.current_mapping(company_id, story_id) is not None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------
# The derivation
# ---------------------------------------------------------------------
def _understand(change) -> Lifecycle:
    stage = change.processing_stage
    if stage in ("receiving", "improving", "checking"):
        return _mk("understand", "in_progress", _jade("JADE is analysing the request and writing the user story."))
    if stage == "failed":
        return _mk("understand", "failed", NextAction(
            kind="task", summary="JADE could not finish analysing this request. Check the problem and try again.",
            owner="product_manager", action="start_analysis", tab="overview",
            effect="JADE analyses the request again and writes the user story."))
    if stage == "done":
        # A finished run that did not produce a backlog story: the check
        # found gaps a person has to resolve.
        return _mk("understand", "blocked", NextAction(
            kind="task", summary="The story needs clarification before it can go to review.",
            owner="product_manager", action="start_analysis", tab="story",
            effect="JADE re-analyses the request with the added information."),
            open_items=["JADE's quality check found open points in the request."])
    return _mk("understand", "waiting", NextAction(
        kind="task", summary="Start JADE's analysis to turn this request into a user story.",
        owner="product_manager", action="start_analysis", tab="overview",
        effect="JADE interprets the request, maps it to the business and writes the user story."))


def _story_review(change) -> Lifecycle:
    stage = change.domain_review_stage
    if not change.business_domain_id and stage in (None, "ready_for_domain_owner"):
        return _mk("story_review", "waiting", NextAction(
            kind="task", summary="Place this story in a business domain so its Domain Owner can review it.",
            owner="product_manager", action="assign_domain", tab="story",
            effect="The story goes to that domain's owner for review."))
    if stage in (None, "ready_for_domain_owner", "domain_owner_reviewing"):
        return _mk("story_review", "waiting_decision", NextAction(
            kind="decision", summary="Review the user story and approve it, or ask for changes.",
            owner="domain_owner", action="review_story", tab="story",
            effect="An approved story goes to the Application Manager to authorise delivery."))
    if stage in ("domain_owner_requested_revision", "reviewer_agent_refining"):
        return _mk("story_review", "in_progress", _jade("JADE is revising the story with the requested changes.", "story"))
    if stage == "domain_owner_rejected":
        return _mk("done", "closed", NextAction(kind="none", summary="The Domain Owner decided this requirement should not proceed."),
                   outcome="rejected")
    if stage in ("domain_owner_approved", "ready_for_application_manager"):
        return _mk("story_review", "waiting_decision", NextAction(
            kind="decision", summary="Authorise JADE to work on this approved story.",
            owner="product_manager", action="authorise_delivery", tab="story",
            effect="JADE starts solutioning: it researches the JDE environment and proposes a solution."))
    return _mk("story_review", "waiting", NextAction(kind="none", summary="Waiting for the story review."))


def _delivery_steps(route: Optional[str], tech: dict, change, final: Optional[dict]) -> list[LifecycleStep]:
    steps: list[LifecycleStep] = []

    def add(i: str, label: str, done: bool, current: bool, detail: str = "", failed: bool = False) -> None:
        steps.append(LifecycleStep(id=i, label=label, detail=detail,
                                   state="failed" if failed else "done" if done else "current" if current else "todo"))

    if route in TECHNICAL_ROUTES:
        st = tech.get("states") or {}
        rec = tech.get("record") or {}
        approved = rec.get("status") == "approved"
        built = st.get("build") == "built"
        cnc = bool(rec.get("cnc_activation"))
        ver = rec.get("verification") or {}
        verified = bool(ver.get("passed")) and bool(ver.get("runtime_is_approved_artifact"))
        results = ver.get("results") or []
        add("build", "Build", built and cnc, bool(tech.get("package")) and not (built and cnc),
            "Built and activated in DEV" if built and cnc else "Built; awaiting activation in DEV" if built else
            "Implementation approved" if approved else "",
            failed=st.get("build") == "failed" or st.get("apply") in ("unknown", "diverged"))
        add("validation", "Validation", verified, built and cnc and not verified,
            f"{sum(1 for r in results if r.get('passed'))} / {len(results)} tests passed" if results else "",
            failed=bool(ver) and not verified)
        add("release", "Release approval", bool(final), verified and not final,
            "As-built record finalised" if final else "")
        add("production", "Production", False, False, "Promotion is done by CNC outside JADE")
    else:
        ex = (change.exact_change.execution if change.exact_change else None)
        applied = bool(ex) and ex.write_state == "applied"
        tested = bool(ex) and ex.test_state == "completed"
        approved = bool(change.change_approval) and change.change_approval.status == "approved"
        add("build", "Build", applied, approved and not applied, "Change applied in DEV" if applied else "",
            failed=bool(ex) and ex.write_state in ("unknown", "diverged"))
        add("validation", "Validation", tested, applied and not tested, "Test completed" if tested else "",
            failed=bool(ex) and ex.test_state in ("unknown", "diverged"))
        add("release", "Release approval", bool(final), tested and not final, "As-built record finalised" if final else "")
        add("production", "Production", False, False, "Promotion is done by CNC outside JADE")
    return steps


def _technical_route(change, company_id: str, story_id: str, design: dict, route: str) -> Lifecycle:
    tech = _technical_facts(company_id, story_id)
    final = _asbuilt_final(company_id, story_id)
    steps = _delivery_steps(route, tech, change, final)
    b = design.get("baseline")
    if not design.get("design_approved"):
        return _mk("solution_review", "waiting_decision", NextAction(
            kind="decision", summary="Review JADE's proposed solution and approve it for delivery.",
            owner="product_manager", action="approve_design", tab="solution",
            effect="JADE prepares the exact implementation in the DEV environment for your approval."),
            route=route)
    pkg, rec, st = tech.get("package"), tech.get("record"), tech.get("states") or {}
    if pkg is None:
        if tech.get("running"):
            return _mk("delivery", "in_progress", _jade("JADE is preparing the implementation.", "delivery"),
                       delivery_steps=steps, route=route)
        return _mk("delivery", "waiting", NextAction(
            kind="task", summary="Start the implementation: JADE prepares the exact change package.",
            owner="product_manager", action="start_technical_prepare", tab="delivery",
            effect="JADE prepares a package revision for you to approve."), delivery_steps=steps, route=route)
    status = (rec or {}).get("status")
    if status not in ("approved", "rejected"):
        return _mk("delivery", "waiting_decision", NextAction(
            kind="decision", summary=f"Approve the exact implementation (package revision {pkg['revision']}).",
            owner="product_manager", action="approve_package", tab="delivery",
            effect="JADE may apply and build exactly this package in DEV -- nothing else."),
            delivery_steps=steps, route=route)
    if status == "rejected":
        return _mk("delivery", "blocked", NextAction(
            kind="task", summary="The implementation was rejected. Start a new revision with the requested changes.",
            owner="product_manager", action="start_technical_prepare", tab="delivery",
            effect="JADE prepares a new package revision."), delivery_steps=steps, route=route)
    apply_s, build_s = st.get("apply"), st.get("build")
    if apply_s in ("unknown", "diverged") or build_s in ("unknown", "diverged", "failed"):
        return _mk("delivery", "failed", NextAction(
            kind="task", summary="An implementation step did not complete cleanly. Check the DEV state and reconcile it.",
            owner="product_manager", action="reconcile_technical", tab="technical",
            effect="Once reconciled, delivery can continue safely."), delivery_steps=steps, route=route)
    if apply_s != "applied" or build_s != "built":
        if tech.get("running") or apply_s == "in_progress" or build_s == "in_progress":
            return _mk("delivery", "in_progress", _jade("JADE is applying and building the change in DEV.", "delivery"),
                       delivery_steps=steps, route=route)
        return _mk("delivery", "waiting", NextAction(
            kind="task", summary="Run the approved implementation in DEV (apply and build).",
            owner="product_manager", action="start_technical_execute", tab="delivery",
            effect="JADE applies and builds exactly the approved package in DEV."), delivery_steps=steps, route=route)
    if not (rec or {}).get("cnc_activation"):
        return _mk("delivery", "waiting", NextAction(
            kind="task", summary="Activate the built package in DEV and record it.",
            owner="cnc_operator", action="record_cnc", tab="delivery",
            effect="Validation can then run against the active DEV runtime."), delivery_steps=steps, route=route)
    ver = (rec or {}).get("verification") or {}
    if not ver:
        if tech.get("running") or tech.get("verify_state") == "in_progress":
            return _mk("validation", "in_progress", _jade("JADE is validating the change against the acceptance tests.",
                                                          "delivery"), delivery_steps=steps, route=route)
        return _mk("validation", "waiting", NextAction(
            kind="task", summary="Run validation: JADE tests the active change against the acceptance criteria.",
            owner="product_manager", action="start_technical_verify", tab="delivery",
            effect="The results are recorded as validation evidence."), delivery_steps=steps, route=route)
    if not (ver.get("passed") and ver.get("runtime_is_approved_artifact")):
        return _mk("validation", "failed", NextAction(
            kind="task", summary="Validation did not pass. Review the failed tests and start a repair revision.",
            owner="product_manager", action="start_technical_prepare", tab="delivery",
            effect="JADE prepares a repair as a new package revision for approval."), delivery_steps=steps, route=route)
    if final is None:
        return _mk("release", "waiting", NextAction(
            kind="task", summary="Validation passed. Review and finalise the as-built record to complete the release.",
            owner="product_manager", action="finalise_asbuilt", tab="delivery",
            effect="The story is recorded as delivered, with its as-built record as permanent knowledge."),
            delivery_steps=steps, route=route)
    return _mk("done", "done", NextAction(kind="none", summary="Delivered and recorded."), outcome="delivered",
               delivery_steps=steps, route=route)


def _functional_route(change, company_id: str, story_id: str, design: dict, route: str) -> Lifecycle:
    final = _asbuilt_final(company_id, story_id)
    tech: dict = {}
    steps = _delivery_steps(route, tech, change, final)
    ec, ap = change.exact_change, change.change_approval
    if route == "Human Implementation":
        return _mk("delivery", "waiting", NextAction(
            kind="task", summary="This change is implemented by a person outside JADE, following the proposed solution.",
            owner="product_manager", action=None, tab="solution"), route=route, delivery_steps=steps)
    if ec is None:
        return _mk("solution_review", "blocked", NextAction(
            kind="task", summary="JADE proposed a route but no exact change yet. Ask JADE or re-run the analysis.",
            owner="product_manager", action="rerun_solutioning", tab="solution",
            effect="JADE re-analyses the story and proposes the exact change."), route=route)
    if ap is None or ap.status not in ("approved", "rejected"):
        return _mk("solution_review", "waiting_decision", NextAction(
            kind="decision", summary="Review JADE's proposed solution and its exact change, then approve it.",
            owner="product_manager", action="approve_exact_change", tab="solution",
            effect="JADE may apply exactly this change in DEV -- nothing else."), route=route)
    if ap.status == "rejected":
        return _mk("solution_review", "blocked", NextAction(
            kind="task", summary="The proposed change was rejected. Re-run solutioning for a new proposal.",
            owner="product_manager", action="rerun_solutioning", tab="solution",
            effect="JADE proposes a new solution."), route=route)
    ex = ec.execution
    if ex and ex.write_state in ("unknown", "diverged"):
        return _mk("delivery", "failed", NextAction(
            kind="task", summary="The change's outcome in DEV is uncertain. Check the DEV value and reconcile it.",
            owner="product_manager", action="reconcile_functional", tab="technical",
            effect="Once reconciled, delivery can continue safely."), route=route, delivery_steps=steps)
    if ec.capability_executable is False:
        return _mk("delivery", "blocked", NextAction(
            kind="task", summary="Approved, but this kind of change is not yet cleared for automated execution in "
                                 "this environment.",
            owner="admin", action=None, tab="technical",
            effect="An administrator and technical validator must clear the capability first."),
            route=route, delivery_steps=steps,
            open_items=["The execution capability for this change still needs validation."])
    if not ex or ex.write_state != "applied":
        return _mk("delivery", "in_progress", _jade("JADE is applying the approved change in DEV.", "delivery"),
                   route=route, delivery_steps=steps)
    if ex.test_state in ("unknown", "diverged"):
        return _mk("validation", "failed", NextAction(
            kind="task", summary="It is unclear whether the test ran. Confirm and reconcile the test run.",
            owner="product_manager", action="reconcile_functional", tab="technical"), route=route, delivery_steps=steps)
    if ex.test_state != "completed":
        return _mk("validation", "in_progress", _jade("JADE is validating the change.", "delivery"),
                   route=route, delivery_steps=steps)
    if final is None:
        return _mk("release", "waiting", NextAction(
            kind="task", summary="Validation completed. Review and finalise the as-built record to complete the release.",
            owner="product_manager", action="finalise_asbuilt", tab="delivery",
            effect="The story is recorded as delivered, with its as-built record as permanent knowledge."),
            route=route, delivery_steps=steps)
    return _mk("done", "done", NextAction(kind="none", summary="Delivered and recorded."), outcome="delivered",
               route=route, delivery_steps=steps)


def derive(change, company_id: Optional[str] = None) -> Lifecycle:
    """The lifecycle of one story (a Change as assembled by change_service)."""
    company_id = company_id or change.customer_id
    story_id = change.id
    state = change.state
    if state in ("RECEIVED", "REFINING"):
        lc = _understand(change)
    elif state == "REJECTED":
        lc = _mk("done", "closed", NextAction(kind="none", summary="Not authorised for delivery."), outcome="rejected")
    elif state == "RESOLVED_WITHOUT_CHANGE":
        lc = _mk("done", "done", NextAction(kind="none", summary="Resolved without a change to JDE."),
                 outcome="resolved_without_change")
    elif state == "CLOSED":
        lc = _mk("done", "done", NextAction(kind="none", summary="Delivered and closed."), outcome="delivered")
    elif state == "BACKLOG_READY":
        lc = _story_review(change)
    else:
        lc = _solution_onwards(change, company_id, story_id)
    try:
        from .customer_service import is_demo_company

        lc.simulated = is_demo_company(company_id)
    except Exception:  # noqa: BLE001
        pass
    return lc


def _solution_onwards(change, company_id: str, story_id: str) -> Lifecycle:
    arch = change.architecture_review_stage
    if arch == "analyzing":
        return _mk("solutioning", "in_progress", _jade("JADE is researching the JDE environment and preparing a solution.",
                                                       "solution"))
    # A proposed exact change (e.g. from an earlier Functional Agent run or a
    # direct proposal) is a solution, even without a recorded analysis stage.
    if arch is None and change.exact_change is None:
        return _mk("solutioning", "waiting", NextAction(
            kind="task", summary="Start solutioning: JADE researches the JDE environment and proposes a solution.",
            owner="product_manager", action="rerun_solutioning", tab="solution",
            effect="JADE proposes a solution for your review."))
    if arch == "failed":
        return _mk("solutioning", "failed", NextAction(
            kind="task", summary="JADE could not finish the solution analysis. Resolve the problem and run it again.",
            owner="product_manager", action="rerun_solutioning", tab="solution",
            effect="JADE runs the solution analysis again."),
            open_items=[_friendly_error(change.architecture_review_error)] if change.architecture_review_error else [])
    decision = change.architect_decision
    route = decision.recommended_route if decision else None
    design = _design_facts(company_id, story_id)
    b = design.get("baseline")
    open_items: list[str] = []
    # A finalised as-built record means the story was delivered. Later changes
    # (e.g. a new process framework version) flag the design, but do not
    # un-deliver the story: they are shown as open items instead.
    final = _asbuilt_final(company_id, story_id)
    if final is not None:
        items = []
        if b is not None and b.get("status") not in (None, "current"):
            items.append("Since delivery, the design was flagged for reassessment (for example after a process or story change).")
        tech = _technical_facts(company_id, story_id) if route in TECHNICAL_ROUTES else {}
        return _mk("done", "done", NextAction(kind="none", summary="Delivered and recorded."), outcome="delivered",
                   route=route, open_items=items, delivery_steps=_delivery_steps(route, tech, change, final))
    if _process_decided(company_id, story_id) is False:
        open_items.append("Affected business processes have not been confirmed yet.")
    if route == "Clarification Required":
        lc = _mk("solution_review", "blocked", NextAction(
            kind="decision", summary="JADE needs a business answer before it can propose a solution.",
            owner="domain_owner", action="clarify", tab="solution",
            effect="With the answer, JADE re-runs solutioning."), route=route)
    elif route == "Resolve without Change":
        lc = _mk("solution_review", "waiting_decision", NextAction(
            kind="decision", summary="JADE found this can be resolved without changing JDE. Review the explanation.",
            owner="product_manager", action=None, tab="solution"), route=route)
    elif b is not None and b.get("status") not in (None, "current"):
        lc = _mk("solutioning", "blocked", NextAction(
            kind="task", summary="The story changed after the solution was designed. Re-run solutioning.",
            owner="product_manager", action="rerun_solutioning", tab="solution",
            effect="JADE reassesses the solution against the updated story."), route=route)
    elif route in TECHNICAL_ROUTES:
        lc = _technical_route(change, company_id, story_id, design, route)
    else:
        lc = _functional_route(change, company_id, story_id, design, route or "Functional Agent")
    lc.open_items = open_items + [i for i in lc.open_items if i not in open_items]
    return lc


def _friendly_error(raw: Optional[str]) -> str:
    """Business wording for a run error; the raw text stays in the technical view."""
    if not raw:
        return ""
    low = raw.lower()
    if "ai connection" in low or "api key" in low:
        return "The AI connection for this customer is not set up or not working (Administration › Agents & AI)."
    if "budget" in low:
        return "This customer's monthly AI budget is used up."
    if "disabled" in low or "switched off" in low:
        return "The agent needed for this step is switched off for this customer."
    return "The analysis stopped before it finished. Details are in the Technical view."


# ---------------------------------------------------------------------
# My Work
# ---------------------------------------------------------------------
def is_mine(lc: Lifecycle, roles: set[str]) -> bool:
    """Whether this story's next action is owned by someone with these roles.
    Admins also see administrator items; nobody 'owns' JADE's own work."""
    owner = lc.next_action.owner
    if owner in ("jade", "none") or lc.next_action.kind == "none":
        return False
    if owner == "admin":
        return "admin" in roles
    return owner in roles
