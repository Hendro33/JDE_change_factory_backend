"""
A story's business classification: its priority and its change type.

People set these (when a request is raised, and later in the story); no
agent sets them, and they never take part in an approval, route, lifecycle
or execution decision. One document per story, with a revision (a save
based on an older revision is refused) and the history of every change.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional, get_args

from ..config import settings
from ..models.change import ChangeType, Priority
from ..persistence.json_file_store import JsonFileStore

PRIORITIES: tuple[str, ...] = get_args(Priority)
CHANGE_TYPES: tuple[str, ...] = get_args(ChangeType)


class ClassificationConflict(RuntimeError):
    """The classification changed since the caller loaded it."""


def _store() -> JsonFileStore:
    return JsonFileStore(os.path.join(settings.data_dir, "story_classifications"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(story_id: str) -> Optional[dict]:
    return _store().get(story_id)


def set_classification(story_id: str, customer_id: str, *, priority: Optional[str], change_type: Optional[str],
                       actor_id: str, actor: str, expected_revision: Optional[int]) -> dict:
    """Saves whichever of the two values is given. `expected_revision` is the
    revision the caller loaded (0 when none was set yet); None skips the
    check, for the value chosen when a request is first raised."""
    if priority is not None and priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
    if change_type is not None and change_type not in CHANGE_TYPES:
        raise ValueError(f"change type must be one of {', '.join(CHANGE_TYPES)}")
    store = _store()
    with store.locked():
        current = store.get(story_id) or {"story_id": story_id, "company_id": customer_id, "revision": 0,
                                         "priority": None, "change_type": None, "history": []}
        if current["company_id"] != customer_id:
            raise ClassificationConflict("this story belongs to another customer")
        if expected_revision is not None and expected_revision != current["revision"]:
            raise ClassificationConflict("The priority or change type was changed meanwhile. Reload and try again.")
        changes = {k: v for k, v in (("priority", priority), ("change_type", change_type))
                   if v is not None and v != current[k]}
        if not changes:
            return current
        now = _now()
        current.update(changes)
        current.update(revision=current["revision"] + 1, updated_by=actor, updated_by_id=actor_id, updated_at=now)
        current["history"].append({"at": now, "by": actor, "by_id": actor_id, **changes})
        store.put(story_id, current)
        return current


def apply(change):
    """Overlays the stored classification on an assembled Change."""
    stored = get(change.id)
    if stored and stored.get("company_id") == change.customer_id:
        if stored.get("priority"):
            change.priority = stored["priority"]
        if stored.get("change_type"):
            change.change_type = stored["change_type"]
        change.classification_revision = stored["revision"]
        change.classified_by = stored.get("updated_by")
        change.classified_at = stored.get("updated_at")
    return change
