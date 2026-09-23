"""
Optimistic concurrency for mutable setup records.

Every setup record carries an integer `revision`, starting at 1 and
incremented on each save. A client saving an existing record must send
the revision it loaded; a different current revision means someone else
saved in between, and the save is refused instead of silently
overwriting their change. Routers map these to HTTP 409 / 428.
"""

from __future__ import annotations

from typing import Optional


class RevisionConflict(Exception):
    def __init__(self, current_revision: int) -> None:
        super().__init__(
            f"this record was changed by someone else (now at revision {current_revision}) -- "
            "reload it and re-apply your change"
        )
        self.current_revision = current_revision


class RevisionRequired(Exception):
    def __init__(self, current_revision: int) -> None:
        super().__init__(
            f"this record already exists (revision {current_revision}); send expectedRevision "
            "so an older copy cannot silently overwrite a newer one"
        )
        self.current_revision = current_revision


def next_revision(current: Optional[int], expected: Optional[int]) -> int:
    """Return the revision the saved record will carry, or raise.

    - No existing record: expected must be absent or 0.
    - Existing record: expected must be present and equal to current.
    """
    if current is None:
        if expected not in (None, 0):
            raise RevisionConflict(0)
        return 1
    if expected is None:
        raise RevisionRequired(current)
    if expected != current:
        raise RevisionConflict(current)
    return current + 1
