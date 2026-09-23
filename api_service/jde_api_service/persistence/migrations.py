"""
Versioned schema migrations for the SQLite database (persistence/db.py
applies these). Append-only: a migration that has already shipped is
never edited, only superseded by a new, higher-numbered one -- the same
"never rewrite recorded history" principle this codebase already
applies to ApprovalRecord/DomainReview history. This is what lets the
schema evolve without losing data across restarts and redeploys.

Companies, Jira connections, users, company memberships, roles, domain
assignments, sessions, invitations, password resets and the access
audit log all live here -- see db.py's own docstring for why SQLite
(not JsonFileStore) for this specific data, and why no ORM.
"""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE companies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            short_name TEXT NOT NULL DEFAULT '',
            tools_release TEXT NOT NULL DEFAULT '',
            environment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        -- One row per company -- see models/jira_integration.py for why
        -- this is company-scoped and why the token is never returned by
        -- any endpoint. Replaces the old JsonFileStore-backed
        -- jira_integrations/ directory with a durable table. company_id
        -- is deliberately NOT a foreign key into companies -- like the
        -- JsonFileStore version it replaces, this table accepts any
        -- customer_id a caller is scoped to; real company existence is
        -- enforced earlier, by require_customer_access, not here.
        CREATE TABLE jira_integrations (
            company_id TEXT PRIMARY KEY,
            base_url TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            pickup_status TEXT NOT NULL DEFAULT '',
            post_pickup_status TEXT NOT NULL DEFAULT '',
            jade_id_field TEXT NOT NULL DEFAULT '',
            request_type_field TEXT NOT NULL DEFAULT '',
            updated_at TEXT,
            updated_by TEXT
        );

        -- Replaces the old JsonFileStore-backed jira_credentials/
        -- directory. Still pilot-scoped, plaintext-in-this-database
        -- storage (see models/jira_integration.py's own docstring) --
        -- not a secrets manager -- but now durable and never returned
        -- by any API response.
        CREATE TABLE jira_credentials (
            company_id TEXT PRIMARY KEY,
            email TEXT NOT NULL DEFAULT '',
            api_token TEXT NOT NULL DEFAULT '',
            updated_at TEXT,
            updated_by TEXT
        );

        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        -- A user's membership in one company. Creating a login alone
        -- (a users row) grants nothing -- only a row here, with status
        -- 'active', does. Deactivating one membership never touches any
        -- other row for the same user.
        CREATE TABLE company_memberships (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            company_id TEXT NOT NULL REFERENCES companies(id),
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            created_by TEXT,
            UNIQUE(user_id, company_id)
        );

        -- Multiple roles per membership -- a user can be both Domain
        -- Owner and Admin on the same company.
        CREATE TABLE membership_roles (
            membership_id TEXT NOT NULL REFERENCES company_memberships(id),
            role TEXT NOT NULL,
            PRIMARY KEY (membership_id, role)
        );

        -- Only meaningful for a membership that also holds the
        -- domain_owner role -- enforced in code (membership_service.py),
        -- not the schema, same as every other business rule here.
        CREATE TABLE domain_assignments (
            membership_id TEXT NOT NULL REFERENCES company_memberships(id),
            business_domain_id TEXT NOT NULL,
            PRIMARY KEY (membership_id, business_domain_id)
        );

        -- id is a hash of the raw session token, never the token
        -- itself -- same principle as password_hash below: a leaked
        -- database row is not a usable credential.
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        );

        CREATE TABLE invitations (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL REFERENCES companies(id),
            email TEXT NOT NULL COLLATE NOCASE,
            roles TEXT NOT NULL,
            domain_ids TEXT NOT NULL DEFAULT '[]',
            token_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            invited_by TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            accepted_at TEXT
        );

        CREATE TABLE password_reset_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            token_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        );

        -- "Record invitations and access changes with the acting user
        -- and timestamp" -- one append-only row per change, never
        -- edited or deleted.
        CREATE TABLE access_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT,
            actor_user_id TEXT,
            action TEXT NOT NULL,
            target_user_id TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX idx_sessions_user ON sessions(user_id);
        CREATE INDEX idx_memberships_company ON company_memberships(company_id);
        CREATE INDEX idx_memberships_user ON company_memberships(user_id);
        CREATE INDEX idx_invitations_company ON invitations(company_id);
        CREATE INDEX idx_invitations_email ON invitations(email);
        CREATE INDEX idx_password_resets_user ON password_reset_tokens(user_id);
        """,
    ),
    (
        2,
        """
        -- Optimistic concurrency (persistence/revisions.py): existing
        -- rows start at revision 1.
        ALTER TABLE jira_integrations ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE jira_credentials ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;

        -- Small company-level settings (dashboard thresholds, approval
        -- policy, ...), each a JSON value under a fixed key, revisioned
        -- and attributed to the authenticated user who saved it.
        CREATE TABLE company_settings (
            company_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            PRIMARY KEY (company_id, key)
        );
        """,
    ),
]
