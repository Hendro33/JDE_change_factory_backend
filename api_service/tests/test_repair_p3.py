"""Repair brief 24 Oct, priority 3: decisions direct, summaries accurate, no duplicate approval tasks."""

from __future__ import annotations

import pytest

from jde_api_service.services.design_quality import is_decision_step
from jde_api_service.services.lifecycle import _stopped_technical
from jde_api_service.technical.service import missing_items

REPORT = ("I ran the full prepare-workflow investigation for CR-HW-013 against the approved design (revision 2, "
          "DA-a483997400, baseline BL-f4a9ac0174) and found that no package can safely be prepared yet, for reasons "
          "independent of the Architect's earlier session (I verified this with my own tool calls in this run). "
          "1. The source of business function B5542001 is not among the authorised sources. "
          "2. No approved test version exists for R42565.")


def test_missing_prerequisites_are_named_not_the_cut_off_report():
    assert missing_items({"explanation": REPORT}) == [
        "The source of business function B5542001 is not among the authorised sources.",
        "No approved test version exists for R42565."]
    assert missing_items({"explanation": REPORT, "missing": ["Source of B5542001 -- upload it"]}) == [
        "Source of B5542001 -- upload it"]


def test_the_next_action_names_what_is_missing_and_keeps_the_full_list():
    stopped = {"kind": "blocked_missing_evidence", "label": "The Technical Agent needs evidence it does not have",
               "explanation": REPORT, "questions": [], "missing": missing_items({"explanation": REPORT}), "changed": []}
    lc = _stopped_technical(stopped, [], "Technical Agent")
    s = lc.next_action.summary
    assert "B5542001" in s and "and 1 more" in s and not s.rstrip().endswith("1.")
    assert lc.open_items == stopped["missing"]


@pytest.mark.parametrize("step", ["Approve the design", "Obtain design approval from the Application Manager",
                                  "Application Manager approves design revision 2", "Get the technical design signed off"])
def test_approving_the_design_is_never_an_implementation_task(step):
    assert is_decision_step(step)


@pytest.mark.parametrize("step", ["Implement the design in OMW", "Build and deploy the package", "Check in B5542001"])
def test_real_implementation_steps_stay(step):
    assert not is_decision_step(step)
