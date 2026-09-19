"""
Sidecar mapping from mcp_server story_id -> customer_id.

Why this exists as a SIDECAR rather than a field inside backlog.py's
own records: mcp_server is explicitly not to be modified for Phase 1
(sibling application, unchanged MCP tool behaviour). backlog.py's
records have no customer_id field, and stories can currently be
created by a human running agents interactively in Claude Code, fully
outside this API's control.

The fail-safe default is exclusion: a story with no link entry is
invisible to every customer-scoped read. This is the secure default --
an unattributed story is never guessed into a customer's view. Once
Claude Agent SDK orchestration exists (a later phase) and story
creation runs through this API end to end, linking can happen
automatically at creation time; for now, tests link stories explicitly
via link(), and any story created by a human outside the API stays
unlisted until it's linked the same way.
"""

from __future__ import annotations

from ..persistence.json_file_store import JsonFileStore


class CustomerLinkService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def link(self, story_id: str, customer_id: str) -> None:
        self._store.put(story_id, {"story_id": story_id, "customer_id": customer_id})

    def customer_for(self, story_id: str) -> str | None:
        doc = self._store.get(story_id)
        return doc["customer_id"] if doc else None
