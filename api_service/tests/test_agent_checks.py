"""Register W3: the Architect's open points are checked before people see
them (E6), low confidence is confirmed by a person (E7), decisions carry real
options routed by kind (E4, E17)."""

from __future__ import annotations

from jde_api_service.models.change import ArchitectDecision, Change
from jde_api_service.models.issues import AgentResult, ResultIssue
from jde_api_service.services import issues, lifecycle

from .test_owned_issues import _change, _proposed, facts  # noqa: F401 -- fixture


def _derive(result: AgentResult, confidence: float = 0.8) -> object:
    d = ArchitectDecision(recommended_route="Functional Agent", confidence=confidence,
                          decided_at="2026-10-01T00:00:00+00:00", result=result)
    return lifecycle.derive(_change(architect_decision=d, business_domain_id="DOM-SALES", **_proposed()))


def test_e6_a_question_about_how_jde_is_set_up_never_reaches_a_person(facts):
    r = AgentResult(conclusion="x", issues=[
        ResultIssue(title="Which stocking type value marks a non-stock item?", owner="business", blocks=True),
        ResultIssue(title="Should zero-stock items be in the export?", owner="business", blocks=True)])
    lc = _derive(r)
    by = {i.title: i for i in lc.issues}
    tech = by["Which stocking type value marks a non-stock item?"]
    assert tech.owner == "jade" and tech.returned and tech.blocks   # repair brief: the blocker is kept, with JADE
    assert by["Should zero-stock items be in the export?"].owner == "domain_owner"
    change = _change(architect_decision=ArchitectDecision(recommended_route="Functional Agent", confidence=0.8,
                                                          decided_at="2026-10-01T00:00:00+00:00", result=r))
    block = issues.returned_for_prompt(change)
    assert "Which stocking type value" in block and "zero-stock" not in block


def test_e7_low_confidence_needs_a_person_to_confirm_the_route(facts):
    lc = _derive(AgentResult(conclusion="x"), confidence=0.4)
    confirm = [i for i in lc.issues if i.source == "design_quality" and i.title.startswith("Confirm the route")]
    assert confirm and confirm[0].blocks and confirm[0].owner == "product_manager"
    assert lc.next_action.action != "approve_exact_change"
    assert not [i for i in _derive(AgentResult(conclusion="x"), confidence=0.8).issues
                if i.title.startswith("Confirm the route")]


def test_e4_a_decision_keeps_its_options(facts):
    r = issues.parse_result({"conclusion": "x", "issues": [{
        "title": "Which line type should freight use?", "owner": "decision", "blocks": True,
        "options": ["Reuse line type F (no new set-up)", "Create line type FR (separate revenue reporting)"],
        "proposed_answer": "Reuse line type F"}]})
    lc = _derive(r)
    d = next(i for i in lc.issues if i.kind == "decision" and i.source == "architect")
    assert d.options == ["Reuse line type F (no new set-up)", "Create line type FR (separate revenue reporting)"]
