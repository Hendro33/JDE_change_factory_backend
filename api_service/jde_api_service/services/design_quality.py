"""Deterministic checks on an Architect design before it can be authorised
(audit 23 Oct, F03, F05, F06).

A completed model run is not a usable design. These checks read the stored
design -- never rewrite it -- and return owned, blocking open points when:

* custom development is recommended while the design itself says standard
  behaviour is not established (F03: the next work is a targeted
  verification, not an asserted need for custom code);
* a change row has an unknown target value (F03: no unknown target is an
  exact implementation specification);
* the design names two different executors for the same build (F06);
* a decision that determines scope (rates, thresholds, mappings, choices,
  "approve the design") is listed as implementation work for after
  authorisation (F06: resolve it before approving the scope);
* the Technical Agent is expected to originate a new object, which it cannot:
  it prepares changes to existing customer objects from their source export
  (F06: say who creates the source and what JADE does with it afterwards).

It also sorts the story's open questions (F05): technical investigation goes
to JADE, business intent and choices to the business, and a question the
Architect's discovery answered is retired with that answer, on the record.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from ..models.issues import Issue

_CUSTOM_ROUTES = ("Technical Agent", "Mixed", "Human Implementation")
_STANDARD_OPEN_RE = re.compile(
    r"(standard (behaviou?r|functionality|capability|jd ?e(dwards)?|feature|program|application|inquiry|report)?[^.]{0,80}"
    r"(not (yet )?(been )?(confirmed|verified|established|tested|checked|known)|unconfirmed|unverified|unknown|"
    r"could not (be )?(confirmed|verified|established|determined))|"
    r"(not (yet )?(confirmed|verified|established)|unconfirmed|unverified)[^.]{0,60}standard|"
    r"regardless of (whether )?(the )?standard|before (the )?standard (behaviou?r )?is (confirmed|known))", re.I)
_UNKNOWN_TARGET_RE = re.compile(r"^\s*$|\b(unknown|tbd|to be (confirmed|determined|decided)|not (yet )?known|\?{1,}|"
                                r"n/?a|pending|to follow)\b", re.I)
_AGENT_BUILD_RE = re.compile(r"\b(technical agent|jade|the agent)\b[^.]{0,60}\b(design\w*|build\w*|develop\w*|creat\w*|"
                             r"writ\w*|cod\w*|generat\w*)\b", re.I)
_HUMAN_BUILD_RE = re.compile(r"\b(developer|development team|programmer|cnc|person)\b[^.]{0,60}\b(build\w*|develop\w*|"
                             r"creat\w*|writ\w*|cod\w*)\b|\b(build\w*|develop\w*|creat\w*|cod\w*)\b[^.]{0,40}\bby (a |the )?"
                             r"(developer|development team|programmer)\b", re.I)
_OBJECT_RE = re.compile(r"\b([RPBNTVF]5[5-9]\w{2,}|UBE|report|program|application|business function|table|"
                        r"interactive application|batch version)\b", re.I)
_NEW_OBJECT_RE = re.compile(r"\b(new|custom)\s+(ube|report|program|application|business function|table|"
                            r"interactive application|object|batch)\b|\bcreate (a |an )?(new )?(custom )?(ube|report|"
                            r"program|application|business function|table)\b", re.I)
_DECISION_STEP_RE = re.compile(
    r"\b(decide|choose|determine (the )?(rate|threshold|mapping|value|percentage|amount)|"
    r"confirm (the )?(rate|threshold|mapping|percentage|amount|branch mapping)|"
    r"approve (the )?(combined |overall |technical |functional )?design)\b", re.I)
_APPROVE_DESIGN_RE = re.compile(r"\bapprove (the )?(combined |overall |technical |functional )?design\b", re.I)
_TECH_Q_RE = re.compile(
    r"\b(which (jd ?e(dwards)? )?(application|program|table|field|udc|code|value|version|report|data item|"
    r"processing option|line type|document type|stocking type)|what (is|are) the (field|udc|code|value|stocking type|"
    r"table)|test (data|item|record|order)|item number|stocking type|field value|data dictionary|udc\b|processing "
    r"option|standard (jd ?e(dwards)? )?(application|inquiry|report|program)|existing (report|program|application|"
    r"version)|\bF\d{4,5}\b|\bP\d{4,6}\b|\bR\d{4,6}\b)", re.I)
_BUSINESS_Q_RE = re.compile(r"\b(should|do you want|would you (like|prefer)|is it (acceptable|required|needed)|"
                            r"must|who (needs|uses|approves)|how often|by when|priority|preference|policy)\b", re.I)


def _id(kind: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return "ISS-" + hashlib.sha256(f"{kind}|{norm}".encode()).hexdigest()[:10]


def _short(text: str, limit: int = 300) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 3}


def _overlap(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa) if wa else 0.0


def is_decision_step(text: str) -> bool:
    """A step that decides scope (or approves the design): it belongs before
    authorisation, never in the implementation hand-off."""
    return bool(_DECISION_STEP_RE.search(text or ""))


def _issue(kind: str, title: str, consequence: str, action: str, action_id: Optional[str], owner: str = "product_manager",
           label: str = "Application Manager", tab: str = "solution") -> Issue:
    return Issue(id=_id(kind + "-quality", title), kind=kind, title=_short(title), consequence=_short(consequence, 400),
                 owner=owner, owner_label=label, action=action, action_id=action_id, blocks=True,
                 source="design_quality", tab=tab)


def design_quality_issues(change) -> list[Issue]:
    d = change.architect_decision
    if d is None:
        return []
    spec = change.implementation_spec
    seq = list(spec.sequence) if spec else []
    human = list(spec.human_actions_required) if spec else []
    result = d.result
    text_all = " ".join([d.existing_functionality_found or "", result.conclusion if result else "",
                         " ".join(result.uncertainty) if result else "", " ".join(seq), " ".join(human)])
    out: list[Issue] = []

    # F03 -- no custom development before standard behaviour is established.
    if d.recommended_route in _CUSTOM_ROUTES:
        m = _STANDARD_OPEN_RE.search(text_all)
        if m:
            out.append(_issue(
                "decision", "Standard behaviour is not established yet, so custom development is not justified.",
                f'The design itself says: "{_short(m.group(0), 200)}". Not found in the inspected records does not mean '
                "it does not exist.",
                "Run the Architect again for a targeted verification of the standard behaviour (name the test), "
                "before any custom route is authorised.", "rerun_solutioning"))

    # F03 -- an unknown target is not an exact specification.
    for c in (result.changes if result and not result.derived and d.recommended_route != "Resolve without Change" else []):
        if _UNKNOWN_TARGET_RE.search(c.proposed or ""):
                out.append(_issue(
                    "jade" if d.recommended_route == "Functional Agent" else "decision",
                    f"The target value of {c.what} is not known yet.",
                    "A change whose target is unknown is not an exact implementation specification.",
                    "Run the Architect again once the target is known (or answer the question that decides it).",
                    "rerun_solutioning"))

    # F06 -- one executor per build.
    agent_lines = [s for s in seq if _AGENT_BUILD_RE.search(s)]
    human_lines = [h for h in human + seq if _HUMAN_BUILD_RE.search(h)]
    for a in agent_lines:
        objs_a = {o.lower() for o in _OBJECT_RE.findall(a)}
        for h in human_lines:
            if h == a:
                continue
            objs_h = {o.lower() for o in _OBJECT_RE.findall(h)}
            shared = objs_a & objs_h
            if shared or (not objs_a and not objs_h):
                what = ", ".join(sorted(shared)) or "the same work"
                out.append(_issue(
                    "decision", f"The design names two different executors for building {what}.",
                    f'"{_short(a, 160)}" versus "{_short(h, 160)}".',
                    "Choose who builds it, then run the Architect again so the plan names one executor.",
                    "decide_issue"))
                break

    # F06 -- decisions that determine scope come before authorisation.
    for step in human + seq:
        if _APPROVE_DESIGN_RE.search(step):
            continue   # the approval is the gate itself, not a work item (filtered from the hand-off)
        if is_decision_step(step):
            finance = re.search(r"\b(rate|threshold|percentage|amount|g/?l|finance|price)\b", step, re.I)
            out.append(_issue(
                "decision", f"Decide before authorising: {step.rstrip('.')}.",
                "It determines what is built, so it cannot be an implementation task after authorisation.",
                "Record the decision on the story, then run the Architect again with it.", "decide_issue",
                owner="domain_owner" if finance else "product_manager",
                label="Finance" if finance else "Application Manager"))

    # F06 -- capability: the Technical Agent does not originate new objects.
    if d.recommended_route in ("Technical Agent", "Mixed") and any(
            _NEW_OBJECT_RE.search(s) and _AGENT_BUILD_RE.search(s) for s in seq):
        out.append(_issue(
            "decision", "The plan expects the Technical Agent to create a new object; it cannot.",
            "The Technical Agent prepares changes to existing customer objects from their uploaded source export. "
            "It never invents missing source.",
            "Assign the new object to a developer (JADE can prepare later changes to it from its export), or "
            "choose a route that changes an existing object; then run the Architect again.", "decide_issue"))
    # E7 -- low confidence is confirmed by a person before the route is authorised.
    threshold = confidence_threshold()
    if d.recommended_route != "Clarification Required" and d.confidence is not None and d.confidence < threshold:
        out.append(_issue(
            "decision", f"Confirm the route: JADE's confidence in \"{d.recommended_route}\" is {d.confidence:.2f}.",
            f"Below the confidence threshold of {threshold:.2f}: a person confirms the route before authorisation.",
            "Record that you agree with the route (or ask JADE on the solution why), then authorise.", "decide_issue"))
    seen: set[str] = set()
    return [i for i in out if not (i.id in seen or seen.add(i.id))]


DEFAULT_CONFIDENCE_THRESHOLD = 0.6


def confidence_threshold() -> float:
    """The confidence below which a route needs human confirmation (V11 §4.3).
    Default 0.6; JDE_ROUTE_CONFIDENCE_THRESHOLD overrides it."""
    import os

    try:
        return float(os.environ.get("JDE_ROUTE_CONFIDENCE_THRESHOLD", DEFAULT_CONFIDENCE_THRESHOLD))
    except ValueError:
        return DEFAULT_CONFIDENCE_THRESHOLD


# Repair brief 24 Oct, priority 1: technical investigation is JADE's work, whatever the wording.
# A question is technical investigation when it asks a FACT about JD Edwards artefacts (which object,
# table, report, field, version, value or test data; where something is held or configured), or when
# it says itself that the Architect/JADE/an agent should find it out. A question about intent, choice
# or policy stays with people.
_JDE_ARTEFACT = (r"(jd ?e(dwards)?|e1|object|table|file|report|program|application|form|field|column|data item|"
                 r"udc|user defined code|version|processing option|data selection|sequencing|aai|line type|"
                 r"document type|order type|stocking type|next number|business function|orchestration|"
                 r"\b[FPR]\d{4,6}\b|\b[PRF]5[5-9]\w*|test (data|item|record|order|customer|case)s?)")
_FACT_SEEKING_RE = re.compile(
    r"\b(which|what|where|whether|is there|are there|does (it|the|jd ?e)|do (we|they) have|exists?|holds?|stores?|"
    r"stored|contains?|configured|set up|currently|already|source)\b[^?.;]{0,80}" + _JDE_ARTEFACT
    + r"|" + _JDE_ARTEFACT + r"[^?.;]{0,60}\b(holds?|stores?|contains?|exists?|is (used|configured|set))\b", re.I)
_SELF_INVESTIGATION_RE = re.compile(
    r"\b(the )?(architect|jade|functional agent|technical agent|agent|discovery)\b[^.;]{0,40}\b(should|will|can|must|"
    r"needs? to|to)\b[^.;]{0,20}\b(resolve|investigate|confirm|read|check|determine|identify|verify|look up|find)", re.I)
_UNKNOWN_TECH_VALUE_RE = re.compile(
    r"\b(unknown|not (yet )?known|to be (determined|confirmed)|tbd|not determined|could not (be )?(read|identified|"
    r"determined|confirmed)|which (exact |target )?values?|what (exact |target )?values?)\b", re.I)


def classify_story_question(question: str) -> str:
    """'jade' for technical investigation JADE answers from discovery,
    'business' for intent and choices."""
    q = question or ""
    if _SELF_INVESTIGATION_RE.search(q):
        return "jade"
    if (_TECH_Q_RE.search(q) or _FACT_SEEKING_RE.search(q)) and not _BUSINESS_Q_RE.search(q):
        return "jade"
    return "business"


def is_technical_investigation(title: str, *, kind: str, options: list | None = None, text: str = "") -> bool:
    """For an Architect issue: True when it is JADE's own investigation rather than a person's decision.
    A 'decision' about technical values nobody knows yet, without real alternatives to choose from, is
    incomplete investigation -- not a choice for the Application Manager."""
    if classify_story_question(title) == "jade" or _SELF_INVESTIGATION_RE.search(text or ""):
        return True
    if kind == "decision" and len(options or []) < 2 and _UNKNOWN_TECH_VALUE_RE.search(f"{title} {text}") \
            and re.search(_JDE_ARTEFACT + r"|configur|setting|value", f"{title} {text}", re.I):
        return True
    return False


def discovery_answer(change, question: str) -> Optional[dict]:
    """The answer the Architect's discovery gave to this question, when its
    design records one (result.answered_questions)."""
    d = change.architect_decision
    answered = (d.result.answered_questions if d and d.result else None) or []
    best = None
    for a in answered:
        q = str(a.get("question", "")) if isinstance(a, dict) else ""
        score = _overlap(question, q)
        if score >= 0.6 and (best is None or score > best[0]):
            best = (score, a)
    if best is None:
        return None
    a = best[1]
    return {"answer": str(a.get("answer", "")).strip(), "evidence": str(a.get("evidence", "")).strip(),
            "at": d.decided_at}
