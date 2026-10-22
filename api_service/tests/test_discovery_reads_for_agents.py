"""
Reads an agent can actually use: common operator spellings and LIKE are
translated instead of refused, a whole code table fits in one read, a column
that comes back empty everywhere is flagged as a probable wrong alias, and a
refused read the agent later corrected is not turned into a question for
people.
"""

from __future__ import annotations

from ._discovery import ready_company, sim_edit
from .test_discovery_policy import _grant
from .test_stage1_execution_safeguards import _approved_story


def test_operator_spellings_and_like_are_translated():
    from jde_api_service.discovery import capabilities

    got = capabilities.normalise_filters([
        {"field": "LNTY", "op": "eq", "value": "S"}, {"field": "LNTY", "op": "NE", "value": "S"},
        {"field": "DSC1", "op": "like", "value": "%frame%"}, {"field": "LITM", "op": "LIKE", "value": "20%"},
        {"field": "LITM", "op": "like", "value": "%01"}, {"field": "X", "op": "regex", "value": "a"}])
    assert [(f["op"], f["value"]) for f in got] == [
        ("=", "S"), ("<>", "S"), ("contains", "frame"), ("begins_with", "20"), ("ends_with", "01"), ("regex", "a")]


def test_a_whole_code_table_reads_in_one_go_with_eq_and_like(client):
    from jde_api_service.discovery import service

    ready_company(client, approvedReads=[])
    _approved_story("S-RA-1")
    with sim_edit("vdb", "line types") as est:
        est["tables"]["F40205"] = [{"LNTY": f"L{i}", "LNDS": f"Line type {i}", "GLI": "Y"} for i in range(60)]
    ev = service.execute_read(_grant("S-RA-1"), "table_browse", "F40205", ["LNTY", "LNDS"], [], 200)
    assert ev["record_count"] == 60 and not ev["more_records_available"]
    ev = service.execute_read(_grant("S-RA-1"), "table_browse", "F40205", ["LNTY"],
                              [{"field": "LNTY", "op": "like", "value": "L1%"}], 50)
    assert ev["record_count"] == 11 and ev["filters"][0]["op"] == "begins_with"
    ev = service.execute_read(_grant("S-RA-1"), "table_browse", "F40205", ["LNTY"],
                              [{"field": "LNTY", "op": "eq", "value": "L7"}], 5)
    assert ev["record_count"] == 1


def test_an_empty_column_is_flagged_as_a_probable_wrong_alias(client):
    from jde_api_service.discovery import service

    ready_company(client, approvedReads=[])
    _approved_story("S-RA-2")
    with sim_edit("vdb", "line types") as est:
        est["tables"]["F40205"] = [{"LNTY": "S", "LNDS": "Stock"}, {"LNTY": "N", "LNDS": "Non-stock"}]
    ev = service.execute_read(_grant("S-RA-2"), "table_browse", "F40205", ["LNTY", "DL01"], [], 10)
    assert len(ev["column_notes"]) == 1 and ev["column_notes"][0].startswith("DL01 returned no value")


def test_a_corrected_read_leaves_no_gap_and_an_uncorrected_one_is_not_an_admin_question():
    from jde_api_service.discovery import baseline

    ledger = baseline.RunLedger(grant=None, no_grant_reason="")
    ledger.grant = object()
    ledger.add_blocked("table_browse", "F40205", "operator 'regex' is not allowed")
    ledger.add_observation({"capability_id": "table_browse", "target": "F40205", "observation_id": "OBS-1",
                            "sharing": {"values_shared": True}})
    ledger.add_blocked("table_browse", "F0012", "operator 'regex' is not allowed")
    ledger.add_blocked("table_browse", "F0012", "operator 'regex' is not allowed")
    gaps, _c, _l = baseline._system_gaps(ledger, None)
    reads = [g for g in gaps if "was not read" in g["description"]]
    assert len(reads) == 1 and "F0012" in reads[0]["description"]
    assert reads[0]["owner"] == "jade" and "Admin" not in reads[0]["question"]


def test_the_engagement_scope_is_a_citable_source():
    from jde_api_service.discovery import baseline

    ledger = baseline.RunLedger(grant=None, no_grant_reason="")
    cites, gaps = baseline._validate_citations(
        [{"claim": "GL posting and AAI configuration is a protected category", "evidence_ids": ["ENGAGEMENT_SCOPE"],
          "basis": "customer_attestation"}], ledger, None)
    assert cites[0]["validated"] and not gaps
