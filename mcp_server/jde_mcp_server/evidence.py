"""
Evidence capture -- Section 7.2 ("Evidence" category), Section 7.3
(capture_evidence tool), and Section 15.5/17.1 (append-only,
tamper-evident evidence).

Evidence is an application-level concern, not a JDE API: each story
stage writes a structured JSON record here rather than fetching
anything from JDE. This is deliberately file-backed for the pilot
(Section 18 calls a persistent audit/event store a product-ready
capability, not a pilot one) -- but "file-backed" and "tamper-evident"
are different questions, and this module answers the second one now:
every entry is chained to the previous one by hash, so a tampered or
reordered entry is detectable even in a plain JSON file.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from . import config

GENESIS_HASH = "GENESIS"


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _entry_hash(prev_hash: str, record_without_hash: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(record_without_hash)).encode("utf-8")).hexdigest()


def capture_evidence(story_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    os.makedirs(config.settings.evidence_dir, exist_ok=True)
    path = os.path.join(config.settings.evidence_dir, f"{story_id}.json")

    existing: list[dict] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)

    prev_hash = existing[-1]["entry_hash"] if existing else GENESIS_HASH

    record = {"story_id": story_id, "captured_at": time.time(), "prev_hash": prev_hash, **payload}
    record["entry_hash"] = _entry_hash(prev_hash, record)

    existing.append(record)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)

    return {"story_id": story_id, "evidence_entries": len(existing), "path": path, "entry_hash": record["entry_hash"]}


def verify_chain(story_id: str) -> dict[str, Any]:
    """Recomputes every entry's hash from scratch and confirms the chain
    is intact -- proof the tamper-evidence is actually checkable, not
    just a field nobody looks at. Returns the first broken index, if
    any, so a real discrepancy is easy to locate."""
    path = os.path.join(config.settings.evidence_dir, f"{story_id}.json")
    if not os.path.exists(path):
        return {"story_id": story_id, "valid": True, "entries": 0, "note": "no evidence recorded yet"}

    with open(path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    prev_hash = GENESIS_HASH
    for i, entry in enumerate(entries):
        claimed_hash = entry.get("entry_hash")
        recomputed_input = {k: v for k, v in entry.items() if k != "entry_hash"}
        expected_hash = _entry_hash(prev_hash, recomputed_input)
        if entry.get("prev_hash") != prev_hash or claimed_hash != expected_hash:
            return {"story_id": story_id, "valid": False, "broken_at_index": i, "entries": len(entries)}
        prev_hash = claimed_hash

    return {"story_id": story_id, "valid": True, "entries": len(entries)}
