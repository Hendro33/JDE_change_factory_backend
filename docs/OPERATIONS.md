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

## Backup

Proportionate to a prototype: no automated backup job, just a manual
copy you can run before anything risky (a schema change, a platform
migration) or on whatever cadence you're comfortable with.

Using Render's shell (Dashboard → the service → **Shell**), or `render
ssh <service>` from the CLI:

```bash
# Inside the service's shell -- /data is the mounted disk.
tar czf /tmp/jde-backup-$(date +%Y%m%d-%H%M).tar.gz -C /data .
```

The archive contains password and session hashes, and Jira tokens encrypted under the current `JDE_CREDENTIAL_KEY` (the key itself is never in it). Encrypt the archive before it leaves the platform, for example `age -r <recipient> -o backup.tar.gz.age backup.tar.gz`, and keep the credential key separately (see "Credential encryption key").

Then download `/tmp/jde-backup-*.tar.gz` via Render's shell file
transfer (or `render ssh <service> -- cat /tmp/jde-backup-*.tar.gz > local-backup.tar.gz` piped through the CLI) to somewhere durable off
the platform — your own machine, or object storage. Delete it from
`/tmp` afterward; it's a full copy of every company's data, including
Jira tokens.

## Restore

1. Provision the disk (a fresh Render service from this same
   `render.yaml`, or the existing one after clearing `/data`).
2. Upload the backup archive into the service (Render's shell file
   transfer, or `render ssh` piping it in).
3. From the service's shell:
   ```bash
   rm -rf /data/*        # only if restoring into a non-empty disk
   tar xzf jde-backup-YYYYMMDD-HHMM.tar.gz -C /data
   ```
4. Restart the service so it picks up the restored files. Schema
   migrations (`persistence/db.py`'s `ensure_schema()`) run
   automatically on startup and are safe to run again against an
   already-migrated database — they no-op past what's already applied.

## Credential encryption key

Jira API tokens are stored encrypted in SQLite (`services/credential_crypto.py`). The key is **never** on the disk:

- It comes only from `JDE_CREDENTIAL_KEY` in the service's environment. Set it in the Render dashboard.
- Keep a second copy in the team password manager.

Generate a key on your own machine, and paste it only into those two places:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

- **Without the key**, saving a credential is refused, so nothing is ever stored in plaintext. The Integrations screen shows that encryption is unavailable.
- **Backups** (below) contain only ciphertext. A restore needs the key that was current when the backup was taken. Keep old keys in the password manager until no backup still needs them.
- **Lost key:** only the stored tokens are lost. Set a new key; each company's Admin then re-enters its Jira token. Integrations shows the old ones as "unreadable", and Jade falls back to mock Jira for them.
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
sqlite3 /data/api_data/jde.sqlite3 "DELETE FROM login_failures WHERE scope='account' AND key='user@example.com';"
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
