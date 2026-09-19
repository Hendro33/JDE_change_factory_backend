"""
Small generic file-backed JSON store -- one JSON document per id, one
file per document, same "directory of JSON files" pattern backlog.py /
approval.py / evidence.py already use in mcp_server. This is Phase 1's
persistence for data api_service owns itself (change requests, the
customer-link sidecar): plain files, matching the maturity of
everything else in the pilot (Section 18 -- a real datastore is a
product-ready step, not a pilot one). No implicit sharing with
mcp_server's own directories.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional


class JsonFileStore:
    def __init__(self, directory: str) -> None:
        self._dir = directory

    def _path(self, doc_id: str) -> str:
        os.makedirs(self._dir, exist_ok=True)
        safe_id = doc_id.replace("/", "_")
        return os.path.join(self._dir, f"{safe_id}.json")

    def get(self, doc_id: str) -> Optional[dict[str, Any]]:
        path = self._path(doc_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def put(self, doc_id: str, document: dict[str, Any]) -> None:
        with open(self._path(doc_id), "w", encoding="utf-8") as f:
            json.dump(document, f, indent=2, default=str)

    def list_all(self) -> list[dict[str, Any]]:
        if not os.path.isdir(self._dir):
            return []
        out = []
        for fn in sorted(os.listdir(self._dir)):
            if fn.endswith(".json"):
                with open(os.path.join(self._dir, fn), "r", encoding="utf-8") as f:
                    out.append(json.load(f))
        return out
