"""The further reference journeys of the redesign (post-step-3 review).

The design names them: a technical package with manual build and activation;
a requirement resolved without a system change; a failed or uncertain
execution that needs recovery; and an existing change already midway through
delivery. (The agent-executed configuration change is the browser journey
e2e/journeys/story_journey.py.) Each journey is walked through the real API,
and at every step it checks what the redesigned screens read: whose move it
is and what it is, the current authorisation, the work items, "What needs to
happen" without repeated open points, when the step began, and the outcome.
"""

from __future__ import annotations

import time

from ._technical import approve_package, cnc, prepare, ready_story, record_apply, record_build, record_verify
from .conftest import accept_outcome, headers


def _lc(client, story: str) -> dict:
    return client.get(f"/changes/{story}", headers=headers("vdb")).json()["lifecycle"]


def _screen_rules(lc: dict) -> None:
    """What every screen relies on, at every step of every journey."""
    na = lc["nextAction"]
    assert na["summary"]
    if na["kind"] != "none" and na["owner"] not in ("jade", "none"):
        assert na["action"], f"a decision or task without a control: {na}"
    # An open point is said once: "What needs to happen" lines each restate a different one.
    ids = [w["issueId"] for w in lc["whatNext"] if w.get("issueId")]
    assert len(ids) == len(set(ids))
    assert lc["since"], "when the current step began must be recorded"


def test_journey_technical_package_with_manual_build_and_activation(client, monkeypatch):
    s = "S-JRN-TECH"
    ready_story(client, monkeypatch, s)
    prepare("vdb", s)
    lc = _lc(client, s)
    _screen_rules(lc)
    assert lc["nextAction"]["action"] == "approve_package" and lc["nextAction"]["owner"] == "product_manager"
    steps = [(approve_package, "record_technical_apply"), (record_apply, "record_technical_build"),
             (record_build, "record_cnc"), (cnc, "record_technical_verify"), (record_verify, "accept_outcome")]
    previous = lc["since"]
    for step, expected in steps:
        time.sleep(0.01)
        r = step(client, s, 1)
        assert r.status_code == 200 if hasattr(r, "status_code") else True
        lc = _lc(client, s)
        _screen_rules(lc)
        assert lc["nextAction"]["action"] == expected, (step.__name__, lc["nextAction"])
        assert lc["since"] >= previous
        previous = lc["since"]
    # The package milestones are the people's work items, each done, then the business acceptance.
    groups = {(w["group"], w["state"]) for w in lc["workItems"]}
    assert ("development", "done") in groups and ("acceptance", "todo") in groups
    accept_outcome(client, s)
    lc = _lc(client, s)
    _screen_rules(lc)
    assert lc["nextAction"]["action"] == "finalise_asbuilt"
    assert lc["outcomeCard"]["devVerified"]["done"] and lc["outcomeCard"]["businessAccepted"]["done"]
    assert not lc["outcomeCard"]["released"]["done"]


def test_journey_resolved_without_a_system_change(client, monkeypatch):
    from ._technical import technical_design
    from .test_stage1_execution_safeguards import _approved_story

    s = "S-JRN-NOCHG"
    _approved_story(s)
    technical_design(client, monkeypatch, s, route="Resolve without Change")
    lc = _lc(client, s)
    _screen_rules(lc)
    # Before: a decision with no control (the story waited for ever). Now: the Application Manager confirms.
    assert lc["nextAction"]["action"] == "confirm_no_change" and lc["nextAction"]["owner"] == "product_manager"
    r = client.post(f"/changes/{s}/resolve-without-change", headers=headers("vdb"), json={"note": "Agreed."})
    assert r.status_code == 200, r.text
    lc = r.json()["lifecycle"]
    assert lc["phase"] == "done" and lc["outcome"] == "resolved_without_change"
    # Nothing to authorise and nothing was changed in JD Edwards.
    assert lc["authorisation"] is None and not lc["workItems"] and lc["outcomeCard"] is None
    # A second confirmation is refused: the ending is terminal.
    assert client.post(f"/changes/{s}/resolve-without-change", headers=headers("vdb"), json={"note": ""}).status_code == 409


def test_journey_uncertain_execution_needs_recovery(client):
    from .test_audited_reconciliation import _unknown_write

    s = "S-JRN-UNK"
    _unknown_write(client, s, reached_jde=True, connected=True)
    lc = _lc(client, s)
    _screen_rules(lc)
    assert lc["health"] in ("blocked", "failed") and lc["nextAction"]["owner"] == "product_manager"
    assert "reconcile" in lc["nextAction"]["action"]
    # The authorisation was used by the attempt: it is not offered for renewal.
    assert lc["authorisation"]["state"] == "used" and not lc["authorisation"]["renewable"]
    r = client.post(f"/changes/{s}/execution/reconcile", headers=headers("vdb"), json={"note": "checked in DEV"})
    assert r.status_code == 200, r.text
    lc = _lc(client, s)
    _screen_rules(lc)
    assert lc["phase"] == "validation" and lc["nextAction"]["action"] == "run_or_record_test"


def test_journey_change_already_midway_through_delivery(client):
    """A change approved and partly delivered before the redesign keeps its
    records, approval and evidence; the new projections read them as they are."""
    from .test_recorded_delivery import _applied, _change as approved_change
    from .fixtures.fake_ais import apply_in_dev

    s = "S-JRN-MID"
    approved_change(client, s)
    lc = _lc(client, s)
    _screen_rules(lc)
    assert lc["authorisation"]["state"] == "valid" and lc["nextAction"]["action"] == "record_applied"
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    assert _applied(client, s).status_code == 200
    lc = _lc(client, s)
    _screen_rules(lc)
    assert lc["phase"] == "validation" and lc["authorisation"]["state"] == "used"
    assert any(w["state"] == "done" for w in lc["workItems"])
