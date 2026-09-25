"""
Approval and baseline consistency (jde_mcp_server/binding.py,
services/work_invalidation.py).

An exact-change approval is bound to the design revision, evidence baseline,
artifact revisions and target before-state it was given against. The
approval record is history and is never edited; execution eligibility is
recomputed before every dispatch, and a later baseline or design is never
silently substituted into approved work.
"""

from __future__ import annotations

import pytest

from ._discovery import ready_company, sim_edit, upload_artifact
from .conftest import headers
from .test_architect_discovery import _run
from .test_stage1_execution_safeguards import _approve, _approved_story, _execute, _full_scope, _propose, _save_scope


def _design_with_change(client, monkeypatch, story: str, *, reads=(("processing_option_values", "P4210|CIQ0001"),),
                        artifact=None) -> dict:
    """A scripted Architect run that reads, optionally consults an artifact,
    and proposes the change -- then a human approves it."""
    proposed = {}

    def architect(tools):
        for capability, target in reads:
            tools.read(capability, target)
        if artifact:
            tools.read_artifact(artifact["artifactId"], artifact["revision"])
        proposed.update(_propose(story))
        return {}

    _run(monkeypatch, architect, story=story)
    return proposed


def _setup(client, story: str) -> None:
    ready_company(client)
    _save_scope(client, "vdb", _full_scope())
    _approved_story(story)


def _preflight(client, story: str) -> dict:
    return client.get(f"/changes/{story}/execution/preflight", headers=headers("vdb")).json()


def test_the_approval_records_the_design_baseline_and_before_state_it_was_given_against(client, monkeypatch):
    from jde_mcp_server.design_baseline import get_design_baseline

    _setup(client, "S-BIND-1")
    change = _design_with_change(client, monkeypatch, "S-BIND-1")
    record = _approve(change["change_id"])
    package = get_design_baseline("S-BIND-1")
    design = record["binding"]["design"]
    assert (design["design_revision"], design["baseline_id"], design["manifest_sha256"]) == (
        package["design_revision"], package["baseline_id"], package["manifest_sha256"])
    assert record["binding"]["before_state"] == {
        "known": True, "value": "S3", "target": "P4210/CIQ0001/PDOCTYPE", "source": "simulated DEV estate read (SIMULATION)"}
    assert record["binding"]["depends_on"]["targets"] == ["processing_option_values:P4210|CIQ0001"]
    assert _preflight(client, "S-BIND-1")["executable"] is True


def test_changing_an_approved_targets_value_blocks_dispatch(client, monkeypatch):
    from jde_mcp_server import approval, execution
    from jde_mcp_server.binding import BindingInvalid

    _setup(client, "S-BIND-DRIFT")
    change = _design_with_change(client, monkeypatch, "S-BIND-DRIFT")
    _approve(change["change_id"])
    with sim_edit("vdb", "drift: someone sets PDOCTYPE to SQ in DEV after approval") as est:
        est["processing_options"]["P4210|CIQ0001"]["PDOCTYPE"] = "SQ"
    with pytest.raises(BindingInvalid, match="target changed since approval: it was 'S3' when approved and is now 'SQ'"):
        _execute("S-BIND-DRIFT", change["change_id"])
    record = approval._load(change["change_id"])
    assert execution.effective_state(record) == "ready" and not (record.get("execution") or {}).get("write")
    pre = _preflight(client, "S-BIND-DRIFT")
    assert pre["executable"] is False
    assert any("target changed since approval" in c["detail"] for c in pre["checks"] if not c["ok"])
    # The history is intact: still approved, same time, same approver.
    assert (record["status"], record["approved_by"]) == ("approved", "Hendro")


def test_a_change_the_design_did_not_propose_cannot_be_approved_against_it(client, monkeypatch):
    from jde_mcp_server.binding import BindingInvalid

    _setup(client, "S-BIND-OTHER")
    _design_with_change(client, monkeypatch, "S-BIND-OTHER")
    stray = _propose("S-BIND-OTHER")  # proposed outside the design
    with pytest.raises(BindingInvalid, match="not the change the story's current design"):
        _approve(stray["change_id"])


def test_a_design_flagged_for_reassessment_cannot_have_work_approved(client, monkeypatch):
    from jde_mcp_server.binding import BindingInvalid

    _setup(client, "S-BIND-FLAG")
    change = _design_with_change(client, monkeypatch, "S-BIND-FLAG")
    with sim_edit("vdb", "drift before approval") as est:
        est["processing_options"]["P4210|CIQ0001"]["PCREDCHK"] = "0"
    client.post("/changes/S-BIND-FLAG/architecture-review/refresh-evidence", headers=headers("vdb"))
    with pytest.raises(BindingInvalid, match="invalidated before approval|flagged for reassessment"):
        _approve(change["change_id"])


def test_a_newer_design_is_never_substituted_into_an_earlier_approval(client, monkeypatch):
    _setup(client, "S-BIND-NEWER")
    first = _design_with_change(client, monkeypatch, "S-BIND-NEWER")
    _approve(first["change_id"])
    _design_with_change(client, monkeypatch, "S-BIND-NEWER")  # the Architect runs again: design revision 2
    pre = _preflight(client, "S-BIND-NEWER")
    detail = " ".join(c["detail"] for c in pre["checks"] if not c["ok"])
    # The preflight looks at the story's latest change (pending); the old approved one is checked directly.
    from jde_mcp_server import approval, binding

    problems = binding.problems(approval._load(first["change_id"]))
    assert any("approved against design revision 1, but the story's design is now revision 2" in p for p in problems)
    assert pre["executable"] is False and detail


def test_refresh_that_does_not_touch_the_changes_dependencies_leaves_it_eligible(client, monkeypatch):
    from jde_mcp_server import approval

    _setup(client, "S-BIND-NONMAT")
    change = _design_with_change(client, monkeypatch, "S-BIND-NONMAT",
                                 reads=(("processing_option_values", "P4210|CIQ0001"), ("udc_values", "00/DT")))
    _approve(change["change_id"])
    with sim_edit("vdb", "a new UDC value -- unrelated to this change's target") as est:
        est["tables"]["F0005"].append({"DRSY": "00", "DRRT": "DT", "DRKY": "SX", "DRDL01": "New", "DRSPHD": ""})
    r = client.post("/changes/S-BIND-NONMAT/architecture-review/refresh-evidence", headers=headers("vdb")).json()
    assert r["status"] == "needs_reassessment"  # the design as a whole is flagged for a person to review...
    assert r["manifest"]["affected_work"] == []  # ...but this work does not depend on what changed
    assert not approval._load(change["change_id"]).get("invalidations")
    assert _preflight(client, "S-BIND-NONMAT")["executable"] is True
    _execute("S-BIND-NONMAT", change["change_id"])


def test_a_new_revision_of_an_artifact_the_design_used_invalidates_the_approval(client, monkeypatch):
    from jde_mcp_server import approval

    _setup(client, "S-BIND-ART")
    art = upload_artifact(client)
    change = _design_with_change(client, monkeypatch, "S-BIND-ART", artifact=art)
    record = _approve(change["change_id"])
    assert record["binding"]["depends_on"]["artifacts"] == [art["artifactId"]]
    upload_artifact(client, content="/* revised export */\n")
    after = approval._load(change["change_id"])
    assert after["invalidations"][0]["kind"] == "artifact_revised"
    assert after["approved_at"] == record["approved_at"]  # history untouched
    assert _preflight(client, "S-BIND-ART")["executable"] is False


def test_an_approval_from_before_bindings_existed_is_not_eligible(client):
    from jde_mcp_server.binding import BindingInvalid

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S-BIND-LEGACY")
    change = _propose("S-BIND-LEGACY")
    _approve(change["change_id"])
    from jde_mcp_server import approval

    record = approval._load(change["change_id"])
    record.pop("binding")  # as recorded before this increment
    approval._save(change["change_id"], record)
    with pytest.raises(BindingInvalid, match="no recorded approval basis"):
        _execute("S-BIND-LEGACY", change["change_id"])
