"""
Persistence for per-company Jira credentials (email + API token).

The API token is stored ENCRYPTED in the jira_credentials table, with a
key that lives only in the server's environment (see
credential_crypto.py for the key handling, rotation and what a lost key
means). Saving is refused when no key is configured. Tokens saved in
plaintext by earlier builds are still read, reported as legacy, and
encrypted on the next start once a key is set. The
database file sits under settings.data_dir (./api_data by default),
which is git-ignored (see .gitignore's "api_data/" entry), so it is
never committed. See models/jira_integration.py's own docstring for
the full pilot/production distinction this follows, and
docs/JDE_AI_Driven_Change_Factory_Design_Document_v11.docx Section
19.7 for where it's recorded as explicit future hardening.

The token never leaves this service as part of a router response --
see JiraCredentials' own docstring: it must never be a FastAPI
response_model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models.jira_integration import JiraCredentials, JiraCredentialsUpdate
from ..persistence.db import connection
from . import credential_crypto


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JiraCredentialsService:
    def get_for_customer(self, customer_id: str) -> Optional[JiraCredentials]:
        with connection() as conn:
            row = conn.execute(
                "SELECT * FROM jira_credentials WHERE company_id = ?", (customer_id,)
            ).fetchone()
        if row is None:
            return None
        return JiraCredentials(
            customer_id=row["company_id"], email=row["email"],
            api_token=credential_crypto.decrypt(row["api_token"]),  # raises CredentialUnreadable
            revision=row["revision"], updated_at=row["updated_at"], updated_by=row["updated_by"],
        )

    def is_configured(self, customer_id: str) -> bool:
        try:
            creds = self.get_for_customer(customer_id)
        except credential_crypto.CredentialUnreadable:
            return False  # an unreadable token is not a usable credential
        return bool(creds and creds.email and creds.api_token)

    def storage_status(self, customer_id: str) -> str:
        """none / encrypted / plaintext (legacy) / unreadable -- never the value."""
        with connection() as conn:
            row = conn.execute("SELECT api_token FROM jira_credentials WHERE company_id = ?", (customer_id,)).fetchone()
        if row is None:
            return "none"
        if not credential_crypto.is_encrypted(row["api_token"]):
            return "plaintext (legacy)"
        try:
            credential_crypto.decrypt(row["api_token"])
        except credential_crypto.CredentialUnreadable:
            return "unreadable"
        return "encrypted"

    def reencrypt_stored(self) -> int:
        """At startup: encrypt legacy plaintext tokens and move tokens under
        a previous key to the current one. Unreadable values are left as
        they are (reported as unreadable). Returns how many were rewritten."""
        if not credential_crypto.is_configured():
            return 0
        rewritten = 0
        with connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT company_id, api_token FROM jira_credentials").fetchall()
            for row in rows:
                if not credential_crypto.needs_reencryption(row["api_token"]):
                    continue
                try:
                    plaintext = credential_crypto.decrypt(row["api_token"])
                except credential_crypto.CredentialUnreadable:
                    continue
                conn.execute(
                    "UPDATE jira_credentials SET api_token = ? WHERE company_id = ?",
                    (credential_crypto.encrypt(plaintext), row["company_id"]),
                )
                rewritten += 1
        return rewritten

    def upsert(self, customer_id: str, payload: JiraCredentialsUpdate, actor: str) -> JiraCredentials:
        """A deliberate overwrite (see JiraCredentialsUpdate), attributed
        to the authenticated actor; the revision still increments so the
        change history is visible. Raises CredentialKeyMissing when no
        encryption key is configured -- nothing is stored in that case."""
        stored_token = credential_crypto.encrypt(payload.api_token)
        with connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM jira_credentials WHERE company_id = ?", (customer_id,)
            ).fetchone()
            creds = JiraCredentials(
                customer_id=customer_id,
                email=payload.email,
                api_token=payload.api_token,
                revision=(row["revision"] + 1) if row else 1,
                updated_at=_now(),
                updated_by=actor,
            )
            conn.execute(
                "INSERT INTO jira_credentials (company_id, email, api_token, updated_at, updated_by, revision) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(company_id) DO UPDATE SET email=excluded.email, api_token=excluded.api_token, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by, revision=excluded.revision",
                (customer_id, creds.email, stored_token, creds.updated_at, creds.updated_by, creds.revision),
            )
        return creds

    def delete(self, customer_id: str) -> None:
        """Disconnect -- removes this customer's stored credential
        entirely (not just blanking the fields), so
        jira_is_live_for_customer goes back to false immediately.
        Idempotent -- deleting a row that isn't there is not an error."""
        with connection() as conn:
            conn.execute("DELETE FROM jira_credentials WHERE company_id = ?", (customer_id,))
