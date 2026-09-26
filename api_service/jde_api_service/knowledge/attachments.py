"""
Documents attached to a new request (demand).

Lifecycle
  * upload   -> a PENDING attachment owned by the uploader, not yet part of
               any request. Extraction runs in the background:
               pending -> extracting -> ready | failed.
  * create request with attachment_ids -> the pending attachments are
               linked to that request (same customer, same uploader only).
  * abandoned -> pending uploads never linked are deleted (file and row)
               after ABANDONED_AFTER_HOURS; the uploader can also remove a
               pending upload before submitting.
  * removed from a request -> the original and its extracted text are
               deleted from storage; the metadata row stays (who removed
               what, when) for the audit trail.
  * retry    -> extraction runs again on the same, unchanged original
               (extraction_version + 1). The original is never modified:
               its SHA-256 is recorded at upload and checked on every read.

Storage: <data_dir>/request_attachments/<company>/<attachment>/original and
extracted.json -- private backend storage, reachable only through the
authenticated API. Uploading never calls a model; whether an agent may read
the text is decided at run time by the customer's document policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..config import settings
from ..persistence.db import connection
from . import extract

MAX_FILES_PER_REQUEST = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
ABANDONED_AFTER_HOURS = 24
LIMITS = {"maxFiles": MAX_FILES_PER_REQUEST, "maxFileBytes": MAX_FILE_BYTES, "maxTotalBytes": MAX_TOTAL_BYTES,
          "types": ["PDF", "DOCX", "TXT"], "abandonedAfterHours": ABANDONED_AFTER_HOURS}


class AttachmentRejected(ValueError):
    pass


class NotFound(LookupError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root() -> str:
    return os.path.join(settings.data_dir, "request_attachments")


def _dir(company_id: str, attachment_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", company_id) or not re.fullmatch(r"ATT-[0-9a-f]{12}", attachment_id):
        raise AttachmentRejected("invalid identifier")
    return os.path.join(_root(), company_id, attachment_id)


def _write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _view(row) -> dict:
    return {"id": row["attachment_id"], "requestId": row["request_id"], "status": row["status"],
            "filename": row["filename"], "fileType": row["file_type"], "sizeBytes": row["size_bytes"],
            "sha256": row["sha256"], "revision": row["revision"], "uploadedBy": row["uploaded_by_name"],
            "uploadedAt": row["uploaded_at"], "extractionStatus": row["extraction_status"],
            "extractionDetail": row["extraction_detail"], "extractionVersion": row["extraction_version"],
            "sections": row["sections"], "deletedAt": row["deleted_at"], "deletedBy": row["deleted_by"]}


def upload(company_id: str, filename: str, data: bytes, *, user_id: str, user_name: str) -> dict:
    cleanup_abandoned()
    name = extract.clean_filename(filename)
    if len(data) > MAX_FILE_BYTES:
        raise AttachmentRejected(f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    try:
        file_type = extract.detect(name, data)
    except extract.Rejected as exc:
        raise AttachmentRejected(f"{name}: {exc}") from None
    with connection() as conn:
        pending = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS b FROM request_attachments "
                               "WHERE company_id = ? AND uploaded_by = ? AND status = 'pending'",
                               (company_id, user_id)).fetchone()
    if pending["n"] >= MAX_FILES_PER_REQUEST * 2:
        raise AttachmentRejected("too many unsubmitted uploads; submit or remove them first")
    attachment_id = f"ATT-{uuid.uuid4().hex[:12]}"
    d = _dir(company_id, attachment_id)
    os.makedirs(d, exist_ok=True)
    _write(os.path.join(d, "original"), data)
    with connection(immediate=True) as conn:
        conn.execute(
            "INSERT INTO request_attachments (attachment_id, company_id, request_id, status, filename, file_type, "
            "size_bytes, sha256, revision, storage_path, uploaded_by, uploaded_by_name, uploaded_at, extraction_status, "
            "extraction_detail) VALUES (?, ?, NULL, 'pending', ?, ?, ?, ?, 1, ?, ?, ?, ?, 'pending', "
            "'waiting to be read')",
            (attachment_id, company_id, name, file_type, len(data), hashlib.sha256(data).hexdigest(),
             os.path.relpath(d, _root()), user_id, user_name, _now()))
    return get(company_id, attachment_id)


def run_extraction(company_id: str, attachment_id: str) -> dict:
    """Reads the stored original (checksum-verified) and records the result.
    Called in the background after upload and on retry."""
    with connection(immediate=True) as conn:
        row = conn.execute("SELECT * FROM request_attachments WHERE attachment_id = ? AND company_id = ?",
                           (attachment_id, company_id)).fetchone()
        if row is None or row["deleted_at"]:
            return {}
        conn.execute("UPDATE request_attachments SET extraction_status = 'extracting', extraction_detail = "
                     "'reading the document' WHERE attachment_id = ?", (attachment_id,))
    d = _dir(company_id, attachment_id)
    try:
        data = _read_original(row)
        result = extract.run_isolated(row["file_type"], data)
    except (OSError, AttachmentRejected) as exc:
        result = {"status": "failed", "reason": "storage", "detail": f"The stored file could not be read: {exc}"}
    status, detail, n = extract.summary(result)
    if status == "ready":
        _write(os.path.join(d, "extracted.json"), json.dumps(
            {"sha256": row["sha256"], "extractor_version": result.get("extractor_version"),
             "truncated": result.get("truncated", False), "sections": result["sections"]}).encode("utf-8"))
    else:
        try:
            os.remove(os.path.join(d, "extracted.json"))
        except FileNotFoundError:
            pass
    with connection(immediate=True) as conn:
        conn.execute("UPDATE request_attachments SET extraction_status = ?, extraction_detail = ?, sections = ?, "
                     "extraction_version = extraction_version + 1 WHERE attachment_id = ? AND deleted_at IS NULL",
                     (status, detail[:500], n, attachment_id))
    return get(company_id, attachment_id)


def _read_original(row) -> bytes:
    path = os.path.join(_root(), row["storage_path"], "original")
    with open(path, "rb") as f:
        data = f.read()
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise AttachmentRejected("the stored file does not match its recorded checksum")
    return data


def get(company_id: str, attachment_id: str) -> dict:
    with connection() as conn:
        row = conn.execute("SELECT * FROM request_attachments WHERE attachment_id = ? AND company_id = ?",
                           (attachment_id, company_id)).fetchone()
    if row is None:
        raise NotFound("no such attachment")
    return _view(row)


def validate_for_request(company_id: str, attachment_ids: list[str], *, user_id: str) -> list[dict]:
    ids = list(dict.fromkeys(attachment_ids or []))
    if len(ids) > MAX_FILES_PER_REQUEST:
        raise AttachmentRejected(f"at most {MAX_FILES_PER_REQUEST} documents per request")
    out = []
    with connection() as conn:
        for aid in ids:
            row = conn.execute("SELECT * FROM request_attachments WHERE attachment_id = ? AND company_id = ?",
                               (aid, company_id)).fetchone()
            if row is None or row["status"] != "pending" or row["uploaded_by"] != user_id:
                raise AttachmentRejected("an attached document is no longer available; upload it again")
            out.append(_view(row))
    if sum(a["sizeBytes"] for a in out) > MAX_TOTAL_BYTES:
        raise AttachmentRejected(f"the documents together are larger than {MAX_TOTAL_BYTES // (1024 * 1024)} MB")
    return out


def link(company_id: str, request_id: str, attachment_ids: list[str], *, user_id: str) -> list[dict]:
    with connection(immediate=True) as conn:
        for aid in dict.fromkeys(attachment_ids or []):
            conn.execute("UPDATE request_attachments SET request_id = ?, status = 'attached' WHERE attachment_id = ? "
                         "AND company_id = ? AND status = 'pending' AND uploaded_by = ?",
                         (request_id, aid, company_id, user_id))
    return list_for_request(company_id, request_id)


def list_for_request(company_id: str, request_id: str, *, include_deleted: bool = True) -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM request_attachments WHERE company_id = ? AND request_id = ? "
                            "ORDER BY uploaded_at", (company_id, request_id)).fetchall()
    out = [_view(r) for r in rows]
    return out if include_deleted else [a for a in out if not a["deletedAt"]]


def download(company_id: str, attachment_id: str, *, request_id: Optional[str] = None) -> tuple[dict, bytes]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM request_attachments WHERE attachment_id = ? AND company_id = ?",
                           (attachment_id, company_id)).fetchone()
    if row is None or row["deleted_at"] or (request_id is not None and row["request_id"] != request_id):
        raise NotFound("no such attachment")
    return _view(row), _read_original(row)


def extracted(company_id: str, attachment_id: str) -> Optional[dict]:
    """Sections of a READY attachment whose text still matches its original."""
    with connection() as conn:
        row = conn.execute("SELECT * FROM request_attachments WHERE attachment_id = ? AND company_id = ?",
                           (attachment_id, company_id)).fetchone()
    if row is None or row["deleted_at"] or row["extraction_status"] != "ready":
        return None
    try:
        with open(os.path.join(_root(), row["storage_path"], "extracted.json"), "rb") as f:
            data = json.loads(f.read())
    except (OSError, json.JSONDecodeError):
        return None
    return data if data.get("sha256") == row["sha256"] else None


def remove_pending(company_id: str, attachment_id: str, *, user_id: str) -> None:
    with connection(immediate=True) as conn:
        row = conn.execute("SELECT * FROM request_attachments WHERE attachment_id = ? AND company_id = ? AND "
                           "status = 'pending' AND uploaded_by = ?", (attachment_id, company_id, user_id)).fetchone()
        if row is None:
            raise NotFound("no such pending upload")
        conn.execute("DELETE FROM request_attachments WHERE attachment_id = ?", (attachment_id,))
    shutil.rmtree(_dir(company_id, attachment_id), ignore_errors=True)


def remove_from_request(company_id: str, request_id: str, attachment_id: str, *, actor: str) -> list[dict]:
    with connection(immediate=True) as conn:
        row = conn.execute("SELECT * FROM request_attachments WHERE attachment_id = ? AND company_id = ? AND "
                           "request_id = ? AND deleted_at IS NULL", (attachment_id, company_id, request_id)).fetchone()
        if row is None:
            raise NotFound("no such attachment")
        conn.execute("UPDATE request_attachments SET status = 'deleted', deleted_at = ?, deleted_by = ?, "
                     "extraction_status = 'deleted', extraction_detail = 'removed from the request; the file was "
                     "deleted' WHERE attachment_id = ?", (_now(), actor, attachment_id))
    shutil.rmtree(_dir(company_id, attachment_id), ignore_errors=True)
    return list_for_request(company_id, request_id)


def cleanup_abandoned(now: Optional[datetime] = None) -> int:
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(hours=ABANDONED_AFTER_HOURS)).isoformat()
    with connection(immediate=True) as conn:
        rows = conn.execute("SELECT attachment_id, company_id FROM request_attachments WHERE status = 'pending' AND "
                            "uploaded_at < ?", (cutoff,)).fetchall()
        conn.execute("DELETE FROM request_attachments WHERE status = 'pending' AND uploaded_at < ?", (cutoff,))
    for r in rows:
        shutil.rmtree(_dir(r["company_id"], r["attachment_id"]), ignore_errors=True)
    return len(rows)


def reset_stuck_extractions() -> int:
    """At start-up: an extraction cut off by a restart is marked failed (retry is offered)."""
    with connection(immediate=True) as conn:
        cur = conn.execute("UPDATE request_attachments SET extraction_status = 'failed', extraction_detail = "
                           "'interrupted by a server restart; use Retry' WHERE extraction_status IN "
                           "('pending', 'extracting') AND deleted_at IS NULL")
        return cur.rowcount
