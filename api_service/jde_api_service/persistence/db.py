"""
Shared SQLite database for data that JsonFileStore's one-document-per-
file model doesn't fit: relational data (users <-> companies <->
roles <-> domain assignments, invitations) and Jira connections, which
move here from JsonFileStore in this same change.

Why SQLite, not a bigger database: nothing else in this project runs a
database server, and this is a prototype -- adding one would be new
infrastructure the current task explicitly doesn't call for. SQLite is
a single file, needs no server, and (like every other store this
service owns) lives under settings.data_dir, so it is covered by
whatever durable disk that already points at -- see config.py's own
comment on JDE_API_DATA_DIR. This is expected to be replaced, not
extended, when the project moves to Azure or another platform.

Why no ORM: every other persistence module in this codebase
(json_file_store.py) is a thin, direct wrapper with no framework
underneath it. sqlite3 is the same idea applied to relational data --
plain SQL, no mapper, nothing to configure.

Migrations: schema changes are numbered, append-only SQL blocks in
migrations.py, applied in order and recorded in schema_migrations
below. A fresh database and a five-versions-old one both end up
identical after ensure_schema() runs; nothing here ever edits a
migration that has already shipped.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from ..config import settings
from .migrations import MIGRATIONS


def db_path() -> str:
    """Read fresh every call (never cached at import time) so tests can
    monkeypatch settings.data_dir per test, exactly like every other
    store in this service already does."""
    override = os.environ.get("JDE_API_DB_PATH")
    if override:
        return override
    return os.path.join(settings.data_dir, "jde.sqlite3")


def get_connection() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Commits on a clean exit, rolls back on an exception, always
    closes -- the pattern every service function in this module uses so
    none of them has to repeat it.

    immediate=True takes SQLite's write lock at the start (BEGIN
    IMMEDIATE), so a read-check-write sequence -- compare a revision,
    re-check the actor's authority, then write -- cannot interleave with
    another writer's."""
    conn = get_connection()
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    """Idempotent -- safe on every startup, same convention
    seed_service.py's dataset seeding already follows. Applies any
    migration not yet recorded in schema_migrations, in order."""
    with connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
                (version,),
            )
