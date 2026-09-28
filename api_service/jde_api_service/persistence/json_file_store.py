"""
One kind of document in Jade's database (jde_mcp_server.docstore).

The class keeps the name and interface it had when each kind was a
directory of JSON files, so every service built on it is unchanged: a
service still says ``JsonFileStore(<data_dir>/delivery_queue)``. The
directory's last path element is now the document kind; nothing is
written to the directory itself. Existing files from an earlier
installation are imported once at start-up (main.py).

``locked()`` is a real database transaction holding the write lock, so
a read-check-write inside it is safe across threads AND processes, and
anything else written in the same block (another document, a table row)
commits or rolls back with it.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from jde_mcp_server import docstore


class JsonFileStore:
    def __init__(self, directory: str) -> None:
        self._dir = directory
        self.kind = os.path.basename(os.path.normpath(directory))

    @property
    def directory(self) -> str:
        """Where this kind's documents lived as files (legacy import source)."""
        return self._dir

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold this while reading, checking a revision and writing, so
        two concurrent saves cannot both pass the check."""
        with docstore.transaction():
            yield

    def get(self, doc_id: str) -> Optional[dict[str, Any]]:
        return docstore.get(self.kind, doc_id)

    def put(self, doc_id: str, document: dict[str, Any]) -> None:
        docstore.put(self.kind, doc_id, document)

    def insert_new(self, doc_id: str, document: dict[str, Any]) -> bool:
        return docstore.insert_new(self.kind, doc_id, document)

    def delete(self, doc_id: str) -> None:
        """Idempotent -- deleting a document that isn't there is not an error."""
        docstore.delete(self.kind, doc_id)

    def list_all(self, *, company_id: Optional[str] = None) -> list[dict[str, Any]]:
        return docstore.list_all(self.kind, company_id=company_id)
