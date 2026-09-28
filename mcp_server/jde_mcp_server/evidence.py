"""
Evidence capture -- Section 7.2 ("Evidence" category), Section 7.3
(capture_evidence tool), and Section 15.5/17.1 (append-only,
tamper-evident evidence).

Evidence is an application-level concern, not a JDE API: each story
stage appends a structured record to the story's log in Jade's
database. Every entry is chained to the previous one by hash, so a
tampered or reordered entry is detectable.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from . import config, docstore

GENESIS_HASH = "GENESIS"


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _entry_hash(prev_hash: str, record_without_hash: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(record_without_hash)).encode("utf-8")).hexdigest()


KIND = "evidence"


def entries(story_id: str) -> list[dict]:
    """The story's evidence log, oldest first."""
    return docstore.get(KIND, story_id) or []


def capture_evidence(story_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Append one entry to the story's hash-chained evidence log. The
    read of the last hash and the append are one database transaction,
    so two writers (the API and the agents' tool server) can never both
    chain onto the same previous entry or lose each other's entries."""
    with docstore.transaction():
        existing: list[dict] = docstore.get(KIND, story_id) or []
        prev_hash = existing[-1]["entry_hash"] if existing else GENESIS_HASH
        record = {"story_id": story_id, "captured_at": time.time(), "prev_hash": prev_hash, **payload}
        record["entry_hash"] = _entry_hash(prev_hash, record)
        existing.append(record)
        docstore.put(KIND, story_id, existing)
    return {"story_id": story_id, "evidence_entries": len(existing), "entry_hash": record["entry_hash"]}


def verify_chain(story_id: str) -> dict[str, Any]:
    """Recomputes every entry's hash from scratch and confirms the chain
    is intact -- proof the tamper-evidence is actually checkable, not
    just a field nobody looks at. Returns the first broken index, if
    any, so a real discrepancy is easy to locate."""
    entries_ = entries(story_id)
    if not entries_:
        return {"story_id": story_id, "valid": True, "entries": 0, "note": "no evidence recorded yet"}

    prev_hash = GENESIS_HASH
    for i, entry in enumerate(entries_):
        claimed_hash = entry.get("entry_hash")
        recomputed_input = {k: v for k, v in entry.items() if k != "entry_hash"}
        expected_hash = _entry_hash(prev_hash, recomputed_input)
        if entry.get("prev_hash") != prev_hash or claimed_hash != expected_hash:
            return {"story_id": story_id, "valid": False, "broken_at_index": i, "entries": len(entries_)}
        prev_hash = claimed_hash

    return {"story_id": story_id, "valid": True, "entries": len(entries_)}
