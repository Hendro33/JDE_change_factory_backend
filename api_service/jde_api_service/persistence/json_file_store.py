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
import tempfile
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

# One lock per store directory, shared by every JsonFileStore instance
# pointing at it (services are constructed per request). This makes a
# read-check-write sequence safe within ONE process only -- the pilot's
# deployment shape. Multiple processes or hosts need a real database.
_DIR_LOCKS: dict[str, threading.RLock] = {}
_DIR_LOCKS_GUARD = threading.Lock()


def _lock_for(directory: str) -> threading.RLock:
    key = os.path.abspath(directory)
    with _DIR_LOCKS_GUARD:
        if key not in _DIR_LOCKS:
            _DIR_LOCKS[key] = threading.RLock()
        return _DIR_LOCKS[key]


class JsonFileStore:
    def __init__(self, directory: str) -> None:
        self._dir = directory

    def _path(self, doc_id: str) -> str:
        os.makedirs(self._dir, exist_ok=True)
        safe_id = doc_id.replace("/", "_")
        return os.path.join(self._dir, f"{safe_id}.json")

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold this while reading, checking a revision and writing, so
        two concurrent saves cannot both pass the check."""
        with _lock_for(self._dir):
            yield

    def get(self, doc_id: str) -> Optional[dict[str, Any]]:
        path = self._path(doc_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def put(self, doc_id: str, document: dict[str, Any]) -> None:
        # Write to a temp file in the same directory, then atomically
        # replace -- a crash mid-write leaves the previous version intact
        # instead of a truncated file.
        path = self._path(doc_id)
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(document, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def delete(self, doc_id: str) -> None:
        """Idempotent -- deleting a document that isn't there is not an
        error, same as every other store in this system treats a
        missing record."""
        path = self._path(doc_id)
        if os.path.exists(path):
            os.remove(path)

    def list_all(self) -> list[dict[str, Any]]:
        if not os.path.isdir(self._dir):
            return []
        out = []
        for fn in sorted(os.listdir(self._dir)):
            if fn.endswith(".json"):
                with open(os.path.join(self._dir, fn), "r", encoding="utf-8") as f:
                    out.append(json.load(f))
        return out
