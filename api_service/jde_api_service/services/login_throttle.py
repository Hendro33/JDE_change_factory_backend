"""
Sign-in rate limiting.

Two limits, both over a sliding window, both counting FAILED attempts
only, both kept in SQLite so a restart does not reset them:

  * per account: ACCOUNT_LIMIT failures for one email address, then that
    account is refused for the rest of the window -- stops password
    guessing against a known user, from any number of addresses;
  * per client: CLIENT_LIMIT failures from one address across any
    accounts -- stops one source spraying many accounts.

A refused attempt is not checked against the password at all, so the
answer never reveals whether the password was right. A successful
sign-in clears that account's failures. The response is the same for a
known and an unknown email address.

The client address is the TCP peer unless JDE_TRUST_PROXY_HEADERS=true,
in which case the first X-Forwarded-For entry is used. Only set that
behind a proxy that overwrites the header (Render does), otherwise any
caller could choose their own address.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import Request

from ..persistence.db import connection

WINDOW_SECONDS = 15 * 60
ACCOUNT_LIMIT = 5
CLIENT_LIMIT = 20


def client_address(request: Request) -> str:
    if (os.environ.get("JDE_TRUST_PROXY_HEADERS") or "").strip().lower() in ("1", "true", "yes"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded.strip():
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _retry_after(conn, scope: str, key: str, limit: int, now: float) -> Optional[int]:
    rows = conn.execute(
        "SELECT failed_at FROM login_failures WHERE scope = ? AND key = ? AND failed_at > ? ORDER BY failed_at",
        (scope, key, now - WINDOW_SECONDS),
    ).fetchall()
    if len(rows) < limit:
        return None
    # Blocked until enough of the counted failures age out of the window.
    return max(1, int(rows[len(rows) - limit]["failed_at"] + WINDOW_SECONDS - now) + 1)


def check(email: str, client: str) -> Optional[int]:
    """Seconds to wait if this attempt must be refused, else None."""
    now = time.time()
    with connection() as conn:
        waits = [
            w for w in (
                _retry_after(conn, "account", email.strip().lower(), ACCOUNT_LIMIT, now),
                _retry_after(conn, "client", client, CLIENT_LIMIT, now),
            ) if w is not None
        ]
    return max(waits) if waits else None


def record_failure(email: str, client: str) -> None:
    now = time.time()
    with connection() as conn:
        conn.execute("DELETE FROM login_failures WHERE failed_at < ?", (now - 24 * 3600,))
        conn.executemany(
            "INSERT INTO login_failures (scope, key, failed_at) VALUES (?, ?, ?)",
            [("account", email.strip().lower(), now), ("client", client, now)],
        )


def record_success(email: str) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM login_failures WHERE scope = 'account' AND key = ?", (email.strip().lower(),))
