from __future__ import annotations

from jde_mcp_server import backlog

from .conftest import headers


def _make_backlog_story(story_id: str = "S-TEST-1") -> None:
    backlog.propose_to_backlog(
        story_id,
        "As a clerk I want the default document type fixed",
        {"financial_impact": "about 2h/week", "operational_reach": "", "risk_compliance": "", "strategic_alignment": "", "urgency": ""},
        "Low",
        source="Support / Topdesk",
    )


def _link(client, story_id: str, customer_id: str) -> None:
    from jde_api_service.services.registry import get_customer_link_service

    get_customer_link_service().link(story_id, customer_id)


def test_unlinked_story_is_invisible_everywhere_fail_safe(client):
    _make_backlog_story("S-UNLINKED")
    r = client.get("/changes", headers=headers())
    assert r.json() == []
    r = client.get("/changes/S-UNLINKED", headers=headers())
    assert r.status_code == 404
    r = client.get("/backlog", headers=headers())
    assert r.json() == []


def test_linked_pending_story_appears_in_backlog_not_in_approved_state(client):
    _make_backlog_story("S-PENDING")
    _link(client, "S-PENDING", "vdb")

    r = client.get("/backlog", headers=headers())
    assert len(r.json()) == 1
    assert r.json()[0]["id"] == "S-PENDING"
    assert r.json()[0]["state"] == "BACKLOG_READY"

    r = client.get("/changes/S-PENDING", headers=headers())
    assert r.json()["storyApproval"] is None


def test_approved_story_carries_story_approval_and_leaves_backlog(client):
    _make_backlog_story("S-APPROVED")
    backlog.approve("S-APPROVED", "Ellen Vos", "clear case")
    _link(client, "S-APPROVED", "vdb")

    r = client.get("/changes/S-APPROVED", headers=headers())
    body = r.json()
    assert body["state"] == "APPROVED"
    assert body["storyApproval"]["status"] == "approved"
    assert body["storyApproval"]["approvedBy"] == "Ellen Vos"

    # No longer pending review, so it must not still show in /backlog.
    r = client.get("/backlog", headers=headers())
    assert r.json() == []


def test_rejected_story_carries_rejection_reason(client):
    _make_backlog_story("S-REJECTED")
    backlog.reject("S-REJECTED", "Ellen Vos", "duplicate of an existing change")
    _link(client, "S-REJECTED", "vdb")

    r = client.get("/changes/S-REJECTED", headers=headers())
    body = r.json()
    assert body["state"] == "REJECTED"
    assert body["storyApproval"]["status"] == "rejected"
    assert body["storyApproval"]["note"] == "duplicate of an existing change"


def test_evidence_entries_are_surfaced_on_the_change(client):
    from jde_mcp_server.evidence import capture_evidence

    _make_backlog_story("S-EVIDENCE")
    _link(client, "S-EVIDENCE", "vdb")
    capture_evidence("S-EVIDENCE", {"stage": "Intake", "detail": "Captured from Topdesk #4521", "actor": "AI Intake"})
    capture_evidence("S-EVIDENCE", {"stage": "Quality gate", "detail": "Passed all criteria", "actor": "AI Check"})

    r = client.get("/changes/S-EVIDENCE", headers=headers())
    entries = r.json()["evidence"]
    assert [e["stage"] for e in entries] == ["Intake", "Quality gate"]
    assert entries[0]["entryId"] == "E1"
    assert entries[0]["prevHash"] == "GENESIS"


def test_malformed_complexity_signal_falls_back_to_unknown_instead_of_crashing(client):
    # Regression: a real Check Agent run once passed a full sentence
    # ("Unknown. Could not confirm...") instead of the exact enum
    # token -- backlog.py stores whatever string a tool caller passes,
    # with no validation of its own, and reading it straight into the
    # strict Complexity literal crashed the whole request with a 500
    # that the browser reported as a misleading CORS failure.
    _make_backlog_story("S-MALFORMED-COMPLEXITY")
    _link(None, "S-MALFORMED-COMPLEXITY", "vdb")

    from jde_mcp_server import backlog as backlog_module
    import json
    import os

    path = os.path.join(backlog_module.BACKLOG_DIR, "S-MALFORMED-COMPLEXITY.json")
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    record["rough_complexity_signal"] = "Unknown. Could not confirm without further discovery."
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f)

    r = client.get("/changes/S-MALFORMED-COMPLEXITY", headers=headers())
    assert r.status_code == 200
    assert r.json()["complexitySignal"] == "Unknown"


def test_metrics_and_activity_are_derived_not_hardcoded(client):
    r = client.get("/metrics", headers=headers())
    assert r.json()["totals"][0]["value"] == 0

    _make_backlog_story("S-METRICS")
    _link(client, "S-METRICS", "vdb")

    r = client.get("/metrics", headers=headers())
    assert r.json()["totals"][0]["value"] == 1
    assert r.json()["pipeline"][2]["stage"] == "Awaiting approval"
    assert r.json()["pipeline"][2]["count"] == 1

    r = client.get("/activity", headers=headers())
    assert len(r.json()) == 1
    assert r.json()[0]["changeId"] == "S-METRICS"
    # A display string for the UI, not a raw ISO timestamp for it to parse.
    assert "T" not in r.json()[0]["time"]
