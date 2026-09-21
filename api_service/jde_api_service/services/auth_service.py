"""
Password hashing, session tokens, and the login/logout/reset mechanics
built on them.

Established libraries, not hand-rolled crypto: bcrypt (a widely audited
password-hashing algorithm, not something this codebase implements
itself) for passwords, and Python's own `secrets` module (the standard
library's CSPRNG, meant exactly for this) for session, invitation and
password-reset tokens. Only a SHA-256 hash of each token is ever stored
-- the same "a leaked database row is not a usable credential"
principle bcrypt already gives passwords, applied to tokens too. The
raw token is handed to the caller once (a cookie, a link) and never
persisted anywhere.

Sessions are a real server-side record (the `sessions` table), not a
self-contained signed cookie -- deactivating a session (logout, or an
admin revoking access) takes effect immediately, because every request
re-reads this table rather than trusting a token's own claims.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt

from ..persistence.db import connection

SESSION_COOKIE_NAME = "jde_session"
SESSION_TTL_DAYS = 14
PASSWORD_RESET_TTL_HOURS = 1
INVITATION_TTL_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        # Malformed hash -- never happens for a hash we wrote ourselves,
        # but a wrong/corrupt value must fail closed, not raise past
        # the caller's login check.
        return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class User:
    id: str
    email: str
    display_name: str
    is_active: bool
    password_hash: str = ""  # never serialised outward; present so set_password's read-modify-write doesn't need a second query


class EmailAlreadyRegistered(RuntimeError):
    pass


def create_user(email: str, password: str, display_name: str, *, user_id: Optional[str] = None) -> User:
    uid = user_id or f"u-{uuid.uuid4().hex[:12]}"
    now = _iso(_now())
    with connection() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing is not None:
            raise EmailAlreadyRegistered(email)
        conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (uid, email, hash_password(password), display_name, now, now),
        )
    return User(id=uid, email=email, display_name=display_name, is_active=True)


def _row_to_user(row) -> User:
    return User(
        id=row["id"], email=row["email"], display_name=row["display_name"],
        is_active=bool(row["is_active"]), password_hash=row["password_hash"],
    )


def get_user_by_email(email: str) -> Optional[User]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)).fetchone()
        return _row_to_user(row) if row else None


def get_user_by_id(user_id: str) -> Optional[User]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row) if row else None


def set_password(user_id: str, new_password: str) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (hash_password(new_password), _iso(_now()), user_id),
        )


def authenticate(email: str, password: str) -> Optional[User]:
    """None on any failure -- unknown email and wrong password are
    deliberately indistinguishable to the caller, so a login form can't
    be used to enumerate registered addresses."""
    user = get_user_by_email(email)
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


# ---------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------
def create_session(user_id: str) -> str:
    """Returns the RAW token -- store it in the response cookie only,
    never anywhere else. Only its hash is persisted."""
    raw = generate_token()
    now = _now()
    with connection() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, created_at, expires_at, revoked_at) VALUES (?, ?, ?, ?, NULL)",
            (hash_token(raw), user_id, _iso(now), _iso(now + timedelta(days=SESSION_TTL_DAYS))),
        )
    return raw


def get_user_for_session(raw_token: str) -> Optional[User]:
    with connection() as conn:
        row = conn.execute(
            "SELECT s.expires_at, s.revoked_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.id = ?",
            (hash_token(raw_token),),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        if datetime.fromisoformat(row["expires_at"]) < _now():
            return None
        user = _row_to_user(row)
        return user if user.is_active else None


def revoke_session(raw_token: str) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_iso(_now()), hash_token(raw_token)),
        )


def revoke_all_sessions_for_user(user_id: str) -> None:
    """Not used by any endpoint yet, but the mechanism a future
    'log out everywhere' action or a forced password reset would call
    -- kept here rather than duplicated later."""
    with connection() as conn:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (_iso(_now()), user_id),
        )


# ---------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------
def create_password_reset_token(user_id: str) -> str:
    raw = generate_token()
    now = _now()
    with connection() as conn:
        conn.execute(
            "INSERT INTO password_reset_tokens (id, user_id, token_hash, created_at, expires_at, used_at) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (
                f"prt-{uuid.uuid4().hex[:12]}", user_id, hash_token(raw),
                _iso(now), _iso(now + timedelta(hours=PASSWORD_RESET_TTL_HOURS)),
            ),
        )
    return raw


def consume_password_reset_token(raw_token: str) -> Optional[str]:
    """Returns the user_id and marks the token used (single-use), or
    None if the token is unknown, expired, or already used."""
    token_hash = hash_token(raw_token)
    with connection() as conn:
        row = conn.execute(
            "SELECT id, user_id, expires_at, used_at FROM password_reset_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None or row["used_at"] is not None:
            return None
        if datetime.fromisoformat(row["expires_at"]) < _now():
            return None
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE id = ?", (_iso(_now()), row["id"])
        )
        return row["user_id"]
