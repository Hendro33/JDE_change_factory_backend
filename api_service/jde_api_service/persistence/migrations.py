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
    (
        3,
        """
        -- Failed sign-in attempts, for rate limiting (services/login_throttle.py).
        -- scope is 'account' (key = lower-cased email) or 'client' (key =
        -- client address). Old rows are pruned as new ones arrive.
        CREATE TABLE login_failures (
            scope TEXT NOT NULL,
            key TEXT NOT NULL,
            failed_at REAL NOT NULL
        );
        CREATE INDEX idx_login_failures ON login_failures(scope, key, failed_at);
        """,
    ),
    (
        4,
        """
        -- Optimistic concurrency for role, domain and status changes
        -- (membership_service.py): an Admin's edit is refused if the
        -- membership changed since they loaded it.
        ALTER TABLE company_memberships ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
        """,
    ),
    (
        5,
        """
        -- Architect Environment Discovery (discovery/). Company-specific,
        -- read-only JDE discovery profile: non-secret settings are JSON in
        -- config; the credential secret is encrypted (credential_crypto);
        -- every saved revision is kept in jde_profile_revisions.
        CREATE TABLE jde_profiles (
            company_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            config TEXT NOT NULL,
            material_hash TEXT NOT NULL,
            credential_username TEXT,
            credential_secret TEXT,
            credential_revision INTEGER NOT NULL DEFAULT 0,
            credential_updated_at TEXT,
            credential_updated_by TEXT,
            health TEXT NOT NULL DEFAULT '{}',
            capability_checks TEXT NOT NULL DEFAULT '{}',
            discovery_enabled INTEGER NOT NULL DEFAULT 0,
            enabled_material_hash TEXT,
            enabled_by TEXT,
            enabled_at TEXT,
            disabled INTEGER NOT NULL DEFAULT 0,
            disabled_by TEXT,
            disabled_at TEXT,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        CREATE TABLE jde_profile_revisions (
            company_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            config TEXT NOT NULL,
            material_hash TEXT NOT NULL,
            credential_revision INTEGER NOT NULL,
            saved_at TEXT NOT NULL,
            saved_by TEXT NOT NULL,
            PRIMARY KEY (company_id, revision)
        );
        -- Sanitised: operation, target shape, counts and outcome only --
        -- never credentials, tokens, filter values or business payloads.
        CREATE TABLE discovery_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            company_id TEXT NOT NULL,
            profile_revision INTEGER,
            actor_user_id TEXT,
            agent_run_id TEXT,
            story_id TEXT,
            operation TEXT NOT NULL,
            target TEXT NOT NULL,
            mode TEXT,
            started_at TEXT NOT NULL,
            duration_ms INTEGER,
            result_count INTEGER,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_discovery_activity_company ON discovery_activity(company_id, id);
        -- Immutable: a refresh adds a new row (refresh_of) instead of editing.
        CREATE TABLE discovery_observations (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            story_id TEXT,
            agent_run_id TEXT,
            actor_user_id TEXT,
            profile_revision INTEGER NOT NULL,
            capability_id TEXT NOT NULL,
            request TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            mode TEXT NOT NULL,
            sharing_policy TEXT NOT NULL,
            evidence TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            result_count INTEGER NOT NULL,
            refresh_of TEXT
        );
        CREATE INDEX idx_discovery_observations_story ON discovery_observations(company_id, story_id);
        -- Technical exports and reference documents: immutable revisions.
        CREATE TABLE technical_artifacts (
            artifact_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            company_id TEXT NOT NULL,
            domain_id TEXT,
            kind TEXT NOT NULL,
            meta TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            storage_key TEXT NOT NULL,
            extraction_status TEXT NOT NULL,
            extraction_note TEXT NOT NULL DEFAULT '',
            uploaded_by TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            PRIMARY KEY (artifact_id, revision)
        );
        CREATE INDEX idx_technical_artifacts_company ON technical_artifacts(company_id);
        -- One immutable evidence manifest per Architect design revision (and
        -- per refresh). Only status/reassessment change afterwards.
        CREATE TABLE design_baselines (
            baseline_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            story_id TEXT NOT NULL,
            design_revision INTEGER NOT NULL,
            baseline_revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            trigger TEXT NOT NULL,
            manifest TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            reassessment TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX idx_design_baselines_story ON design_baselines(company_id, story_id);
        """,
    ),
    (
        6,
        """
        -- A person's approval of one Architect design revision for technical
        -- implementation. Separate from (and a precondition for) the exact
        -- implementation approval of a package revision.
        CREATE TABLE design_approvals (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            story_id TEXT NOT NULL,
            design_revision INTEGER NOT NULL,
            baseline_id TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            approved_by TEXT NOT NULL,
            approver_user_id TEXT NOT NULL,
            roles TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            approved_at TEXT NOT NULL
        );
        CREATE INDEX idx_design_approvals_story ON design_approvals(company_id, story_id);
        -- Technical Agent runs: progress, failures, model usage, outcome.
        CREATE TABLE technical_runs (
            run_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            story_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            status TEXT NOT NULL,
            design_revision INTEGER,
            baseline_id TEXT,
            design_approval_id TEXT,
            expected_package_revision INTEGER NOT NULL DEFAULT 0,
            initiated_by TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error TEXT,
            outcome TEXT NOT NULL DEFAULT '{}',
            model TEXT,
            usage TEXT NOT NULL DEFAULT '{}',
            events TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX idx_technical_runs_story ON technical_runs(company_id, story_id);
        -- Implementation packages: immutable content per revision.
        CREATE TABLE technical_packages (
            package_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            company_id TEXT NOT NULL,
            story_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by_run TEXT,
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            change_id TEXT,
            superseded_by INTEGER,
            PRIMARY KEY (package_id, revision)
        );
        CREATE INDEX idx_technical_packages_story ON technical_packages(company_id, story_id);
        """,
    ),
    (
        7,
        """
        -- Process frameworks: a company's imported process hierarchy
        -- (authorised APQC content or its own). A version is immutable once
        -- activated; nodes of every version are kept so historical
        -- references stay resolvable after the framework changes.
        CREATE TABLE process_frameworks (
            framework_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            name TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_process_frameworks_name ON process_frameworks(company_id, name);
        CREATE TABLE framework_versions (
            framework_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            company_id TEXT NOT NULL,
            status TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_sha256 TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            storage_key TEXT NOT NULL,
            sheet_name TEXT NOT NULL,
            column_mapping TEXT NOT NULL,
            source_statement TEXT NOT NULL,
            validation TEXT NOT NULL,
            node_count INTEGER NOT NULL,
            content_sha256 TEXT NOT NULL,
            changes TEXT NOT NULL DEFAULT '{}',
            uploaded_by TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            activated_by TEXT,
            activated_at TEXT,
            superseded_at TEXT,
            PRIMARY KEY (framework_id, version)
        );
        CREATE TABLE framework_nodes (
            framework_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            node_key TEXT NOT NULL,
            parent_key TEXT,
            level INTEGER NOT NULL,
            position INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            node_type TEXT NOT NULL DEFAULT '',
            external_ref TEXT NOT NULL DEFAULT '',
            node_sha256 TEXT NOT NULL,
            PRIMARY KEY (framework_id, version, node_key)
        );
        -- Which framework a company works with (revisioned setting).
        CREATE TABLE process_settings (
            company_id TEXT PRIMARY KEY,
            selected_framework_id TEXT,
            revision INTEGER NOT NULL,
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        -- Agent suggestions for a story's processes (refinement analysis).
        CREATE TABLE process_analysis_runs (
            run_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            story_id TEXT NOT NULL,
            status TEXT NOT NULL,
            framework_id TEXT,
            framework_version INTEGER,
            initiated_by TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error TEXT,
            model TEXT,
            usage TEXT NOT NULL DEFAULT '{}',
            result TEXT NOT NULL DEFAULT '{}',
            scripted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_process_analysis_story ON process_analysis_runs(company_id, story_id);
        -- A reviewer's decision on a story's processes: append-only
        -- revisions, each with exact framework/version/node references.
        CREATE TABLE story_process_mappings (
            company_id TEXT NOT NULL,
            story_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            refs TEXT NOT NULL,
            no_mapping_reason TEXT NOT NULL DEFAULT '',
            findings TEXT NOT NULL DEFAULT '{}',
            analysis_run_id TEXT,
            reviewer_name TEXT NOT NULL,
            reviewer_user_id TEXT NOT NULL,
            roles TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (company_id, story_id, revision)
        );
        -- As-is / to-be process maps beside a story; immutable versions.
        CREATE TABLE process_maps (
            map_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            story_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_process_maps_story ON process_maps(company_id, story_id, kind);
        CREATE TABLE process_map_versions (
            map_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            company_id TEXT NOT NULL,
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            material_sha256 TEXT NOT NULL,
            material_change INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            created_by_user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (map_id, version)
        );
        -- As-built records: generated, versioned, finalised once.
        CREATE TABLE as_built_records (
            company_id TEXT NOT NULL,
            story_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            delivery_mode TEXT NOT NULL,
            inputs_sha256 TEXT NOT NULL,
            content TEXT NOT NULL,
            markdown TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            generated_by TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            finalised_by TEXT,
            finalised_at TEXT,
            PRIMARY KEY (company_id, story_id, version)
        );
        """,
    ),
    (
        8,
        """
        -- Findings from process analysis (refinement agent, Architect), each
        -- with its own review status. Applying one creates a story revision.
        CREATE TABLE story_findings (
            finding_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            story_id TEXT NOT NULL,
            source TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            decided_by TEXT,
            decided_by_user_id TEXT,
            decided_at TEXT,
            applied_in_revision INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_story_findings_story ON story_findings(company_id, story_id);
        -- Person-applied revisions of an approved story; revision 1 is the
        -- story as it was before the first applied change.
        CREATE TABLE story_revisions (
            company_id TEXT NOT NULL,
            story_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            source TEXT NOT NULL,
            user_story TEXT NOT NULL,
            story_sha256 TEXT NOT NULL,
            applied_findings TEXT NOT NULL DEFAULT '[]',
            process_refs TEXT NOT NULL DEFAULT '{}',
            author_name TEXT NOT NULL,
            author_user_id TEXT NOT NULL,
            roles TEXT NOT NULL DEFAULT '[]',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (company_id, story_id, revision)
        );
        """,
    ),
    (
        9,
        """
        -- Demo customers are labelled everywhere. Simulated JDE (discovery
        -- and execution) is only ever available inside a demo customer; a
        -- real customer gets live connections or nothing.
        ALTER TABLE companies ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE companies ADD COLUMN updated_at TEXT;
        ALTER TABLE companies ADD COLUMN updated_by TEXT;
        UPDATE companies SET is_demo = 1 WHERE id IN ('vdb', 'nhd', 'mrv', 'bwm');
        """,
    ),
    (
        10,
        """
        -- The AIS server certificate (or its CA) an Admin uploads for a
        -- company's JDE connection. It only ever ADDS trust for that one
        -- connection; verification is never switched off.
        CREATE TABLE jde_ca_certificates (
            company_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            pem TEXT NOT NULL,
            summary TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            PRIMARY KEY (company_id, sha256)
        );
        -- The address + certificate a saved JDE password was entered for; a
        -- different destination never receives it.
        ALTER TABLE jde_profiles ADD COLUMN credential_destination TEXT;
        """,
    ),
]
