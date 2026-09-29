"""
At start-up, bring every stored secret under the server's current
credential key: the Jira API tokens, each customer's AI provider key and
each customer's JD Edwards credentials (the read-only discovery user and
the DEV write user). A value encrypted under the
previous key (JDE_CREDENTIAL_KEY_PREVIOUS) is re-encrypted; a legacy
plaintext value is encrypted; a value neither key can read is left as it
is and reported as unreadable where it is used.
"""

from __future__ import annotations

from ..persistence.db import connection
from . import credential_crypto

# (table, key column, secret column)
SECRETS = (
    ("jira_credentials", "company_id", "api_token"),
    ("ai_connections", "company_id", "credential_secret"),
    ("jde_profiles", "company_id", "credential_secret"),
    ("jde_execution_settings", "company_id", "credential_secret"),
)


def reencrypt_all() -> dict[str, int]:
    if not credential_crypto.is_configured():
        return {}
    rewritten: dict[str, int] = {}
    with connection(immediate=True) as conn:
        for table, key_col, secret_col in SECRETS:
            rows = conn.execute(
                f"SELECT {key_col} AS k, {secret_col} AS s FROM {table} WHERE {secret_col} IS NOT NULL")  # noqa: S608
            for row in rows.fetchall():
                if not row["s"] or not credential_crypto.needs_reencryption(row["s"]):
                    continue
                try:
                    plaintext = credential_crypto.decrypt(row["s"])
                except credential_crypto.CredentialUnreadable:
                    continue
                conn.execute(f"UPDATE {table} SET {secret_col} = ? WHERE {key_col} = ?",  # noqa: S608
                             (credential_crypto.encrypt(plaintext), row["k"]))
                rewritten[table] = rewritten.get(table, 0) + 1
    return rewritten
