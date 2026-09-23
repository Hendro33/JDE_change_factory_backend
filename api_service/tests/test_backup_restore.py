"""
A consistent backup and restore across SQLite and the JSON records,
including approval and execution state.

The backup is taken during a brief write pause (the API answers 503 to any
mutating request and the gate refuses to start a JDE attempt), so SQLite and
the JSON files describe the same moment. The restore verifies every
checksum first, never deletes current data (it is moved aside), and reports
whether the server's credential key can read the restored Jira tokens.
After a restart on the restored data, the system behaves exactly as at
backup time: memberships and revisions, approvals with their approver and
expiry, applied / ready / unknown execution states, scope revisions and
valid evidence chains.
"""

from __future__ import annotations

import io
import json
import os
import tarfile

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from .conftest import headers
from .test_concurrency_and_stale_authority import _membership, _set_roles
from .test_execution_attempts import _ready_change, _state
from .test_stage1_execution_safeguards import _approved_story, _execute, _propose


def _build_state(client, monkeypatch) -> dict:
    """Every kind of state a restore must bring back."""
    from jde_mcp_server import ais_client

    from jde_mcp_server.evidence import capture_evidence

    applied = _ready_change(client, "S-BR-APPLIED")
    _execute("S-BR-APPLIED", applied["change_id"])
    capture_evidence("S-BR-APPLIED", {"stage": "Test", "detail": "order type SO confirmed in DEV", "actor": "Hendro"})
    approved = _ready_change(client, "S-BR-APPROVED")  # approved, not yet executed
    unknown = _ready_change(client, "S-BR-UNKNOWN")
    real_submit = ais_client._mock_submit

    def timed_out(*args):
        raise httpx.ReadTimeout("no response")

    monkeypatch.setattr(ais_client, "_mock_submit", timed_out)
    with pytest.raises(httpx.ReadTimeout):
        _execute("S-BR-UNKNOWN", unknown["change_id"])
    monkeypatch.setattr(ais_client, "_mock_submit", real_submit)
    _approved_story("S-BR-PENDING")
    pending = _propose("S-BR-PENDING")
    assert client.put(
        "/admin/jira-credentials", headers=headers("vdb"), json={"email": "bot@example.com", "apiToken": "tok-123"}
    ).status_code == 200
    ellen = _membership("vdb", "u-ellen")
    assert client.put(
        f"/admin/users/{ellen['membership_id']}/roles", headers=headers("vdb"),
        json={"roles": ["product_manager"], "domainIds": [], "expectedRevision": ellen["revision"]},
    ).status_code == 200
    return {"applied": applied, "approved": approved, "unknown": unknown, "pending": pending}


def _backup(tmp_path) -> tuple[str, dict]:
    from jde_api_service.services import backup_restore

    archive = str(tmp_path / "backup" / "jade.tar.gz")
    return archive, backup_restore.create_backup(archive, by="test", settle_seconds=0)


def test_restore_brings_back_sqlite_and_json_state_together(client, monkeypatch, tmp_path):
    from jde_api_service.main import app
    from jde_api_service.services import backup_restore
    from jde_api_service.services.registry import get_jira_credentials_service
    from jde_mcp_server import approval
    from jde_mcp_server.execution import ExecutionBlocked

    ids = _build_state(client, monkeypatch)
    archive, manifest = _backup(tmp_path)
    summary = manifest["summary"]
    assert summary["changes"][ids["approved"]["change_id"]]["status"] == "approved"
    assert summary["changes"][ids["approved"]["change_id"]]["approver_user_id"] == "u-hendro"
    assert summary["changes"][ids["applied"]["change_id"]]["write_state"] == "applied"
    assert summary["changes"][ids["unknown"]["change_id"]]["write_state"] == "unknown"
    assert summary["changes"][ids["pending"]["change_id"]]["status"] == "pending"
    assert summary["evidence_chains"]["S-BR-APPLIED"] == {"valid": True, "entries": 1}
    assert manifest["credential_key_id_at_backup"] in summary["credential_key_ids_needed"]
    assert os.environ["JDE_CREDENTIAL_KEY"] not in json.dumps(manifest)  # the key is never in the archive

    # Life goes on after the backup...
    _execute("S-BR-APPROVED", ids["approved"]["change_id"])
    _set_roles("vdb", "u-ellen", ["dashboard_viewer"])
    approval.reject_change(ids["pending"]["change_id"], "Hendro", "changed my mind", company_id="vdb")

    # ...then the backup is restored over it.
    report = backup_restore.restore_backup(archive, by="test", replace_existing=True)
    assert report["sqlite_integrity"] == "ok"
    assert report["matches_backup"] is True
    assert report["credentials_readable"] is True
    assert set(report["moved_aside"]) == {"api_data", "backlog", "changes", "evidence"}
    assert all(os.path.isdir(p) for p in report["moved_aside"].values())  # nothing deleted

    with TestClient(app):  # restart on the restored data
        pass
    # Approval and execution state are exactly as at backup time.
    assert approval._load(ids["pending"]["change_id"])["status"] == "pending"
    assert _state(ids["approved"]["change_id"]) == "ready"
    assert _state(ids["unknown"]["change_id"]) == "unknown"
    with pytest.raises(ExecutionBlocked):
        _execute("S-BR-APPLIED", ids["applied"]["change_id"])
    with pytest.raises(ExecutionBlocked):
        _execute("S-BR-UNKNOWN", ids["unknown"]["change_id"])
    # The restored approval is usable: its approver is re-checked against the
    # restored membership database.
    assert _execute("S-BR-APPROVED", ids["approved"]["change_id"])["mock"] is True
    # Memberships, with their revisions, are as at backup time.
    assert _membership("vdb", "u-ellen")["roles"] == ["product_manager"]
    assert _membership("vdb", "u-ellen")["revision"] == summary["memberships"][_membership("vdb", "u-ellen")["membership_id"]]["revision"]
    # The existing session still works and the Jira token decrypts.
    assert client.get("/admin/users", headers=headers("vdb")).status_code == 200
    assert get_jira_credentials_service().get_for_customer("vdb").api_token == "tok-123"


def test_a_restore_under_a_different_key_reports_unreadable_credentials(client, monkeypatch, tmp_path):
    from jde_api_service.services import backup_restore, credential_crypto

    _build_state(client, monkeypatch)
    archive, manifest = _backup(tmp_path)
    old_id = manifest["credential_key_id_at_backup"]
    monkeypatch.setenv("JDE_CREDENTIAL_KEY", Fernet.generate_key().decode())
    report = backup_restore.restore_backup(archive, by="test", replace_existing=True)
    assert report["credentials_readable"] is False and report["missing_key_ids"] == [old_id]
    assert report["matches_backup"] is True
    status = client.get("/admin/jira-integration/status", headers=headers("vdb")).json()
    assert status["credentialStorage"] == "unreadable"

    # Recovery: supply the old key (by its id, from the password manager) as the previous key.
    monkeypatch.setenv("JDE_CREDENTIAL_KEY_PREVIOUS", _key_with_id(old_id))
    assert credential_crypto.current_key_id() != old_id
    report = backup_restore.restore_backup(archive, by="test", replace_existing=True)
    assert report["credentials_readable"] is True


_KEYS: dict[str, str] = {}


def _key_with_id(key_id: str) -> str:
    return _KEYS[key_id]


@pytest.fixture(autouse=True)
def _remember_test_keys(isolated_dirs):
    """Stand-in for the password manager: remember each key by its id."""
    from jde_api_service.services import credential_crypto

    key = os.environ["JDE_CREDENTIAL_KEY"]
    _KEYS[credential_crypto._key_id(key.encode())] = key
    yield


def test_writes_are_refused_while_the_backup_is_taken(client, monkeypatch, tmp_path):
    from jde_api_service.services import backup_restore
    from jde_mcp_server import execution

    change = _ready_change(client, "S-BR-PAUSE")
    seen = {}
    real_active = backup_restore._active_work

    def during_backup(locations):
        # A concurrent Admin edit and a JDE dispatch arrive mid-backup.
        seen["put"] = client.put("/admin/engagement-scope", headers=headers("vdb"), json={}).status_code
        seen["get"] = client.get("/admin/users", headers=headers("vdb")).status_code
        try:
            execution.begin(change["change_id"], execution.WRITE)
        except execution.ExecutionBlocked as exc:
            seen["gate"] = str(exc)
        return real_active(locations)

    monkeypatch.setattr(backup_restore, "_active_work", during_backup)
    _backup(tmp_path)
    assert seen["put"] == 503 and seen["get"] == 200
    assert "paused" in seen["gate"]
    # The pause is lifted afterwards.
    assert _execute("S-BR-PAUSE", change["change_id"])["mock"] is True


def test_no_backup_while_a_jde_attempt_is_in_flight(client, tmp_path):
    from jde_api_service.services import backup_restore
    from jde_mcp_server import execution

    change = _ready_change(client, "S-BR-INFLIGHT")
    execution.begin(change["change_id"], execution.WRITE)
    with pytest.raises(backup_restore.BackupRefused, match="JDE write attempt"):
        _backup(tmp_path)
    from jde_api_service.services import write_pause

    assert write_pause.status() is None  # the pause never outlives a refused backup


def test_a_damaged_or_altered_archive_is_refused_before_anything_changes(client, monkeypatch, tmp_path):
    from jde_api_service.services import backup_restore

    ids = _build_state(client, monkeypatch)
    archive, _manifest = _backup(tmp_path)
    altered = str(tmp_path / "altered.tar.gz")
    with tarfile.open(archive, "r:gz") as src, tarfile.open(altered, "w:gz") as dst:
        for member in src.getmembers():
            data = src.extractfile(member).read() if member.isfile() else None
            if member.name == f"changes/{ids['pending']['change_id']}.json":
                data = data.replace(b'"pending"', b'"approved"')  # someone "approves" inside the backup
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data) if data is not None else None)
    with pytest.raises(backup_restore.RestoreRefused, match="checksum mismatch"):
        backup_restore.restore_backup(altered, by="test", replace_existing=True)
    from jde_mcp_server import approval

    assert approval._load(ids["pending"]["change_id"])["status"] == "pending"


def test_a_restore_never_overwrites_existing_data_unless_asked(client, monkeypatch, tmp_path):
    from jde_api_service.services import backup_restore

    _build_state(client, monkeypatch)
    archive, _manifest = _backup(tmp_path)
    with pytest.raises(backup_restore.RestoreRefused, match="already hold data"):
        backup_restore.restore_backup(archive, by="test")
