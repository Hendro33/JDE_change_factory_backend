"""
Every agent role may read the documents its Start-up Pack references -- the
Technical Agent and the Process Analyst included -- and whatever they rely on
from a document is cited and checked against what their run actually read
(knowledge/tools.verify_citations), never taken on the model's word.
"""

from __future__ import annotations

from ._technical import OLD_LINE, TESTS, ready_story
from .conftest import headers

KNOWLEDGE = ("mcp__jade-knowledge__list_documents", "mcp__jade-knowledge__read_document")
READ = [{"action": "read", "document": "doc:coding-standards@r2",
         "sections": ["[Coding standards.pdf, page 3]", "[Coding standards.pdf, page 4]"]}]


def _template(client, role: str) -> dict:
    from jde_api_service.ai import packs

    pack = next(p for p in client.get("/admin/ai/packs", headers=headers("vdb")).json()["packs"]
                if p["packId"] == f"tpl-{role}")
    rev = max(r["revision"] for r in pack["revisions"] if r["status"] == "published")
    return packs.get_revision("vdb", pack["packId"], rev)


def test_the_technical_agent_and_process_analyst_may_read_their_packs_documents(client):
    from jde_api_service.ai import packs

    for role in ("technical-agent", "process-analyst"):
        assert set(KNOWLEDGE) <= set(packs.ceiling(role)), role
        tpl = _template(client, role)
        assert tpl["content"]["knowledge"] == ["request_documents"], role
        assert set(KNOWLEDGE) <= set(tpl["content"]["capabilities"]), role
        snap = packs.PackSnapshot(role=role, pack_id=tpl["packId"], pack_name=tpl["name"], revision=tpl["revision"],
                                  sha256=tpl["sha256"], content=tpl["content"])
        assert set(KNOWLEDGE) <= set(snap.effective_tools(documents_allowed=True))
        # The customer's document policy still decides: metadata only lists, never reads.
        tools = snap.effective_tools(documents_allowed=False)
        assert KNOWLEDGE[0] in tools and KNOWLEDGE[1] not in tools


def test_a_pack_still_cannot_go_beyond_the_ceiling(client):
    from jde_api_service.ai import packs

    content = {**_template(client, "process-analyst")["content"],
               "capabilities": [*KNOWLEDGE, "mcp__jde-change-factory__propose_change"]}
    try:
        packs.normalise("process-analyst", content, "vdb")
    except packs.InvalidPack as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("a tool outside the ceiling was accepted")


def test_a_technical_package_marks_only_citations_its_run_actually_read_as_verified(client, monkeypatch):
    from jde_api_service.technical import service, store
    from jde_api_service.technical.tools import TechnicalAgentTools

    ready_story(client, monkeypatch, "S-DOC-TECH")
    run_id = service.start_run("vdb", "S-DOC-TECH", purpose="prepare", initiated_by="u-hendro")["run_id"]
    tools = TechnicalAgentTools(company_id="vdb", story_id="S-DOC-TECH", run_id=run_id, knowledge_log=READ)
    source = next(a for a in tools.list_source_artifacts()["artifacts"] if a["format"] == "c_source")
    opened = tools.open_in_workspace(source["evidence_id"])
    assert tools.replace(opened["file_id"], OLD_LINE, OLD_LINE[:-1] + " && lpDS->cCreditExempt != 'Y')")["changed"]
    out = tools.submit({"explanation": "credit-exempt customers are excluded from the C1 hold",
                        "requirement_trace": [{"requirement": "exempt customers not held", "how": "extra condition"}],
                        "test_plan": TESTS, "recovery": "restore the previous source",
                        "document_citations": [
                            {"claim": "Error handling follows the customer's standard", "source": "[Coding standards.pdf, page 3]"},
                            {"claim": "Naming uses the CIQ prefix", "source": "[Naming.pdf, page 1]"},
                            "not a citation"]})
    assert out["submitted"], out
    package = store.get_package("vdb", "S-DOC-TECH")
    assert package["content"]["document_citations"] == [
        {"claim": "Error handling follows the customer's standard", "source": "[Coding standards.pdf, page 3]", "verified": True},
        {"claim": "Naming uses the CIQ prefix", "source": "[Naming.pdf, page 1]", "verified": False},
    ]
    # A reported outcome carries checked citations too.
    rec = tools.report_outcome("blocked_missing_evidence", "the interface specification is missing", ["please upload it"],
                               [{"claim": "the standard requires an interface spec", "source": "[Coding standards.pdf, page 4]"}])
    assert rec["document_citations"][0]["verified"] is True


def test_process_findings_mark_only_citations_their_run_actually_read_as_verified():
    from jde_api_service.process.agent import ProcessAnalysisTools

    tools = ProcessAnalysisTools(company_id="vdb", story_id="S-DOC-PROC",
                                 run={"framework_id": "FW", "framework_version": 1}, story_text={}, knowledge_log=[
                                     {"action": "read", "sections": ["[Control matrix.xlsx, section Credit]"]}])
    out = tools.submit({"missing_controls": ["The credit manager must approve credit limit changes above EUR 50,000."],
                        "summary": "credit control gap",
                        "document_citations": [
                            {"claim": "credit limit changes need approval", "source": "[Control matrix.xlsx, section Credit]"},
                            {"claim": "an invented control", "source": "[Policy.pdf, page 9]"}]})
    assert out["recorded"]
    assert tools.findings["document_citations"] == [
        {"claim": "credit limit changes need approval", "source": "[Control matrix.xlsx, section Credit]", "verified": True},
        {"claim": "an invented control", "source": "[Policy.pdf, page 9]", "verified": False},
    ]
