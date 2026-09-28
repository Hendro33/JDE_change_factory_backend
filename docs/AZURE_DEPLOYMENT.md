# Deploying Jade on Azure

Jade runs the same code locally and on Azure. Only configuration changes: on Azure the database is Azure Database for PostgreSQL, uploaded files go to Azure Blob Storage, and secrets come from Key Vault. Every customer lives in the same installation, each with its own JD Edwards connection, AI key, Jira and users.

## What runs where

| Part | Azure service | Notes |
|---|---|---|
| Backend (API and the agents' tool server) | Azure Container Apps, or App Service for Containers | One image, built from `Dockerfile` in this repository. It is stateless, so you can run more than one instance. |
| Frontend | Azure Static Web Apps, or any static host | Build it with `VITE_API_BASE_URL` set to the backend's address. |
| Database | Azure Database for PostgreSQL – Flexible Server | Holds users, customers, settings, stories, approvals, evidence and runs. |
| Uploaded files | Azure Blob Storage, one private container | Holds JDE source exports, reference documents, process-framework workbooks and request attachments. |
| Secrets | Azure Key Vault | Referenced from the App Settings / Container Apps secrets. |
| E-mail | Azure Communication Services Email (SMTP), or Microsoft 365 / SendGrid SMTP | Used for invitations and password resets. |

Outbound traffic from the backend must reach:
- each customer's AIS server (a VPN, private endpoint or firewall rule per customer; see the JD Edwards connection's network-restriction evidence);
- `api.anthropic.com`, or your gateway set in `JDE_ANTHROPIC_BASE_URL`;
- each customer's Jira Cloud site;
- the SMTP host.

## Backend settings

Put secrets in Key Vault and reference them. Don't put them in plain App Settings.

| Setting | Value |
|---|---|
| `JDE_DATABASE_URL` | `postgresql://<user>:<password>@<server>.postgres.database.azure.com:5432/jade?sslmode=require` (Key Vault) |
| `JDE_DATABASE_SCHEMA` | Optional: a schema name, e.g. `jade` |
| `JDE_DATABASE_POOL_MAX` | Connections per instance (default 10). Keep instances × this below the server's connection limit. |
| `JDE_BLOB_CONTAINER_URL` | `https://<account>.blob.core.windows.net/<container>`. Grant the app's managed identity the **Storage Blob Data Contributor** role on the container. |
| `JDE_CREDENTIAL_KEY` | Fernet key that encrypts every stored credential: AI keys, JDE passwords, Jira tokens (Key Vault). Generate it with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `JDE_CREDENTIAL_KEY_PREVIOUS` | Only while rotating keys. At start-up, stored credentials are re-encrypted under the new key. |
| `JDE_API_ALLOWED_ORIGINS` | The frontend's address, e.g. `https://jade.consultiq.nl` |
| `JDE_PUBLIC_URL` | The same address. It is used for the links in e-mails. |
| `JDE_COOKIE_SECURE` | `true` |
| `JDE_COOKIE_SAMESITE` / `JDE_COOKIE_DOMAIN` | See Sign-in cookies below. |
| `JDE_TRUST_PROXY_HEADERS` | `true`, because Azure's front end forwards the client address. |
| `JDE_SMTP_HOST`, `JDE_SMTP_PORT`, `JDE_SMTP_USERNAME`, `JDE_SMTP_PASSWORD` (Key Vault), `JDE_MAIL_FROM` | E-mail. Leave `JDE_SMTP_HOST` empty and Administrators hand invitation links over personally. |
| `JDE_BOOTSTRAP_ADMIN_EMAIL`, `JDE_BOOTSTRAP_ADMIN_PASSWORD` (Key Vault), `JDE_BOOTSTRAP_CUSTOMER_NAME` | Needed for the first start only. See First start below. |
| `JDE_DISCOVERY_ALLOWED_HOSTS` | Optional. Limits which AIS hosts may be contacted at all. |
| `JDE_DISCOVERY_LIVE_ENABLED` | Optional. `false` locks every live JDE connection off (emergency stop). |
| `JDE_DISCOVERY_CA_BUNDLE` | Optional. A server-wide CA bundle. A certificate uploaded for a customer's connection takes precedence. |
| `JDE_ANTHROPIC_BASE_URL` | Optional. An HTTPS gateway in front of the Anthropic API. |

The image already sets `JDE_API_REPO_ROOT=/app` and `JDE_API_DATA_DIR=/data`. On Azure, `/data` holds only per-instance scratch space (the Technical Agent's working copies during a run). Everything that must last is in PostgreSQL and Blob Storage.

Each customer's own AI key, JDE connection and credential, and Jira connection are entered in the app by that customer's Administrator. They are stored encrypted in the database, never in server settings.

## Sign-in cookies

The session is an HttpOnly cookie. Pick one of these setups:

- **Same site** (recommended). Serve the frontend and the API under one parent domain, e.g. `jade.consultiq.nl` and `api.jade.consultiq.nl`. Set `JDE_COOKIE_DOMAIN=jade.consultiq.nl` and leave `JDE_COOKIE_SAMESITE=lax`.
- **Different sites.** Set `JDE_COOKIE_SAMESITE=none` and `JDE_COOKIE_SECURE=true`.

## First start

1. Create the PostgreSQL database and the blob container, and grant the managed identity access.
2. Deploy the backend with the settings above and the three bootstrap settings.
3. On first start, Jade:
   - creates its tables, applying migrations one instance at a time;
   - creates the temporary setup account;
   - creates one first customer named `JDE_BOOTSTRAP_CUSTOMER_NAME`.
4. Sign in as the setup account. Use **Finish setup** to create your own administrator account; the setup account is then switched off.
5. Remove `JDE_BOOTSTRAP_ADMIN_PASSWORD` from the settings.
6. In Administration, set up:
   - customers;
   - users and roles (Domain Owners, Application Managers, CNC operators);
   - each customer's AI connection;
   - Jira;
   - the JD Edwards connection: address, certificate, credential, then Test connection and Enable.

## Backups

On PostgreSQL, use the server's automated backups and point-in-time restore, plus Blob Storage soft delete / versioning. The `scripts/jade_backup.py` tool is for the single-server SQLite installation and refuses to run on PostgreSQL.

## Building the image

```bash
docker build -t <registry>.azurecr.io/jade-backend:<version> .
docker push <registry>.azurecr.io/jade-backend:<version>
```

The frontend is a static build:

```bash
VITE_API_BASE_URL=https://api.jade.consultiq.nl npm ci && npm run build   # output in dist/
```
