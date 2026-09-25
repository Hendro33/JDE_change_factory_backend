"""
Technical baseline artifacts and reference documents -- the evidence live
discovery cannot provide (source, event rules, object specifications,
manuals).

  * Metadata in SQLite; file bytes behind ArtifactStore, a replaceable
    interface (today: local files under the data directory, reachable
    only through the authenticated API).
  * Every upload is an immutable revision with its SHA-256. Uploading the
    same object again adds revision N+1; nothing is overwritten.
  * Company-scoped always; optionally domain-scoped. A story only sees its
    company's company-wide artifacts and those of its own domain.
  * Only formats Jade can safely turn into text are extracted. Everything
    else is kept, listed, and explicitly "unavailable for analysis".
  * Content is evidence, never instructions: it is returned wrapped and
    labelled as data.
  * Whether an export matches the active DEV runtime is what the
    customer/CNC STATES -- recorded as an attestation, never assumed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional, Protocol

from ..config import settings
from ..persistence.db import connection
from .models import ArtifactUpload

MAX_BYTES = 2 * 1024 * 1024
MAX_EXTRACT_CHARS = 60_000
# jade_sim_er: the SYNTHETIC simulation format (jde_mcp_server/technical_sim.py).
TEXT_FORMATS = {"text", "c_source", "er_text", "jade_sim_er", "omw_xml", "json", "markdown", "csv"}


class ArtifactRejected(ValueError):
    pass


class ArtifactStore(Protocol):
    def put(self, company_id: str, data: bytes) -> str: ...
    def get(self, storage_key: str) -> bytes: ...


class LocalArtifactStore:
    """Content-addressed files under <data_dir>/artifacts/<company>/. Swap for
    object storage by implementing the same two methods."""

    def __init__(self, root: Optional[str] = None) -> None:
        self.root = root or os.path.join(settings.data_dir, "artifacts")

    def put(self, company_id: str, data: bytes) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", company_id):
            raise ArtifactRejected("invalid company id")
        digest = hashlib.sha256(data).hexdigest()
        key = f"{company_id}/{digest}"
        path = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return key

    def get(self, storage_key: str) -> bytes:
        if ".." in storage_key or storage_key.startswith("/"):
            raise ArtifactRejected("invalid storage key")
        with open(os.path.join(self.root, storage_key), "rb") as f:
            return f.read()


def default_store() -> ArtifactStore:
    return LocalArtifactStore()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", text.upper()).strip("-")[:40] or "X"


def artifact_id_for(kind: str, object_name: str, object_type: str) -> str:
    prefix = "DOC" if kind == "reference_document" else "ART"
    return f"{prefix}-{_slug(object_type)}-{_slug(object_name)}"


def _extract(fmt: str, data: bytes) -> tuple[str, str, Optional[str], dict]:
    """(status, note, text, coverage). Unsupported formats are never parsed.
    coverage states exactly how much of the file can be analysed."""
    if fmt not in TEXT_FORMATS:
        return ("unsupported", f"{fmt} files are stored but not analysed: no safe text extraction for this format",
                None, {"analysed_chars": 0, "total_chars": None, "truncated": False, "analysed": False})
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ("unsupported", "declared as text but not valid UTF-8; not analysed", None,
                {"analysed_chars": 0, "total_chars": None, "truncated": False, "analysed": False})
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    total = len(text)
    note = ""
    if total > MAX_EXTRACT_CHARS:
        text = text[:MAX_EXTRACT_CHARS]
        note = (f"TRUNCATED: only the first {MAX_EXTRACT_CHARS:,} of {total:,} characters "
                f"({MAX_EXTRACT_CHARS * 100 // total}%) are available for analysis; the rest was not read")
    return "supported", note, text, {"analysed_chars": len(text), "total_chars": total,
                                     "truncated": total > MAX_EXTRACT_CHARS, "analysed": True}


def upload(company_id: str, payload: ArtifactUpload, *, actor: str, store: Optional[ArtifactStore] = None) -> dict:
    try:
        data = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArtifactRejected("content_base64 is not valid base64") from exc
    if not data:
        raise ArtifactRejected("the file is empty")
    if len(data) > MAX_BYTES:
        raise ArtifactRejected(f"at most {MAX_BYTES // 1024} KB per file in this increment")
    if payload.domain_id:
        from ..services.registry import get_business_domain_service

        if get_business_domain_service().get_for_customer(payload.domain_id, company_id) is None:
            raise ArtifactRejected("no such business domain for this company")
    if payload.kind == "reference_document" and not payload.doc_title.strip():
        raise ArtifactRejected("a reference document needs its title")
    try:
        datetime.fromisoformat(payload.exported_at)
    except ValueError as exc:
        raise ArtifactRejected("exported_at must be an ISO-8601 time") from exc
    store = store or default_store()
    sha = hashlib.sha256(data).hexdigest()
    storage_key = store.put(company_id, data)
    status, note, text, coverage = _extract(payload.export_format, data)
    if text is not None:
        text_key = store.put(company_id, text.encode("utf-8"))
    else:
        text_key = None
    artifact_id = artifact_id_for(payload.kind, payload.object_name, payload.object_type)
    meta = payload.model_dump(exclude={"content_base64", "domain_id", "kind"})
    meta["text_storage_key"] = text_key
    meta["analysis_coverage"] = coverage
    now = datetime.now(timezone.utc).isoformat()
    with connection(immediate=True) as conn:
        current = conn.execute(
            "SELECT MAX(revision) AS r FROM technical_artifacts WHERE artifact_id = ? AND company_id = ?",
            (artifact_id, company_id),
        ).fetchone()["r"] or 0
        revision = current + 1
        conn.execute(
            "INSERT INTO technical_artifacts (artifact_id, revision, company_id, domain_id, kind, meta, sha256, size_bytes, "
            "storage_key, extraction_status, extraction_note, uploaded_by, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, revision, company_id, payload.domain_id, payload.kind, json.dumps(meta), sha, len(data),
             storage_key, status, note, actor, now),
        )
    return get(company_id, artifact_id, revision)  # type: ignore[return-value]


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["meta"] = json.loads(d["meta"])
    return d


def get(company_id: str, artifact_id: str, revision: Optional[int] = None) -> Optional[dict]:
    """Company-checked: another company's artifact is simply not found."""
    with connection() as conn:
        if revision is None:
            row = conn.execute(
                "SELECT * FROM technical_artifacts WHERE artifact_id = ? AND company_id = ? ORDER BY revision DESC LIMIT 1",
                (artifact_id, company_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM technical_artifacts WHERE artifact_id = ? AND company_id = ? AND revision = ?",
                (artifact_id, company_id, revision),
            ).fetchone()
    return _row_to_dict(row) if row else None


def latest_revision(company_id: str, artifact_id: str) -> int:
    with connection() as conn:
        row = conn.execute("SELECT MAX(revision) AS r FROM technical_artifacts WHERE artifact_id = ? AND company_id = ?",
                           (artifact_id, company_id)).fetchone()
    return row["r"] or 0


def list_for(company_id: str, *, domain_id: Optional[str] = None, all_domains: bool = False,
             latest_only: bool = True) -> list[dict]:
    """all_domains for Admin screens; otherwise company-wide artifacts plus
    those of domain_id (a story's own domain)."""
    with connection() as conn:
        rows = conn.execute("SELECT * FROM technical_artifacts WHERE company_id = ? ORDER BY artifact_id, revision",
                            (company_id,)).fetchall()
    out = [_row_to_dict(r) for r in rows]
    if not all_domains:
        out = [a for a in out if a["domain_id"] in (None, "") or a["domain_id"] == domain_id]
    if latest_only:
        latest: dict[str, dict] = {}
        for a in out:
            latest[a["artifact_id"]] = a
        out = list(latest.values())
    return out


def read_text(company_id: str, artifact: dict, *, store: Optional[ArtifactStore] = None) -> Optional[str]:
    if artifact["company_id"] != company_id or artifact["extraction_status"] != "supported":
        return None
    key = artifact["meta"].get("text_storage_key")
    if not key:
        return None
    return (store or default_store()).get(key).decode("utf-8")


def compatibility(document: dict, application_release: Optional[str], tools_release: Optional[str]) -> str:
    """compatible / incompatible / unknown against the profile's expected
    releases. A document with no stated release is 'unknown', never assumed
    to apply."""
    releases = [r.strip() for r in document["meta"].get("applies_to_releases") or [] if r.strip()]
    if not releases or not (application_release or tools_release):
        return "unknown"
    targets = [t for t in (application_release, tools_release) if t]
    for r in releases:
        for t in targets:
            if t == r or t.startswith(r + ".") or r.startswith(t + "."):
                return "compatible"
    return "incompatible"


def evidence_ref(artifact: dict) -> str:
    return f"{artifact['artifact_id']}@r{artifact['revision']}"
