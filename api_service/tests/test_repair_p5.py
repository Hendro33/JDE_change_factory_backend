"""Repair brief 24 Oct, priority 5: meaningful metrics and outcomes."""

from __future__ import annotations

from jde_api_service.services.work_items import _business_headline, _verified_line

from .conftest import headers


class _US:
    statement = "As a planner I want the pick slip to show remaining shelf life so that short-dated lots are not shipped."


class _C:
    user_story = _US
    title = "Shelf life on pick slips"


def test_an_outcome_is_described_in_business_terms():
    assert _business_headline("The requirement is met by setting it to 1.", _C) == _US.statement
    assert _business_headline("Processing option PSHELF of P42520 is set to Y.", _C) == _US.statement
    assert _business_headline("Pick slips now show each lot's remaining shelf life.", _C) == \
        "Pick slips now show each lot's remaining shelf life."


def test_who_or_what_verified_it_is_named():
    assert _verified_line({"passed": True, "source": "recorded by a person", "by": "Sanne", "evidence_reference": "T-1"}) \
        == "Test in DEV recorded by Sanne: passed (evidence: T-1)"
    assert _verified_line({"passed": True, "source": "live orchestration", "orchestration": "ORCH_SO"}) \
        == "Checked by JADE in DEV with ORCH_SO: passed"


def test_health_of_open_work_leaves_out_finished_stories(client):
    r = client.get("/metrics", headers=headers("vdb"))
    assert r.status_code == 200, r.text
    labels = [h["stage"] for h in r.json()["health"]]
    assert "Delivered" not in labels and "Closed" not in labels
