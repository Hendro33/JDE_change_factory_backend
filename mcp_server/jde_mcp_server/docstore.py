"""
Jade's one persistent store for document-shaped records.

Every record that used to be "one JSON file per id in a directory" --
stories in the backlog, exact changes and their approvals, the evidence
log, change requests, story -> customer links, domain reviews,
architecture reviews, the delivery queue, engagement scopes, business
domains, agent runs, design-baseline hand-offs -- is a row in ONE
table (``documents``) of the same SQLite database that holds users,
customers, settings and the relational process/technical records.

Why this matters for a multi-customer product:

* Atomic and durable: every write is a database transaction, never a
  partially written file.
* Safe across processes: the API and the agents' tool server (a
  separate process) share the database. ``transaction()`` takes the
  database write lock (BEGIN IMMEDIATE), so a read-check-write sequence
  cannot interleave with another process's -- the old per-directory
  thread lock only protected one process.
* One transaction across stores: the API's relational helper
  (persistence/db.py) joins an open transaction here, so a change that
  touches a document and a table commits or rolls back as one.
* Customer-scoped: each row carries the customer it belongs to when the
  document names one, so listings can be filtered in the database.

The database file is shared with the API. Its location comes from the
first of: a resolver the API registers at start-up (so tests that point
the API at a temporary directory are followed exactly), JDE_API_DB_PATH,
JDE_AUTH_DB_PATH, JDE_API_DATA_DIR/jde.sqlite3.
"""

from __future__ import annotations

import contextvars
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

_resolver: Optional[Callable[[], str]] = None


def set_db_path_resolver(resolver: Optional[Callable[[], str]]) -> None:
    """The API registers its own db_path() so both always agree."""
    global _resolver
    _resolver = resolver


def db_path() -> str:
    if _resolver is not None:
        return _resolver()
    for name in ("JDE_API_DB_PATH", "JDE_AUTH_DB_PATH"):
        value = os.environ.get(name)
        if value:
            return value
    return os.path.join(os.environ.get("JDE_API_DATA_DIR", "./api_data"), "jde.sqlite3")


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    kind TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    company_id TEXT,
    body TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_company ON documents(kind, company_id);
CREATE TABLE IF NOT EXISTS advisory_locks (
    name TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_imports (
    source TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    documents INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
"""

_schema_ready: set[str] = set()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    """A new connection with the settings every Jade connection uses:
    rows by column name, foreign keys enforced, WAL journal (readers never
    block the writer), and a generous wait for the write lock instead of
    failing at once when another process is writing."""
    path = path or db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if path not in _schema_ready:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _schema_ready.add(path)
    return conn


# The transaction open in this execution context, if any: (connection, path).
_current: contextvars.ContextVar[Optional[tuple[sqlite3.Connection, str]]] = contextvars.ContextVar(
    "jade_docstore_tx", default=None)


def current_connection(path: Optional[str] = None) -> Optional[sqlite3.Connection]:
    """The connection of the transaction open in this context, if it is on
    the same database file."""
    active = _current.get()
    if active is None:
        return None
    conn, active_path = active
    if path is not None and os.path.abspath(path) != os.path.abspath(active_path):
        return None
    return conn


@contextmanager
def transaction(*, immediate: bool = True, path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """Run a block as one database transaction. Nested calls join the
    outer transaction. immediate=True (the default) takes the write lock
    at the start, which is what a read-check-write needs."""
    path = path or db_path()
    joined = current_connection(path)
    if joined is not None:
        yield joined
        return
    conn = connect(path)
    token = _current.set((conn, path))
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        _current.reset(token)
        conn.close()


def _company_of(document: Any) -> Optional[str]:
    if isinstance(document, dict):
        for key in ("company_id", "customer_id", "companyId", "customerId"):
            value = document.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def get(kind: str, doc_id: str) -> Optional[Any]:
    with transaction(immediate=False) as conn:
        row = conn.execute("SELECT body FROM documents WHERE kind = ? AND doc_id = ?", (kind, doc_id)).fetchone()
    return json.loads(row["body"]) if row else None


def exists(kind: str, doc_id: str) -> bool:
    with transaction(immediate=False) as conn:
        return conn.execute("SELECT 1 FROM documents WHERE kind = ? AND doc_id = ?", (kind, doc_id)).fetchone() is not None


def put(kind: str, doc_id: str, document: Any, *, company_id: Optional[str] = None) -> None:
    body = json.dumps(document, default=str, sort_keys=False)
    company = company_id or _company_of(document)
    now = _now()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO documents (kind, doc_id, company_id, body, version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(kind, doc_id) DO UPDATE SET body = excluded.body, "
            "company_id = COALESCE(excluded.company_id, documents.company_id), "
            "version = documents.version + 1, updated_at = excluded.updated_at",
            (kind, doc_id, company, body, now, now))


def insert_new(kind: str, doc_id: str, document: Any, *, company_id: Optional[str] = None) -> bool:
    """Insert only if no document with this id exists. Returns False
    (and writes nothing) when one does -- a race-free "create"."""
    body = json.dumps(document, default=str)
    now = _now()
    with transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO documents (kind, doc_id, company_id, body, version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (kind, doc_id, company_id or _company_of(document), body, now, now))
        return cur.rowcount == 1


def delete(kind: str, doc_id: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM documents WHERE kind = ? AND doc_id = ?", (kind, doc_id))


def list_all(kind: str, *, company_id: Optional[str] = None) -> list[Any]:
    with transaction(immediate=False) as conn:
        if company_id is None:
            rows = conn.execute("SELECT body FROM documents WHERE kind = ? ORDER BY doc_id", (kind,)).fetchall()
        else:
            rows = conn.execute("SELECT body FROM documents WHERE kind = ? AND company_id = ? ORDER BY doc_id",
                                (kind, company_id)).fetchall()
    return [json.loads(r["body"]) for r in rows]


def list_ids(kind: str) -> list[str]:
    with transaction(immediate=False) as conn:
        return [r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents WHERE kind = ? ORDER BY doc_id", (kind,))]


def company_of(kind: str, doc_id: str) -> Optional[str]:
    with transaction(immediate=False) as conn:
        row = conn.execute("SELECT company_id FROM documents WHERE kind = ? AND doc_id = ?", (kind, doc_id)).fetchone()
    return row["company_id"] if row else None


# ---------------------------------------------------------------------
# Advisory locks: a named mutex across processes for work that must not
# hold the database write lock while it waits on a network call (an
# execution attempt against JD Edwards, for example).
# ---------------------------------------------------------------------
class LockTimeout(RuntimeError):
    pass


@contextmanager
def advisory_lock(name: str, *, wait_seconds: float = 30.0, stale_after_seconds: float = 900.0) -> Iterator[None]:
    holder = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    deadline = time.monotonic() + wait_seconds
    while True:
        with transaction() as conn:
            row = conn.execute("SELECT holder, acquired_at FROM advisory_locks WHERE name = ?", (name,)).fetchone()
            if row is None or time.time() - row["acquired_at"] > stale_after_seconds:
                conn.execute("INSERT OR REPLACE INTO advisory_locks (name, holder, acquired_at) VALUES (?, ?, ?)",
                             (name, holder, time.time()))
                break
        if time.monotonic() >= deadline:
            raise LockTimeout(f"{name} is held by another operation; try again shortly")
        time.sleep(0.05)
    try:
        yield
    finally:
        with transaction() as conn:
            conn.execute("DELETE FROM advisory_locks WHERE name = ? AND holder = ?", (name, holder))


# ---------------------------------------------------------------------
# One-time import of the old file-per-document directories, so an
# existing installation keeps its data. Files are read, never removed.
# ---------------------------------------------------------------------
def import_directory(kind: str, directory: str) -> int:
    source = f"{kind}:{os.path.abspath(directory)}"
    if not directory or not os.path.isdir(directory):
        return 0
    with transaction() as conn:
        if conn.execute("SELECT 1 FROM legacy_imports WHERE source = ?", (source,)).fetchone():
            return 0
        count = 0
        for fn in sorted(os.listdir(directory)):
            if not fn.endswith(".json") or fn.startswith("."):
                continue
            try:
                with open(os.path.join(directory, fn), encoding="utf-8") as f:
                    document = json.load(f)
            except (OSError, ValueError):
                continue
            doc_id = fn[:-5]
            now = _now()
            cur = conn.execute(
                "INSERT OR IGNORE INTO documents (kind, doc_id, company_id, body, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                (kind, doc_id, _company_of(document), json.dumps(document, default=str), now, now))
            count += cur.rowcount
        conn.execute("INSERT INTO legacy_imports (source, kind, documents, imported_at) VALUES (?, ?, ?, ?)",
                     (source, kind, count, _now()))
    return count
