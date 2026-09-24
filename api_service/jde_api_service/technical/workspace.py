"""
The Technical Agent's isolated workspace for one run.

A workspace is a private directory per run under the API data directory.
Opening a source artifact COPIES its immutable original (the artifact store
keeps the original and its checksum untouched); the agent can then only
view numbered lines and make exact, bounded replacements through typed
tools. There is no shell, no path the agent chooses, no network. The
candidate and the exact unified diff are computed by Jade from the copy,
never taken from the model's description.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
from typing import Optional

from ..config import settings

MAX_EDIT_CHARS = 4000
_lock = threading.Lock()


class WorkspaceError(ValueError):
    pass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Workspace:
    def __init__(self, run_id: str) -> None:
        if not re.fullmatch(r"TR-[0-9a-f]{10}", run_id):
            raise WorkspaceError("invalid run id")
        self.root = os.path.join(settings.data_dir, "technical_workspaces", run_id)
        os.makedirs(self.root, exist_ok=True)
        self._index_path = os.path.join(self.root, "index.json")

    def _index(self) -> dict:
        if not os.path.exists(self._index_path):
            return {}
        with open(self._index_path, encoding="utf-8") as f:
            return json.load(f)

    def _save_index(self, index: dict) -> None:
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2)
        os.replace(tmp, self._index_path)

    def _path(self, file_id: str) -> str:
        if not re.fullmatch(r"F[0-9]{1,3}", file_id):
            raise WorkspaceError("invalid workspace file id")
        return os.path.join(self.root, f"{file_id}.txt")

    def open(self, *, evidence_id: str, artifact: dict, original_text: str, object_key: str, fmt: str) -> dict:
        """Copy an original into the workspace (idempotent per evidence id)."""
        with _lock:
            index = self._index()
            for file_id, entry in index.items():
                if entry["evidence_id"] == evidence_id:
                    return {"file_id": file_id, **entry}
            if _sha(original_text) != artifact["sha256"]:
                raise WorkspaceError(f"{evidence_id}: the stored original does not match its checksum -- refusing")
            file_id = f"F{len(index) + 1}"
            entry = {"evidence_id": evidence_id, "artifact_id": artifact["artifact_id"], "revision": artifact["revision"],
                     "original_sha256": artifact["sha256"], "object_key": object_key, "format": fmt,
                     "file_name": artifact["meta"].get("file_name") or f"{artifact['artifact_id']}.txt"}
            with open(self._path(file_id), "w", encoding="utf-8") as f:
                f.write(original_text)
            with open(os.path.join(self.root, f"{file_id}.original"), "w", encoding="utf-8") as f:
                f.write(original_text)
            index[file_id] = entry
            self._save_index(index)
            return {"file_id": file_id, **entry}

    def files(self) -> dict:
        return self._index()

    def text(self, file_id: str) -> str:
        if file_id not in self._index():
            raise WorkspaceError(f"no workspace file {file_id}")
        with open(self._path(file_id), encoding="utf-8") as f:
            return f.read()

    def original(self, file_id: str) -> str:
        with open(os.path.join(self.root, f"{file_id}.original"), encoding="utf-8") as f:
            return f.read()

    def view(self, file_id: str, start: int = 1, end: Optional[int] = None) -> dict:
        lines = self.text(file_id).splitlines()
        start = max(1, int(start or 1))
        end = min(len(lines), int(end or len(lines)))
        return {"file_id": file_id, "total_lines": len(lines),
                "lines": [f"{n:4d}| {lines[n - 1]}" for n in range(start, end + 1)]}

    def replace(self, file_id: str, old: str, new: str) -> dict:
        """Replace exactly one occurrence of `old` with `new`."""
        if not old:
            raise WorkspaceError("old_text must not be empty")
        if len(old) > MAX_EDIT_CHARS or len(new) > MAX_EDIT_CHARS:
            raise WorkspaceError(f"one edit may replace at most {MAX_EDIT_CHARS} characters")
        with _lock:
            text = self.text(file_id)
            count = text.count(old)
            if count != 1:
                raise WorkspaceError(f"old_text occurs {count} times in {file_id}; it must match exactly once")
            updated = text.replace(old, new, 1)
            with open(self._path(file_id), "w", encoding="utf-8") as f:
                f.write(updated)
        return {"file_id": file_id, "diff": self.diff(file_id)}

    def diff(self, file_id: str) -> str:
        entry = self._index()[file_id]
        return "".join(difflib.unified_diff(
            self.original(file_id).splitlines(keepends=True), self.text(file_id).splitlines(keepends=True),
            fromfile=f"a/{entry['file_name']} ({entry['evidence_id']}, sha256 {entry['original_sha256'][:12]})",
            tofile=f"b/{entry['file_name']} (candidate)"))

    def changed_files(self) -> list[str]:
        return [f for f in self._index() if self.text(f) != self.original(f)]
