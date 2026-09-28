"""
Consistent backup and restore of a single-server Jade installation that
uses the built-in SQLite database: the database (every record -- users,
customers, settings, stories, exact-change approvals with their
execution attempts, evidence chains) and the uploaded files under the
data directory. A deployment on PostgreSQL uses the database service's
own backups (on Azure: automated backups with point-in-time restore)
plus the file store's.

Consistency comes from a brief write pause (write_pause.py): while the
archive is taken, the API refuses every mutating request and the gate
refuses to start a JDE attempt, so SQLite and the JSON files describe the
same moment. SQLite is copied with its own online-backup API (never a raw
file copy). A backup is refused while an agent run or a JDE attempt is in
progress, because those write outside a request.

Every archive carries manifest.json: a sha256 per file, row counts, a
summary of the security-relevant state (memberships and their revisions,
exact-change approvals and execution states, scope revisions, evidence
chain validity) and the IDs -- never the keys -- of the credential
encryption keys the stored Jira tokens need. restore_backup() verifies
every checksum before touching anything, and reports whether the key the
server has now can read the restored tokens.

Used by scripts/jade_backup.py; tested end to end in
tests/test_backup_restore.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from typing import Optional

from jde_mcp_server import approval, execution

from ..config import settings
from ..persistence.db import db_path
from . import credential_crypto, write_pause

FORMAT = 1
DB_NAME = "jde.sqlite3"
_SKIP_SUFFIXES = (".lock", "-wal", "-shm", "-journal")


class BackupRefused(RuntimeError):
    pass


class RestoreRefused(RuntimeError):
    pass


def data_locations() -> dict[str, str]:
    """Every directory Jade writes, by a stable name used inside the archive.
    All records are in the database under the data directory; the data
    directory also holds uploaded files (artifacts, request documents)."""
    return {"api_data": os.path.abspath(settings.data_dir)}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _skip(name: str) -> bool:
    return name == write_pause.FILE_NAME or name.startswith(".") or name.endswith(_SKIP_SUFFIXES)


# ---------------------------------------------------------------------
# State summary: what a restore must bring back exactly.
# ---------------------------------------------------------------------
def summarise(locations: dict[str, str], database: str) -> dict:
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}  # noqa: S608 -- names from sqlite_master
        memberships = {
            r["id"]: {"status": r["status"], "revision": r["revision"],
                      "roles": sorted(x["role"] for x in conn.execute(
                          "SELECT role FROM membership_roles WHERE membership_id = ?", (r["id"],)))}
            for r in conn.execute("SELECT id, status, revision FROM company_memberships ORDER BY id")
        }
        schema_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        stored_key_ids = sorted({
            credential_crypto.stored_key_id(r["api_token"]) or "plaintext"
            for r in conn.execute("SELECT api_token FROM jira_credentials")
        })
    finally:
        conn.close()

    changes, scopes, evidence = {}, {}, {}
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        has_documents = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'").fetchone() is not None
        docs = (lambda kind: [(r["doc_id"], json.loads(r["body"])) for r in conn.execute(
            "SELECT doc_id, body FROM documents WHERE kind = ? ORDER BY doc_id", (kind,))]) if has_documents else (lambda kind: [])
        for _id, rec in docs(approval.KIND):
            changes[rec["change_id"]] = {
                "status": rec.get("status"),
                "approver_user_id": (rec.get("approver_authority") or {}).get("user_id"),
                "expires_at": rec.get("expires_at"),
                "write_state": execution.effective_state(rec, execution.WRITE),
                "test_state": execution.effective_state(rec, execution.TEST),
                "write_attempts": len(((rec.get("execution") or {}).get("write") or {}).get("attempts") or []),
            }
        for doc_id, scope in docs("engagement_scope"):
            scopes[doc_id] = scope.get("revision")
        for doc_id, entries in docs("evidence"):
            evidence[doc_id] = {"valid": _chain_valid(entries), "entries": len(entries)}
        for table, column in (("ai_connections", "credential_secret"), ("jde_profiles", "credential_secret")):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
                stored_key_ids = sorted(set(stored_key_ids) | {
                    credential_crypto.stored_key_id(r[column]) or "plaintext"
                    for r in conn.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")  # noqa: S608
                })
    finally:
        conn.close()
    return {
        "schema_version": schema_version,
        "row_counts": counts,
        "memberships": memberships,
        "credential_key_ids_needed": stored_key_ids,
        "changes": changes,
        "scope_revisions": scopes,
        "evidence_chains": evidence,
    }


def _chain_valid(entries: list[dict]) -> bool:
    from jde_mcp_server.evidence import GENESIS_HASH, _entry_hash

    prev = GENESIS_HASH
    for entry in entries:
        body = {k: v for k, v in entry.items() if k != "entry_hash"}
        if entry.get("prev_hash") != prev or entry.get("entry_hash") != _entry_hash(prev, body):
            return False
        prev = entry["entry_hash"]
    return True


def _active_work(locations: dict[str, str]) -> list[str]:
    from .run_recovery import _ENHANCEMENT_IN_PROGRESS
    from .registry import get_agent_run_service, get_architecture_review_service, get_enhancement_run_service

    active = [f"agent run {r.run_id}" for r in get_agent_run_service().list_all() if r.stage == "started"]
    active += [f"enhancement {r.request_id}" for r in get_enhancement_run_service().list_all() if r.stage in _ENHANCEMENT_IN_PROGRESS]
    active += [f"architecture review {r.story_id}" for r in get_architecture_review_service().list_all() if r.stage == "analyzing"]
    for rec in approval.all_records():
        for kind in (execution.WRITE, execution.TEST):
            if ((rec.get("execution") or {}).get(kind) or {}).get("state") == "in_progress":
                active.append(f"JDE {kind} attempt on {rec['change_id']}")
    return active


# ---------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------
def create_backup(out_path: str, *, by: str, settle_seconds: float = 2.0) -> dict:
    """Write a .tar.gz archive of every data location plus manifest.json,
    taken during a write pause. Returns the manifest."""
    locations = data_locations()
    with write_pause.paused("backup", by, settle_seconds=settle_seconds):
        active = _active_work(locations)
        if active:
            raise BackupRefused(
                "work is in progress that writes outside a request, so a consistent backup cannot be taken now: "
                + ", ".join(sorted(active))
            )
        with tempfile.TemporaryDirectory(prefix="jade-backup-") as staging:
            files: dict[str, dict] = {}
            for name, src in locations.items():
                dest_root = os.path.join(staging, name)
                os.makedirs(dest_root, exist_ok=True)
                if not os.path.isdir(src):
                    continue
                for root, dirs, fns in os.walk(src):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for fn in fns:
                        if _skip(fn) or (name == "api_data" and root == src and fn == DB_NAME):
                            continue
                        rel = os.path.relpath(os.path.join(root, fn), src)
                        dest = os.path.join(dest_root, rel)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.copy2(os.path.join(root, fn), dest)
            # SQLite's own online backup: a consistent copy even of a live file.
            source_db = db_path()
            staged_db = os.path.join(staging, "api_data", DB_NAME)
            src_conn, dst_conn = sqlite3.connect(source_db), sqlite3.connect(staged_db)
            try:
                src_conn.backup(dst_conn)
                # A self-contained single file in the archive (the live
                # database runs in WAL mode; the copy must not).
                dst_conn.execute("PRAGMA journal_mode = DELETE")
            finally:
                src_conn.close()
                dst_conn.close()
            for root, _dirs, fns in os.walk(staging):
                for fn in fns:
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, staging)
                    files[rel] = {"sha256": _sha256(full), "bytes": os.path.getsize(full)}
            staged_locations = {name: os.path.join(staging, name) for name in locations}
            manifest = {
                "format": FORMAT,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": by,
                "locations": sorted(locations),
                "files": files,
                "summary": summarise(staged_locations, staged_db),
                "credential_key_id_at_backup": credential_crypto.current_key_id(),
                "note": "Stored Jira tokens are ciphertext. Restoring them needs the JDE_CREDENTIAL_KEY whose key id "
                        "is listed in summary.credential_key_ids_needed; the key itself is never in this archive.",
            }
            with open(os.path.join(staging, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, sort_keys=True)
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            with tarfile.open(out_path, "w:gz") as tar:
                for entry in sorted(os.listdir(staging)):
                    tar.add(os.path.join(staging, entry), arcname=entry)
    return manifest


# ---------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------
def _extract_verified(archive: str, into: str) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.issym() or member.islnk() or member.name.startswith("/") or ".." in member.name.split("/"):
                raise RestoreRefused(f"unsafe path in archive: {member.name}")
        tar.extractall(into)  # noqa: S202 -- every member checked above
    with open(os.path.join(into, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("format") != FORMAT:
        raise RestoreRefused(f"unknown backup format {manifest.get('format')!r}")
    seen = set()
    for root, _dirs, fns in os.walk(into):
        for fn in fns:
            rel = os.path.relpath(os.path.join(root, fn), into)
            if rel != "manifest.json":
                seen.add(rel)
    if seen != set(manifest["files"]):
        raise RestoreRefused("the archive's files do not match its manifest (missing or extra files): "
                             + ", ".join(sorted(seen ^ set(manifest["files"]))[:5]))
    for rel, meta in manifest["files"].items():
        if _sha256(os.path.join(into, rel)) != meta["sha256"]:
            raise RestoreRefused(f"checksum mismatch for {rel} -- the archive is damaged or was altered")
    return manifest


def _key_report(manifest: dict) -> dict:
    needed = [k for k in manifest["summary"]["credential_key_ids_needed"] if k != "plaintext"]
    available = {k for k in (
        credential_crypto.current_key_id(),
        credential_crypto._key_id(os.environ["JDE_CREDENTIAL_KEY_PREVIOUS"].strip().encode())
        if os.environ.get("JDE_CREDENTIAL_KEY_PREVIOUS", "").strip() else None,
    ) if k}
    missing = sorted(set(needed) - available)
    return {
        "credential_key_ids_needed": needed,
        "credential_key_ids_available": sorted(available),
        "credentials_readable": not missing,
        "missing_key_ids": missing,
    }


def restore_backup(archive: str, *, by: str, replace_existing: bool = False) -> dict:
    """Verify the archive, then restore it into the configured locations
    during a write pause. Existing data is never deleted: with
    replace_existing it is moved aside to <dir>.pre-restore-<timestamp>.
    Restart the service afterwards so nothing keeps pre-restore state in
    memory. Returns a report including whether the current credential key
    can read the restored Jira tokens."""
    locations = data_locations()
    with tempfile.TemporaryDirectory(prefix="jade-restore-") as unpacked:
        manifest = _extract_verified(archive, unpacked)
        occupied = [n for n, p in locations.items() if os.path.isdir(p) and any(not _skip(x) for x in os.listdir(p))]
        if occupied and not replace_existing:
            raise RestoreRefused(
                f"these locations already hold data: {', '.join(occupied)}. Restore into empty locations, or pass "
                "replace_existing to move the current data aside first."
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        moved_aside = {}
        with write_pause.paused("restore", by):
            for name, target in locations.items():
                if name in occupied:
                    aside = f"{target}.pre-restore-{stamp}"
                    os.rename(target, aside)
                    moved_aside[name] = aside
                    # Keep the pause flag where the running service looks for it.
                    pause_src = os.path.join(aside, write_pause.FILE_NAME)
                    os.makedirs(target, exist_ok=True)
                    if os.path.exists(pause_src):
                        shutil.copy2(pause_src, os.path.join(target, write_pause.FILE_NAME))
                os.makedirs(target, exist_ok=True)
                src = os.path.join(unpacked, name)
                if os.path.isdir(src):
                    shutil.copytree(src, target, dirs_exist_ok=True)
        restored_db = os.path.join(locations["api_data"], DB_NAME)
        conn = sqlite3.connect(restored_db)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        summary_now = summarise(locations, restored_db)
    return {
        "restored_from": os.path.abspath(archive),
        "backup_created_at": manifest["created_at"],
        "sqlite_integrity": integrity,
        "matches_backup": summary_now == manifest["summary"],
        "moved_aside": moved_aside,
        **_key_report(manifest),
    }


def verify_archive(archive: str) -> dict:
    """Check every checksum without restoring anything. Returns the manifest
    plus the credential-key report for the current environment."""
    with tempfile.TemporaryDirectory(prefix="jade-verify-") as unpacked:
        manifest = _extract_verified(archive, unpacked)
    return {"manifest": manifest, **_key_report(manifest)}
