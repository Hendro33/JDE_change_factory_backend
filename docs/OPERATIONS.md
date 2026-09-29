# Operating a hosted deployment

This covers running Jade as a hosted, multi-customer service. The Azure set-up itself is in `AZURE_DEPLOYMENT.md`;
running on a Mac is in `PREVIEW.md`.

## What's durable, and where

| Data | Single server | Azure |
|---|---|---|
| Users, customers, memberships and roles, sessions, settings, connections (credentials encrypted), stories, approvals, delivery records, evidence, runs | SQLite in `JDE_API_DATA_DIR` | PostgreSQL (`JDE_DATABASE_URL`) |
| Uploaded files: source exports, reference documents, process-framework workbooks, request attachments | `JDE_API_DATA_DIR` | Blob Storage (`JDE_BLOB_CONTAINER_URL`) |
| Technical Agent working copies during a run | `JDE_API_DATA_DIR/technical_workspaces` | per-instance scratch (`/data`) |

A restart or redeploy loses nothing: settings, credentials, connection readiness, sessions and stories are all in the
database.

## Creating the first Admin

Registration is invite-only, so the first Admin comes from the server's settings (`services/bootstrap_service.py`):

- `JDE_BOOTSTRAP_ADMIN_EMAIL` and `JDE_BOOTSTRAP_ADMIN_PASSWORD` (Key Vault) create a temporary setup account;
- `JDE_BOOTSTRAP_CUSTOMER_NAME` names the first customer, created on the first start only.

Sign in as the setup account and use **Finish setup** to create your own administrator account. The setup account is
then switched off and never re-enabled by a restart. Remove `JDE_BOOTSTRAP_ADMIN_PASSWORD` from the settings afterwards.

## Backup and restore

**On PostgreSQL (Azure)** use the database service's automated backups and point-in-time restore, plus Blob Storage soft delete / versioning; `scripts/jade_backup.py` refuses to run there. The rest of this section is for the **single-server SQLite** installation: one script that takes a **consistent** backup of SQLite and all JSON records together, and restores it.

### What "consistent" means here

Jade keeps state in two places: SQLite (`api_data/jde.sqlite3`: users, sessions, memberships and roles, domain assignments, Jira settings and encrypted tokens, company settings, sign-in failures) and JSON files (`api_data/*` records such as company scopes, story links and domain reviews, plus `backlog/`, `changes/` with exact-change approvals and their execution attempts, and `evidence/`). A backup must capture both at the same moment, or a restore could pair an approval with a membership that did not exist when it was given.

`scripts/jade_backup.py backup` therefore:

1. **Pauses writes** by creating the flag file `JDE_WRITE_PAUSE_FILE` (default `<JDE_API_DATA_DIR>/WRITE_PAUSED`). While it exists, the API answers every POST/PUT/PATCH/DELETE with **503 + Retry-After** ("nothing was changed"). Reads keep working, and the execution gate refuses to start any JDE write or test attempt.
2. Waits `--settle-seconds` (default 2) for requests already in flight.
3. **Refuses** (exit 2, no archive) if an agent run or a JDE attempt is in progress, because those write outside a request.
4. Copies SQLite with its online-backup API, never a raw file copy, and copies every JSON location.
5. Writes `manifest.json`, which holds:
   - a sha256 for every file;
   - table row counts;
   - a summary of the security-relevant state: memberships with roles and revisions, every exact change with status, approver id, expiry, and write and test state, scope revisions, and the validity of each evidence chain;
   - the **ids** of the credential keys the stored tokens need. The key itself is never included.
6. Removes the pause flag, even if the backup failed.

The pause usually lasts a few seconds. A browser that saves during it gets a readable 503 and can simply retry.

### Taking a backup

On the server, with the service's environment:

```bash
python3 scripts/jade_backup.py backup --out /tmp/jade-$(date +%Y%m%d-%H%M).tar.gz
python3 scripts/jade_backup.py verify --archive /tmp/jade-YYYYMMDD-HHMM.tar.gz
```

The archive contains password and session hashes, and Jira tokens encrypted under `JDE_CREDENTIAL_KEY`. Encrypt it before it leaves the server (for example `age -r <recipient> -o jade.tar.gz.age jade.tar.gz`), keep it in durable storage elsewhere, and delete it from `/tmp`. A disk snapshot is not paused, so it is not guaranteed to be consistent across SQLite and JSON.

### Restoring

1. Copy the archive onto the server.
2. Make sure `JDE_CREDENTIAL_KEY` (or `JDE_CREDENTIAL_KEY_PREVIOUS`) holds the key the archive needs. `verify` prints `credential_key_ids_needed` and whether the current environment can read them. See "Recovering the matching credential key" below.
3. Run:
   ```bash
   python3 scripts/jade_backup.py restore --archive /tmp/jade-YYYYMMDD-HHMM.tar.gz --replace-existing
   ```
   The restore runs these steps in order:
   - It verifies every checksum before touching anything. A damaged or altered archive is refused.
   - It pauses writes.
   - It moves the current data aside to `<dir>.pre-restore-<timestamp>`. Nothing is deleted.
   - It restores all locations and runs SQLite's `integrity_check`.
   - It re-summarises the restored state and compares it with the manifest.

   It prints a report with `matches_backup`, `sqlite_integrity` and `credentials_readable`, and exits non-zero unless the integrity check and the state comparison both pass. Without `--replace-existing` it only restores into empty locations.
4. **Restart the service**, so nothing keeps pre-restore state in memory. On start, schema migrations run (they are idempotent), and any JDE attempt that was in flight at backup time is marked *unknown*. Backups refuse to run while an attempt is in flight, so there should be none.
5. When you are satisfied, delete the `*.pre-restore-*` directories.

`tests/test_backup_restore.py` exercises all of this end to end:
- backup, followed by further changes, followed by restore;
- after a restart, the restored state is checked: memberships and revisions, pending, approved, applied and unknown changes, whether a restored approval is usable, the Jira token and the evidence chains;
- the 503 and refused dispatch during a backup, and no backup while an attempt is in flight;
- a tampered archive is refused;
- a key mismatch is reported, then fixed by supplying the old key.

### Recovering the matching credential key

The key is deliberately **not** in the backup: whoever holds the archive alone cannot read the Jira tokens. Recover it separately:

1. When a key is created or rotated, store it in the team password manager as `Jade JDE_CREDENTIAL_KEY <key id>`. The key id is the first 8 hex characters of its SHA-256; print it in the service shell with `python3 -c "import sys; sys.path[:0]=['api_service']; from jde_api_service.services import credential_crypto; print(credential_crypto.current_key_id())"`.
2. `verify` or `restore` reports `credential_key_ids_needed`. Look up the entry with that id in the password manager.
3. Set that key as `JDE_CREDENTIAL_KEY`. If the service should keep its newer key, set it as `JDE_CREDENTIAL_KEY_PREVIOUS` instead, and the next start re-encrypts the tokens under the current key. Then restart.
4. If the key cannot be recovered, only the stored credentials are lost (AI keys, JDE passwords, Jira tokens). The screens show them as unreadable and the connections refuse to run. Each customer's Admin re-enters them.

Keep every retired key in the password manager until no retained backup still lists its id.

## JDE discovery for the Architect

Each customer has one **JD Edwards connection**, set up under Administration › Systems & Connections › JDE. It is used for the Architect's approved, read-only discovery reads, for reading the before-value and the applied value of an approved change, and for running approved test Orchestrations. Jade never writes to JD Edwards: changes are applied in DEV by a person and recorded.

**Where things are stored:**
- **Profile metadata:** in `jde_profiles`, with every saved revision kept in `jde_profile_revisions`.
- **The credential:** encrypted with `JDE_CREDENTIAL_KEY`.
- **Observations, sanitised activity, artifact metadata and design evidence baselines:** in SQLite.
- **Artifact bytes:** in the blob store (local folder or Azure Blob Storage).
- **Design hand-offs:** in the database.

**Connection settings and operator overrides.** A company Admin configures the live connection in the app: the AIS
address (the one permitted destination), the AIS certificate (uploaded, stored in `jde_ca_certificates`, trusted only
for that connection), and the credential (encrypted, bound to that address and certificate). Certificate and
host-name/IP verification are never switched off. The server needs no settings; these optional overrides remain:

| Variable | Meaning |
|---|---|
| `JDE_DISCOVERY_LIVE_ENABLED` | `false` locks live discovery off for every company. |
| `JDE_DISCOVERY_ALLOWED_HOSTS` | If set, only these AIS hosts may be used, whatever a company saves. |
| `JDE_DISCOVERY_CA_BUNDLE` | Fallback CA file for connections without an uploaded certificate. If it is unusable, those connections stay off. |

**Readiness.** A live profile can be enabled only when every required item in five separately shown groups is satisfied:
- **Connectivity:** live access not locked, TLS trust usable, the saved address permitted, endpoint reached over verified TLS.
- **Identity:** signed in, and the session's environment, role, application release and Tools / server release captured. Environment names are compared exactly and never aliased (`JPS920` is not `PS920`). A `*ALL` role proves only that sign-in works. The path code comes only from JDE's F00941 answer, never from the environment name.
- **JDE authorisation:** a dedicated user and non-`*ALL` role, verified independently with a linked evidence document, and the approved sample read succeeding within its bounds. The customer's JDE permissions are the primary boundary; Jade's approved reads are an extra restriction.
- **Network restriction:** AIS accepts only the backend's source address, with evidence.
- **Jade runtime safeguards:** no JDE write path, approved reads defined, window open, credential encrypted, not disabled.

Passing TLS, sign-in or the attestations alone never makes a connection ready. Sample reads are built on the server from the approved read; the browser can preview the exact request (method, URL, body, sha256) but never supplies an endpoint or query.

**The live transport itself:**
- verified TLS;
- no redirects;
- the profile's timeout;
- a circuit breaker after 3 consecutive failures, open for 5 minutes;
- one request at a time per company;
- at most 10 records, no paging, no retries.

The only paths it can call are the token request, logout, `defaultconfig`, `dataservice` (BROWSE only), `poservice` and, for an approved test only, `orchestrator/<approved name>`.

**Environment verification.** Test Connection verifies the environment against the documented AIS contract, and keeps four sources of information apart:

| Source | What it is | Used as evidence? |
|---|---|---|
| Expected | What the Admin saved in the profile | It is what is being checked |
| Server defaults | `GET /jderest/defaultconfig`: `defaultEnvironment`, `defaultRole`, `defaultJasServer`, `aisVersion` | **Never.** These are the server's defaults, not the environment a session runs in. A difference from the expected environment is shown as a note only |
| Session context | The v2 token response for this credential and the requested environment and role: `environment`, `role`, `userInfo.appsRelease` | Yes: session environment and role must equal the expected values; the application release must match |
| Attested | What the AIS contract does not report: the Tools release and the path code (CNC runtime attestation), and OCM data-source routing and isolation (CNC isolation evidence) | Recorded as customer attestation, never as verified |

Each item is `verified`, `attested`, `missing` or `mismatch`:
- Any `mismatch` fails the check.
- Any `missing` item leaves it `unknown`, and discovery cannot be enabled. The check names the missing evidence.

For example, if the token response does not report the environment, the connection stays blocked until it does. The check is never relaxed to accept `defaultconfig` instead.

Contract sources (docs.oracle.com could not be fetched from the build environment; these pages were consulted through search results):
- [v2 token request](https://docs.oracle.com/en/applications/jd-edwards/cross-product/9.2/rest-api/op-v2-tokenrequest-post.html)
- [defaultconfig](https://docs.oracle.com/en/applications/jd-edwards/cross-product/9.2/rest-api/op-defaultconfig-get.html)
- [AIS client DefaultConfig](https://docs.oracle.com/en/applications/jd-edwards/cross-product/9.2/ais-client-api-reference/com/oracle/e1/aisclient/DefaultConfig.html)

**Before a customer's first live connection:**
1. **Customer/CNC:** a dedicated JDE user and a role that can read only the approved tables and applications in the DEV environment. Jade's read-only design does not make an over-privileged account safe.
2. **Customer/CNC:** a network route that reaches only the DEV AIS server, and written confirmation that OCM maps the environment to the DEV data source only. Record this in the profile.
3. **CNC:** a written statement of the Tools release and path code the DEV environment runs on (the runtime attestation). AIS does not report them.
4. **Operator (optional):** set `JDE_DISCOVERY_ALLOWED_HOSTS` to the customers' AIS hosts.
5. **Admin:** save the live profile with a short window and a minimal approved-read list. Enter the credential.
6. **With the customer present:**
   - run Test Connection;
   - run one approved sample read per capability;
   - compare the results, including the session context the token response reports, with what the customer sees in JDE.
7. **Admin:** enable discovery only after that comparison. Disable the connection when the window ends.

**Disable Connection** blocks new and queued calls at once. It reports any request already in flight, which finishes; nothing is interrupted mid-request. Re-enabling needs a fresh Test Connection and fresh sample reads.

**Data sharing.** The profile's policy decides what reaches the external model:

| Policy | What the model sees |
|---|---|
| `metadata_only` (default) | Values and artifact content are redacted |
| `configuration_and_artifacts` | Configuration values and artifact text; business data stays redacted |
| `full` | Everything |

Choose `full` only with the customer's written agreement.

**Evidence limits.**
- **Artifacts:** only the first 60,000 characters of a text artifact are analysed. The artifact record, the manifest and the Architect's tool result all state the coverage, and a citation of a truncated artifact carries the limitation.
- **Observations:** each one stores its full-result SHA-256 as a change detector only. Redacted values are not retained, so the hash is not proof of what the model did not see.
- **Refresh Evidence:** it records new observations and flags the design for reassessment. It never regenerates or re-approves a design.

**Agent runtime.** Every agent run:
- is restricted to `Task` as its only built-in tool (`services/agent_runtime.py`);
- has the credential encryption keys, the database and blob-storage secrets, the SMTP password and the bootstrap password blanked in the agent process. The project tools run in-process, bound to the run's story (`ai/project_tools.py`); no settings or `.mcp.json` are loaded. Agents have no way to authenticate to AIS or change JD Edwards;
- has every project MCP tool it is not allowed removed from its context.

**Design hand-off.** The hand-off file names the exact change its design revision proposed. `get_design_baseline` returns that change with its current approval state.

## Recorded delivery

**Functional changes** (configuration change sets, `mcp_server/jde_mcp_server/config_items.py`). The Functional
Agent proposes an ordered set of items under the eight configuration capabilities: UDC values, set-up and constants
tables, document types (F40039), line types (F40205), order activity rules (F40203), processing options, batch version
data selection and sequencing. Every item is checked against the universal rules (no deletes, no XJDE/ZJDE versions,
never the UDC hard-coded flag, no protected categories) and the customer's approved configuration
(`functional_agent.approved_configuration`: target, fields, add/update, allowed values; set in Governance) when it
is proposed, when it is approved (including add-versus-exists, read live) and at every delivery step. For the
customer's approved reads, add key filters (for example `udc_values` 00/DT filtered by DRKY, `table_browse` F40039
filtered by DCTO) so Jade can read each item back. After the change set is approved:
- The Application Manager applies the items in DEV, in order, and records each (`POST /changes/{id}/delivery/applied`
  with `itemId`). Jade re-checks the approval, scope and authority, and reads the item back live. Anything other than
  the approved values is refused, never recorded. When the connection cannot read an item, the person states the
  values (`statedValues`) or confirms a batch version's data selection (`confirmedAsSpecified`) with an evidence
  reference, and every record says it was stated, not read. The change counts as applied when every item is recorded.
- The approved test Orchestration runs live (`.../delivery/run-test`), or the person records the result with
  evidence (`.../delivery/test-result`). A call whose outcome is unknown (for example a timeout) must be reconciled
  before anything else happens.

**Technical changes** (business functions, event rules). The Technical Agent (`.claude/agents/technical-agent.md`)
only prepares an immutable package revision from the customer's uploaded source exports. After a person approves it,
people apply it through OMW, build it, have the CNC activate it, and verify it, and each step is recorded in Jade
(`.../technical/packages/{rev}/apply|build|verify`, CNC activation by a `cnc_operator`). The CNC role is never granted
by bootstrap: an Admin assigns it to a named person.

The engagement scope must authorise the object types (`technical_agent.authorized_object_types`). Objects must carry
a customer system code 55–59, and `reserved_product_code` narrows that further if set.

## Credential encryption key

AI keys, JDE passwords and Jira API tokens are stored encrypted in the database (`services/credential_crypto.py`). The key is **never** stored with the data:

- It comes only from `JDE_CREDENTIAL_KEY` in the service's environment (on Azure: a Key Vault reference).
- Keep a second copy in the team password manager.

Generate a key on your own machine, and paste it only into those two places:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

- **Without the key**, saving a credential is refused, so nothing is ever stored in plaintext. The Integrations screen shows that encryption is unavailable.
- **Backups** (above) contain only ciphertext. A restore needs the key that was current when the backup was taken. Keep old keys in the password manager until no backup still needs them.
- **Lost key:** only the stored credentials are lost. Set a new key; each customer's Admin then re-enters them. Until then they show as "unreadable" and the connections refuse to run.
- **Rotation:**
  1. Set the new key as `JDE_CREDENTIAL_KEY`, and the old one as `JDE_CREDENTIAL_KEY_PREVIOUS`.
  2. Redeploy. On start, every token is re-encrypted under the new key.
  3. Remove `JDE_CREDENTIAL_KEY_PREVIOUS` and redeploy again.
- **Tokens saved before encryption existed** are reported as "plaintext (legacy)". They are encrypted automatically on the first start with a key set.

## Sign-in rate limiting

Failed sign-ins are counted in SQLite (`services/login_throttle.py`), so the limits survive restarts:

- **Per account:** after 5 failures in 15 minutes, that account is refused, even with the right password, until the window passes.
- **Per client address:** after 20 failures in 15 minutes, all sign-ins from that address are refused.

Refusals return HTTP 429 with `Retry-After`. Behind Azure's front end (or any proxy), set `JDE_TRUST_PROXY_HEADERS=true` so the real client address is used. Leave it unset anywhere the `X-Forwarded-For` header is not overwritten by a trusted proxy.

To unlock an account early (for example, a user who mistyped repeatedly), delete its rows from the service shell:

```sql
DELETE FROM login_failures WHERE scope = 'account' AND key = 'user@example.com';   -- in the SQLite or PostgreSQL database
```

## E-mail: invitations and password resets

With `JDE_SMTP_HOST` set (Azure Communication Services Email, Microsoft 365, SendGrid or any SMTP submission service;
see `services/email_service.py`), invitations and password resets are e-mailed, with links built from
`JDE_PUBLIC_URL`. Every screen says truthfully what happened:

- **E-mailed:** the Admin sees "e-mailed to …" and no link.
- **Not configured or failed:** the Admin sees why, and the link to hand over personally.
- **Forgot password:** the form never reveals whether an address exists. Without a mail server it tells the person to
  ask their Administrator.
- **Limit on Admins:** an Admin cannot create a reset link for someone who also belongs to a customer where that Admin
  is not an Admin.

## HTTPS, CORS, and cookies

- **HTTPS**: terminated by the Azure front end (Container Apps / App Service) for the default and custom domains.
- **CORS**: `JDE_API_ALLOWED_ORIGINS` must be the frontend's exact origin (scheme and host, no trailing slash, no
  wildcard), e.g. `https://jade.consultiq.nl`. Credentials are allowed, so a wildcard can never work.
- **Cookies**: `JDE_COOKIE_SECURE=true`. Serve the API under the same registrable domain as the frontend (e.g.
  `api.jade.consultiq.nl`) with `JDE_COOKIE_DOMAIN` set and `JDE_COOKIE_SAMESITE=lax`. If the API is on a different
  site, `JDE_COOKIE_SAMESITE=none` is needed instead.
- **CSRF**: a double-submit cookie (`jde_csrf`), checked against the `X-CSRF-Token` header on every state-changing
  request. No configuration needed.

## Process frameworks, process maps and as-built records

- **Frameworks** (Admin › Process Framework, admin only): an `.xlsx` import (template at `GET /process/template`) goes
  upload → column mapping → validation and preview (a draft) → activation. Everything lives in SQLite (migration 7):
  - `process_frameworks`;
  - `framework_versions`: original file checksum and storage key, mapping, validation, and the changes against the previous version;
  - `framework_nodes`: every version's nodes, each with a checksum;
  - `process_settings`: the framework selected for the company.

  An activated version is immutable. Superseded versions stay resolvable. The original file is kept in the artifact
  store. Jade supplies no APQC content: `apqc_authorised` requires a statement of authority to use it.
  `fixtures/process_framework/` contains **SYNTHETIC** workbooks only (`scripts/make_process_fixtures.py`).
- **Story finalisation**:
  - The refinement process analysis (`POST /changes/{id}/process/analysis`) runs the real agent runtime. It has only
    the run-bound `jade-process` tools.
  - Its suggestions are validated against the exact framework version.
  - A Product Manager, or the Domain Owner assigned to the story's domain, confirms the mapping or records why no
    mapping applies.
  - Decisions are append-only revisions with pinned `(framework, version, node, node checksum)` references.
- **Architect**:
  - `get_process_context` (jade-discovery) returns this company's framework, the confirmed mapping and the maps.
  - The design baseline records the process fingerprint, whether the Architect consulted it, and the Architect's
    process findings.
- **Reassessment**: these changes flag the story's current design:
  - a new mapping revision;
  - a materially different map version (a title- or note-only edit is not material);
  - a framework version that changes or removes a mapped node;
  - Refresh Evidence after a process change.

  References are never rewritten.
- **As-built** (`/changes/{id}/as-built`):
  - Each generation is a new version, with its Markdown stored.
  - It is finalised only when every checkpoint is complete **and** its sources are unchanged since generation.
  - Checkpoints are: story approved, processes decided, to-be map, design approved and not flagged, exact approval,
    applied, built, CNC activation, verified (or, for a functional change: applied, test passed, applied value is
    the approved value).
  - Delivery is recorded: the record says for each step whether Jade read it live or a person stated it.
  - Without an active process framework, the process checkpoints are not applicable and the record says so.

## Story refinement from findings

- Findings: missing requirements, controls and acceptance criteria from the refinement process analysis and from
  the Architect's `process_findings`. Each is registered in `story_findings` with its own status
  (proposed / applied / rejected / deferred, plus reason and decider). Agents never change a story.
- A reviewer (a Product Manager, or the Domain Owner assigned to the story's domain):
  - previews the change as a diff (`/changes/{id}/process/refinement/preview`);
  - applies selected findings (`.../apply`, compare-and-set on the story revision).
- Applying writes the following:
  - a `story_revisions` row, attributed to the reviewer and linked to the findings and to the exact process
    references in force. Revision 1 is the story as approved.
  - the backlog record's text, which the Architect reads, with the previous text kept in `story_revisions`.
- A finding is applied at most once.
- Applying one flags the current design (`story_revised`) and invalidates pending or approved work for the story.

## Local preview

`scripts/run_local_preview.sh` runs the same code on one machine, with SQLite and a local folder for files, and no
data pre-loaded. See `PREVIEW.md`.

## Deployment trust controls

Customer settings (the JD Edwards connection, AI key, Jira) are edited in the app. These are set on the server only,
never from a browser:

| Setting | Purpose |
|---|---|
| `JDE_DISCOVERY_LIVE_ENABLED=false` | Locks every JD Edwards connection off (emergency stop) |
| `JDE_DISCOVERY_ALLOWED_HOSTS` | The only AIS hosts the backend may contact |
| `JDE_DISCOVERY_CA_BUNDLE` | Optional PEM file for a private CA. Certificate verification is never switched off |
| `JDE_ANTHROPIC_BASE_URL` | Optional HTTPS gateway in front of the Anthropic API |
| `JDE_CREDENTIAL_KEY` | The credential-encryption key |

The JDE panel shows each server-managed prerequisite and a plain diagnostic when one blocks a connection (TLS trust,
DNS, connect timeout, VPN hints). Diagnostics never include credentials, tokens or response bodies.
