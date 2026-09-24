# Operating a hosted deployment

This covers what's specific to running `api_service` as a real, hosted
backend (currently: Render, via `render.yaml` at the repo root) — not
local development, which the main `README.md` / `GETTING_STARTED.md`
already cover.

## What's durable, and where

Everything this service and `mcp_server` write lives under one mounted
disk (`/data` in `render.yaml`):

| Env var | Contents |
|---|---|
| `JDE_API_DATA_DIR` | SQLite database (`jde.sqlite3`: users, companies, roles, memberships, invitations, sessions, Jira connections) + this service's own JSON collections (change requests, business domains, engagement scope, ...) |
| `JDE_BACKLOG_DIR` | mcp_server's backlog records (Gate 2) |
| `JDE_CHANGE_DIR` | mcp_server's approval records |
| `JDE_EVIDENCE_DIR` | mcp_server's evidence files |

A restart or redeploy that keeps the same disk attached loses nothing —
this has been verified locally (stop/restart a real `uvicorn` process
against the same directory: saved Jira settings, user accounts, and
even active login sessions all survived) and should be re-verified the
same way against the actual hosted disk once it exists.

## Creating the first Admin

There is no public admin-registration endpoint anywhere in this
service — registration is invite-only, and only an existing Admin can
invite someone. `services/bootstrap_service.py` is the one deliberate
way around that circularity, and it is controlled entirely by two
server-side environment variables:

- `JDE_BOOTSTRAP_ADMIN_EMAIL`
- `JDE_BOOTSTRAP_ADMIN_PASSWORD`

**Set these directly in the hosting platform's dashboard (Render:
Environment tab), never anywhere else.** The password must never be
typed into chat, a commit, or any file in this repo — this is why the
Blueprint declares both with `sync: false`: Render will ask you to
fill them in yourself when you deploy, and never stores or displays
them back to anyone but you.

Once you've confirmed you can log in as that Admin on the live site:

1. Go back to the Environment tab.
2. Blank out (or delete) `JDE_BOOTSTRAP_ADMIN_PASSWORD`.
3. Redeploy (Render redeploys automatically on an env var change).

`ensure_bootstrap_admin()` is idempotent and already skips itself once
that email is registered, so this isn't strictly required for
correctness — but removing the password afterward means it can never
be read back out of the dashboard or reused if the account is ever
deleted.

## Backup and restore

Proportionate to a single-process pilot: no automated job, one script that takes a **consistent** backup of SQLite and all JSON records together, and restores it.

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

In Render's shell (Dashboard → service → **Shell**), which has the service's environment:

```bash
python3 scripts/jade_backup.py backup --out /tmp/jade-$(date +%Y%m%d-%H%M).tar.gz
python3 scripts/jade_backup.py verify --archive /tmp/jade-YYYYMMDD-HHMM.tar.gz
```

The archive contains password and session hashes, and Jira tokens encrypted under `JDE_CREDENTIAL_KEY`. Encrypt it before it leaves the platform (for example `age -r <recipient> -o jade.tar.gz.age jade.tar.gz`), download it to durable storage off the platform, and delete it from `/tmp`. Render's daily disk snapshot is a second layer, but it is not paused, so it is not guaranteed to be consistent across SQLite and JSON.

### Restoring

1. Upload the archive into the service (Render's shell file transfer, or `render ssh` piping it in).
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
4. If the key cannot be recovered, only the Jira tokens are lost. Integrations shows Jira as **Unavailable (cannot be decrypted)** and sync is refused. There is no fallback to simulated data. Each company's Admin re-enters its token.

Keep every retired key in the password manager until no retained backup still lists its id.

## JDE discovery for the Architect

Each company has one read-only **discovery profile**, set up under Admin → Integrations → JDE. It is separate from the execution gate's JDE settings (`JDE_AIS_*`, used only by `mcp_server`); the two never share credentials.

**Where things are stored:**
- **Profile metadata:** in `jde_profiles`, with every saved revision kept in `jde_profile_revisions`.
- **The credential:** encrypted with `JDE_CREDENTIAL_KEY`.
- **Observations, sanitised activity, artifact metadata and design evidence baselines:** in SQLite.
- **Artifact bytes:** under `<JDE_API_DATA_DIR>/artifacts/`.
- **Hand-off files for the Functional Agent:** under `<JDE_API_DATA_DIR>/design_baselines/`.

All of it is covered by the backup script.

**Deployment controls.** Both default to off, and both are needed before anything live is contacted:

| Variable | Meaning |
|---|---|
| `JDE_DISCOVERY_LIVE_ENABLED` | `true` allows profiles in *live* mode. Unset means only the labelled simulation works. |
| `JDE_DISCOVERY_ALLOWED_HOSTS` | Comma-separated AIS host names a live profile may use. Anything else is refused before a connection is opened. |

A live profile never falls back to the simulation, and the simulation never pretends to be live.

**The live transport itself:**
- verified TLS;
- no redirects;
- the profile's timeout;
- a circuit breaker after 3 consecutive failures, open for 5 minutes;
- one request at a time per company;
- at most 10 records, no paging, no retries.

The only paths it can call are the token request, logout, `defaultconfig`, `dataservice` (BROWSE only) and `poservice`.

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

**Before the first supervised live connection** (none has happened yet):
1. **Customer/CNC:** a dedicated JDE user and a role that can read only the approved tables and applications in the DEV environment. Jade's read-only design does not make an over-privileged account safe.
2. **Customer/CNC:** a network route that reaches only the DEV AIS server, and written confirmation that OCM maps the environment to the DEV data source only. Record this in the profile.
3. **CNC:** a written statement of the Tools release and path code the DEV environment runs on (the runtime attestation). AIS does not report them.
4. **Operator:** set `JDE_DISCOVERY_ALLOWED_HOSTS` to that host, and `JDE_DISCOVERY_LIVE_ENABLED=true`, for the supervised session only.
5. **Admin:** save the live profile with a short window and a minimal approved-read list. Enter the credential.
6. **With the customer present:**
   - run Test Connection;
   - run one approved sample read per capability;
   - compare the results, including the session context the token response reports, with what the customer sees in JDE. Response shapes are unverified until this is done (Experiment A1).
7. **Admin:** enable discovery only after that comparison. Disable the connection when the window ends, and remove `JDE_DISCOVERY_LIVE_ENABLED` again.

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
- has the credential encryption keys, the execution AIS credential (`JDE_AIS_USERNAME`, `JDE_AIS_PASSWORD`) and the bootstrap password blanked in the agent process, and so also in the project MCP server that process starts. Consequence: live execution through an agent run cannot authenticate to AIS. It fails closed. This must be revisited, deliberately, before any authorised live execution;
- has every project MCP tool it is not allowed removed from its context.

**Design hand-off.** The hand-off file names the exact change its design revision proposed. `get_design_baseline` returns that change with its current approval state.

**Integration proof.** `scripts/prove_architect_discovery.py` runs the real Architect, and then the existing functional-agent (non-executing), through the Claude CLI against the simulated endpoint. The last recorded run is in `docs/proof/architect_discovery_run/`. It needs a Claude login; it is not part of CI.

## Credential encryption key

Jira API tokens are stored encrypted in SQLite (`services/credential_crypto.py`). The key is **never** on the disk:

- It comes only from `JDE_CREDENTIAL_KEY` in the service's environment. Set it in the Render dashboard.
- Keep a second copy in the team password manager.

Generate a key on your own machine, and paste it only into those two places:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

- **Without the key**, saving a credential is refused, so nothing is ever stored in plaintext. The Integrations screen shows that encryption is unavailable.
- **Backups** (above) contain only ciphertext. A restore needs the key that was current when the backup was taken. Keep old keys in the password manager until no backup still needs them.
- **Lost key:** only the stored tokens are lost. Set a new key; each company's Admin then re-enters its Jira token. Until then Integrations shows the old ones as "unreadable" and Jira as Unavailable for that company; sync is refused (no fallback to simulated Jira).
- **Rotation:**
  1. Set the new key as `JDE_CREDENTIAL_KEY`, and the old one as `JDE_CREDENTIAL_KEY_PREVIOUS`.
  2. Redeploy. On start, every token is re-encrypted under the new key.
  3. Remove `JDE_CREDENTIAL_KEY_PREVIOUS` and redeploy again.
- **Tokens saved before encryption existed** are reported as "plaintext (legacy)". They are encrypted automatically on the first start with a key set.

## Sign-in rate limiting

Failed sign-ins are counted in SQLite (`services/login_throttle.py`), so the limits survive restarts:

- **Per account:** after 5 failures in 15 minutes, that account is refused, even with the right password, until the window passes.
- **Per client address:** after 20 failures in 15 minutes, all sign-ins from that address are refused.

Refusals return HTTP 429 with `Retry-After`. Behind Render's proxy, set `JDE_TRUST_PROXY_HEADERS=true` so the real client address is used. Leave it unset anywhere the `X-Forwarded-For` header is not overwritten by a trusted proxy.

To unlock an account early (for example, a user who mistyped repeatedly), delete its rows from the service shell:

```bash
python3 -c "import sqlite3; c=sqlite3.connect('/data/api_data/jde.sqlite3'); c.execute(\"DELETE FROM login_failures WHERE scope='account' AND key='user@example.com'\"); c.commit()"
```

## Password resets without an email provider

Until an email provider is configured, no email is sent.

- **Invitations:** the link is shown to the inviting Admin.
- **Password resets:** a company Admin creates the link under Admin > Users. The anonymous "forgot password" form never returns a link, because that would let anyone reset anyone's password.
- **Limit on Admins:** an Admin cannot create a reset link for someone who also belongs to a company where that Admin is not an Admin.

## HTTPS, CORS, and cookies

- **HTTPS**: automatic on Render for both its `*.onrender.com` URL and
  any custom domain you attach — no code or config here handles TLS
  termination directly.
- **CORS**: `JDE_API_ALLOWED_ORIGINS` must be the frontend's exact
  origin (scheme + host, no trailing slash, no wildcard) —
  `https://jade.consultiq.nl`. `allow_credentials=True` is already set
  in `main.py`, which is what lets the session cookie cross origins at
  all; browsers refuse `allow_credentials` combined with a wildcard
  origin, so this can never be `*`.
- **Cookies**: `JDE_COOKIE_SECURE=true` (cookies only sent over
  HTTPS), `JDE_COOKIE_SAMESITE=lax` when the API is on a
  `*.consultiq.nl` subdomain (recommended — see below), and
  `JDE_COOKIE_DOMAIN=.consultiq.nl` (or whatever the shared
  registrable domain is) so the same cookie is valid across both
  `api.consultiq.nl` and `jade.consultiq.nl`. If the API instead stays
  on a bare `*.onrender.com` URL (a genuinely different domain from
  the frontend), `JDE_COOKIE_SAMESITE` must be `none` instead, which
  browsers only allow when `Secure` is also set (already the default
  here).
- **CSRF**: independent of the above — a double-submit cookie
  (`jde_csrf`), checked against the `X-CSRF-Token` header on every
  state-changing request (`dependencies.verify_csrf_if_unsafe`). No
  extra configuration needed; it works the same regardless of the
  SameSite setting, as defence in depth.

## Prefer an API subdomain under consultiq.nl

Recommended over a bare `*.onrender.com` URL, for two reasons:
1. It lets `JDE_COOKIE_DOMAIN`/`JDE_COOKIE_SAMESITE=lax` treat the API
   and the frontend as the same site (safer than the cross-site
   `SameSite=None` case, and simpler to reason about).
2. It reads better and survives a future move off Render (Azure or
   elsewhere) without changing the URL the frontend, or anyone's
   bookmarks, point at.

To set it up once the service exists: in Render's dashboard, add a
Custom Domain (e.g. `api.consultiq.nl`) to the service; Render gives
you a CNAME target to add at wherever consultiq.nl's DNS is managed.
Render provisions HTTPS for it automatically once that CNAME resolves.
