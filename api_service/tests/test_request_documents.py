"""
Documents on new requests: upload, validation, extraction states,
authorization, cleanup, sharing policy, and their use (with citations) in
refinement -- with a MOCKED agent runtime (no model is called).
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone

import claude_agent_sdk as sdk
import pytest

from . import _docs
from .conftest import headers


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _upload(client, name, data, customer="vdb"):
    return client.post("/change-requests/attachments", headers=headers(customer),
                       json={"filename": name, "contentBase64": _b64(data)})


def _wait_ready(client, attachment_id, customer="vdb"):
    for _ in range(100):
        a = client.get(f"/change-requests/attachments/{attachment_id}", headers=headers(customer)).json()
        if a.get("extractionStatus") in ("ready", "failed"):
            return a
        time.sleep(0.05)
    raise AssertionError("extraction did not finish")


def _create(client, attachment_ids, customer="vdb"):
    return client.post("/change-requests", headers=headers(customer), json={
        "title": "Delivery date rule", "businessSource": "Business", "rawContent": "See the attached spec.",
        "attachmentIds": attachment_ids})


SPEC_PDF = _docs.pdf(["Synthetic spec page one: orders ship within 2 working days.",
                      "Synthetic spec page two: public holidays are excluded."])


def test_pdf_docx_and_txt_are_read_with_citable_sections(client):
    ids = []
    for name, data in (("spec.pdf", SPEC_PDF),
                       ("notes.docx", _docs.docx([("h", "Delivery dates"), ("p", "Two working days."),
                                                  ("h", "Exceptions"), ("p", "Holidays excluded.")])),
                       ("mail.txt", b"Synthetic mail: please fix the date.\n")):
        r = _upload(client, name, data)
        assert r.status_code == 201, r.text
        assert r.json()["requestId"] is None and r.json()["status"] == "pending"
        ids.append(r.json()["id"])
    ready = [_wait_ready(client, i) for i in ids]
    assert [a["extractionStatus"] for a in ready] == ["ready"] * 3
    r = _create(client, ids)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    listed = client.get(f"/change-requests/{cid}/attachments", headers=headers("vdb")).json()["attachments"]
    assert [a["filename"] for a in listed] == ["spec.pdf", "notes.docx", "mail.txt"]
    assert all(a["uploadedBy"] == "Hendro" and len(a["sha256"]) == 64 and a["revision"] == 1 for a in listed)
    from jde_api_service.knowledge import attachments

    pdf_sections = attachments.extracted("vdb", ids[0])["sections"]
    assert [s["label"] for s in pdf_sections] == ["page 1", "page 2"] and "holidays" in pdf_sections[1]["text"]
    assert [s["label"] for s in attachments.extracted("vdb", ids[1])["sections"]] == [
        "section: Delivery dates", "section: Exceptions"]
    dl = client.get(f"/change-requests/{cid}/attachments/{ids[0]}/download", headers=headers("vdb"))
    assert dl.status_code == 200 and dl.content == SPEC_PDF and "attachment" in dl.headers["content-disposition"]


@pytest.mark.parametrize("name,data,reason", [
    ("evil.pdf", b"MZ\x90\x00 not a pdf", "not a valid PDF"),
    ("tool.exe", b"MZ\x90\x00", "only PDF, DOCX and TXT"),
    ("macro.docx", _docs.docx([("p", "x")], macro=True), "macros"),
    ("fake.txt", b"\x00\x01\x02binary", "not a valid TXT"),
    ("../../etc/passwd.txt", b"ok", None),  # path components are stripped, not followed
])
def test_content_type_and_names_are_validated(client, name, data, reason):
    r = _upload(client, name, data)
    if reason is None:
        assert r.status_code == 201 and r.json()["filename"] == "passwd.txt"
    else:
        assert r.status_code == 422 and reason in r.json()["detail"]


def test_size_and_count_limits(client, monkeypatch):
    from jde_api_service.knowledge import attachments

    monkeypatch.setattr(attachments, "MAX_FILE_BYTES", 100)
    assert "larger than" in _upload(client, "big.txt", b"x" * 101).json()["detail"]
    monkeypatch.setattr(attachments, "MAX_FILE_BYTES", 10 * 1024 * 1024)
    ids = [_upload(client, f"n{i}.txt", b"synthetic").json()["id"] for i in range(6)]
    r = _create(client, ids)
    assert r.status_code == 422 and "at most 5" in r.json()["detail"]


def test_encrypted_and_scanned_pdfs_fail_honestly_and_the_requirement_is_kept(client):
    enc = _upload(client, "locked.pdf", _docs.encrypted_pdf()).json()
    scan = _upload(client, "scan.pdf", _docs.blank_pdf()).json()
    a, b = _wait_ready(client, enc["id"]), _wait_ready(client, scan["id"])
    assert a["extractionStatus"] == "failed" and "password-protected" in a["extractionDetail"]
    assert b["extractionStatus"] == "failed" and "OCR" in b["extractionDetail"]
    r = _create(client, [enc["id"], scan["id"]])
    assert r.status_code == 201  # the requirement is kept
    cid = r.json()["id"]
    assert client.get(f"/changes/{cid}", headers=headers("vdb")).status_code == 200
    # Retry re-reads the unchanged original; removal deletes the file but keeps the record.
    r = client.post(f"/change-requests/{cid}/attachments/{scan['id']}/retry", headers=headers("vdb"))
    assert r.status_code == 200
    assert _wait_ready(client, scan["id"])["extractionVersion"] == 2
    r = client.delete(f"/change-requests/{cid}/attachments/{enc['id']}", headers=headers("vdb"))
    gone = next(x for x in r.json()["attachments"] if x["id"] == enc["id"])
    assert gone["deletedBy"] == "Hendro" and gone["extractionStatus"] == "deleted"
    assert client.get(f"/change-requests/{cid}/attachments/{enc['id']}/download",
                      headers=headers("vdb")).status_code == 404


def test_other_customers_and_other_users_cannot_reach_documents(client, ellen_client):
    up = _upload(client, "spec.pdf", SPEC_PDF, customer="bwm").json()
    cid = _create(client, [up["id"]], customer="bwm").json()["id"]
    # Ellen (vdb only) cannot address bwm at all; with her own customer the request does not exist.
    assert ellen_client.get(f"/change-requests/{cid}/attachments", headers=headers("bwm")).status_code == 403
    assert ellen_client.get(f"/change-requests/{cid}/attachments", headers=headers("vdb")).status_code == 404
    assert ellen_client.get(f"/change-requests/{cid}/attachments/{up['id']}/download",
                            headers=headers("vdb")).status_code == 404
    # A pending upload of one user cannot be attached by another user.
    mine = _upload(client, "mine.txt", b"synthetic", customer="vdb").json()
    r = ellen_client.post("/change-requests", headers=headers("vdb"), json={
        "title": "t", "businessSource": "Business", "rawContent": "x", "attachmentIds": [mine["id"]]})
    assert r.status_code == 422


def test_abandoned_uploads_are_deleted(client):
    from jde_api_service.knowledge import attachments
    from jde_api_service.persistence.db import connection as db

    up = _upload(client, "old.txt", b"synthetic").json()
    with db(immediate=True) as conn:
        conn.execute("UPDATE request_attachments SET uploaded_at = ? WHERE attachment_id = ?",
                     ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(), up["id"]))
    assert attachments.cleanup_abandoned() == 1
    assert client.get(f"/change-requests/attachments/{up['id']}", headers=headers("vdb")).status_code == 404


def test_uploading_never_calls_a_model(client, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("a model was called")
        yield  # pragma: no cover

    monkeypatch.setattr(sdk, "query", boom)
    up = _upload(client, "spec.pdf", SPEC_PDF).json()
    assert _wait_ready(client, up["id"])["extractionStatus"] == "ready"
    assert _create(client, [up["id"]]).status_code == 201


# -- Use in refinement --------------------------------------------------------------------
def _summary(citations):
    return {"story_id": "x", "user_story": {
        "statement": "As a planner I want delivery dates in working days so that promises are kept",
        "business_context": "c", "acceptance_criteria": [{"id": "AC1", "text": "t", "verified_by": "T1"}],
        "test_script": [{"id": "T1", "action": "a", "expected": "e"}], "business_rules": [], "assumptions": [],
        "open_questions": [], "quality_status": "needs_human_input", "revision_count": 0,
        "document_citations": citations},
        "business_impact": {}, "rough_complexity_signal": "Low", "check_outcome": "needs_human_input"}


def _run_refinement(client, monkeypatch, policy):
    from jde_api_service.ai import connection
    from jde_api_service.knowledge import tools as ktools

    from . import _ai

    _ai.configure("vdb", policy=policy)
    if policy != "permitted_content":
        pass
    up = _upload(client, "spec.pdf", SPEC_PDF).json()
    _wait_ready(client, up["id"])
    cid = _create(client, [up["id"]]).json()["id"]
    captured = {}
    instances = []
    real_init = ktools.KnowledgeTools.__init__

    def init(self, **kw):
        real_init(self, **kw)
        instances.append(self)

    monkeypatch.setattr(ktools.KnowledgeTools, "__init__", init)

    async def fake_query(*, prompt, options):
        captured["agents"] = {k: v.tools for k, v in (options.agents or {}).items()}
        captured["servers"] = list(options.mcp_servers)
        yield sdk.SystemMessage(subtype="init", data={"model": "claude-sonnet-5", "apiKeySource": "ANTHROPIC_API_KEY"})
        kt = instances[-1]
        listing = kt.list_documents()
        captured["listing"] = listing
        read = kt.read_document(listing["documents"][0]["document_id"])
        captured["read"] = read
        cites = [{"claim": "Holidays are excluded", "source": "[spec.pdf, page 2]"},
                 {"claim": "Invented", "source": "[spec.pdf, page 9]"}]
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                                session_id="s", total_cost_usd=0.01, result="```json\n" + json.dumps(_summary(cites)) + "\n```")

    monkeypatch.setattr(sdk, "query", fake_query)
    r = client.post(f"/changes/{cid}/enhance", headers=headers("vdb"))
    assert r.status_code == 202, r.text
    change = client.get(f"/changes/{cid}", headers=headers("vdb")).json()
    return change, captured, connection


def test_refinement_reads_permitted_documents_and_keeps_only_verifiable_citations(client, monkeypatch):
    change, captured, _ = _run_refinement(client, monkeypatch, "permitted_content")
    assert "jade-knowledge" in captured["servers"]
    assert "mcp__jade-knowledge__read_document" in captured["agents"]["improve-agent"]
    doc = captured["listing"]["documents"][0]
    assert doc["filename"] == "spec.pdf" and doc["readable"] is True
    assert captured["read"]["untrusted_evidence"] is True
    assert [s["cite"] for s in captured["read"]["sections"]] == ["[spec.pdf, page 1]", "[spec.pdf, page 2]"]
    cites = change["userStory"]["documentCitations"]
    assert {(c["source"], c["verified"]) for c in cites} == {("[spec.pdf, page 2]", True), ("[spec.pdf, page 9]", False)}
    from jde_api_service.ai import runtime

    rec = runtime.list_runs("vdb")[0]
    assert rec["status"] == "completed"
    assert any(k["action"] == "read" and k["filename"] == "spec.pdf" and len(k["sha256"]) == 64
               for k in rec["knowledge"])


def test_metadata_only_policy_keeps_document_bodies_away_from_the_model(client, monkeypatch):
    change, captured, _ = _run_refinement(client, monkeypatch, "metadata_only")
    assert "mcp__jade-knowledge__read_document" not in captured["agents"]["improve-agent"]
    doc = captured["listing"]["documents"][0]
    assert doc["readable"] is False and "metadata only" in doc["why_not"]
    assert "error" in captured["read"] and "sections" not in captured["read"]
    # Nothing was read, so no citation can be verified.
    assert all(c["verified"] is False for c in change["userStory"]["documentCitations"])


def test_knowledge_tools_never_show_another_customers_documents(client):
    from jde_api_service.knowledge.tools import KnowledgeTools

    other = _upload(client, "other.pdf", SPEC_PDF, customer="bwm").json()
    ocid = _create(client, [other["id"]], customer="bwm").json()["id"]
    kt = KnowledgeTools(company_id="vdb", story_id=ocid, refs=["request_documents"], documents_allowed=True, log=[])
    assert kt.list_documents()["documents"] == []
    assert "not available" in kt.read_document(other["id"])["error"]
