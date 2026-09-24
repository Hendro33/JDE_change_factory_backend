"""
Evidence limitations are explicit, never implied away.

  * A truncated artifact says how much was analysed, and a design citing it
    carries that limitation.
  * The full-result hash is a change detector: it changes when a value the
    model never saw changes, but Jade keeps no copy of that value.
  * Refresh Evidence records NEW observations and flags the existing design
    for reassessment. It never regenerates the design or re-approves the
    exact change.
"""

from __future__ import annotations

from ._discovery import ready_company, upload_artifact
from .conftest import headers
from .test_architect_discovery import STORY, _baselines, _run
from .test_stage1_execution_safeguards import _approved_story


def test_a_truncated_artifact_states_how_much_was_analysed(client, monkeypatch):
    ready_company(client, "vdb")
    big = "/* line */\n" * 8000  # 88,000 characters
    art = upload_artifact(client, "vdb", content=big)
    assert art["meta"]["analysis_coverage"] == {"analysed_chars": 60000, "total_chars": 88000, "truncated": True,
                                                "analysed": True}
    assert "TRUNCATED" in art["extractionNote"] and "60,000 of 88,000" in art["extractionNote"]
    _approved_story(STORY)
    seen = {}

    def script(tools):
        seen["a"] = tools.read_artifact(tools.list_artifacts()["artifacts"][0]["evidence_id"])
        return {"citations": [{"claim": "the function has no credit override", "evidence_ids": [seen["a"]["evidence_id"]],
                               "basis": "observed"}]}

    _run(monkeypatch, script)
    assert len(seen["a"]["content"]) == 60000
    assert seen["a"]["metadata"]["analysis_coverage"]["truncated"] is True
    manifest = _baselines(client)[0]["manifest"]
    assert manifest["artifacts"][0]["analysis_coverage"]["total_chars"] == 88000
    assert any("only 60,000 of 88,000 characters were analysed" in l for l in manifest["citations"][0]["limitations"])
    assert any("TRUNCATED" in l for l in manifest["confidence_limitations"])


def test_the_full_result_hash_detects_change_but_retains_nothing_unseen(client, monkeypatch):
    from jde_api_service.discovery import service, transport

    ready_company(client, "vdb", dataSharingPolicy="metadata_only")
    _approved_story(STORY)
    grant, _ = service.grant_for_story(STORY, "vdb", agent_run_id=None, actor_user_id="u-hendro")
    first = service.execute_read(grant, "processing_option_values", "P4210|CIQ0001")
    transport.simulated_estate("vdb")["processing_options"]["P4210|CIQ0001"]["PCREDCHK"] = "9"
    second = service.execute_read(grant, "processing_option_values", "P4210|CIQ0001")
    a, b = service.get_observation("vdb", first["observation_id"]), service.get_observation("vdb", second["observation_id"])
    # What the model saw -- and what Jade kept -- is identical: redacted structure only.
    assert a["evidence"]["records"] == b["evidence"]["records"]
    assert all(v.startswith("[redacted") for r in b["evidence"]["records"] for v in r.values())
    # The hash still detects that a hidden value changed.
    assert a["payload_sha256"] != b["payload_sha256"]

    _run(monkeypatch, lambda tools: (tools.read("processing_option_values", "P4210|CIQ0001"), {})[1])
    manifest = _baselines(client)[0]["manifest"]
    obs = manifest["observations"][0]
    assert obs["payload_sha256_role"] == "change_detector" and "redacted" in obs["retained"]
    assert any("not proof of any content" in n for n in manifest["evidence_notes"])


def test_refresh_records_new_observations_and_flags_without_regenerating_or_reapproving(client, monkeypatch):
    from jde_api_service.discovery import transport
    from jde_api_service.services.registry import get_architecture_review_service
    from jde_mcp_server import approval
    from jde_mcp_server.design_baseline import get_design_baseline

    from .test_stage1_execution_safeguards import _approve, _full_scope, _propose, _save_scope

    ready_company(client, "vdb")
    _save_scope(client, "vdb", _full_scope())
    _approved_story(STORY)
    _run(monkeypatch, lambda tools: (tools.read("processing_option_values", "P4210|CIQ0001"), {})[1])
    change = _propose(STORY)
    approved = _approve(change["change_id"])
    history_before = [v.model_dump() for v in get_architecture_review_service().get(STORY).history]
    first = _baselines(client)[0]

    transport.simulated_estate("vdb")["processing_options"]["P4210|CIQ0001"]["PCREDCHK"] = "0"
    r = client.post(f"/changes/{STORY}/architecture-review/refresh-evidence", headers=headers("vdb")).json()

    old_ids = {o["observation_id"] for o in first["manifest"]["observations"]}
    new_ids = {o["observation_id"] for o in r["manifest"]["observations"]}
    assert new_ids and not new_ids & old_ids  # new observations, not edited ones
    assert r["manifest"]["refresh_changes"][0]["previous"] in old_ids
    assert r["status"] == "needs_reassessment" and r["reassessment"][0]["kind"] == "observation_changed"
    assert "nothing was regenerated or re-approved" in r["manifest"]["refresh_note"]
    # The design itself is untouched: same analysis history, no new version.
    assert [v.model_dump() for v in get_architecture_review_service().get(STORY).history] == history_before
    # The exact change is not re-approved (same approval, same time, same approver).
    record = approval._load(change["change_id"])
    assert (record["status"], record["approved_at"], record["approved_by"]) == (
        "approved", approved["approved_at"], approved["approved_by"])
    # The earlier baseline is kept, byte for byte.
    assert _baselines(client)[-1]["manifestSha256"] == first["manifestSha256"]
    # Downstream agents see the flag.
    assert get_design_baseline(STORY)["status"] == "needs_reassessment"
