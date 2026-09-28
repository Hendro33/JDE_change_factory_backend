# Running Jade on your Mac

One script starts the real backend and frontend of the branches you have checked out, with your own data kept
between runs. Nothing is simulated and nothing is pre-loaded.

## One-time setup

1. Install Homebrew (the one command on https://brew.sh), then the tools:

   ```bash
   brew install git python@3.12 node gh
   ```

2. Sign in to GitHub (the repositories are private) and get both repositories side by side:

   ```bash
   gh auth login
   mkdir -p ~/jade && cd ~/jade
   gh repo clone Hendro33/JDE_change_factory_backend
   gh repo clone Hendro33/JDE_change_factory_frontend
   ```

## Start (every time)

```bash
~/jade/JDE_change_factory_backend/scripts/run_local_preview.sh
```

- The first start installs everything into a private environment (a few minutes).
- Each start fast-forwards both clones to the latest commit of their branch, unless they have local edits.
- Open **http://localhost:5173**. Stop with Ctrl-C. Start again to continue with the same data.

**First sign-in.** Sign in as `setup@jade.local`. While that temporary account is active, the launcher puts its
password on your clipboard (it is never shown); it is also the `ADMIN_PW` line in `.jade-data/credentials.env`, a file
readable only by you. Then use **Finish setup** to create your own administrator account; the setup account is
switched off for good. From then on, follow `GETTING_STARTED.md`.

## Your data

Everything Jade saves is under `JDE_change_factory_backend/.jade-data/` (git-ignored):

- `api/`: the database (accounts, password hashes, encrypted credentials, settings, stories, approvals, evidence)
  and uploaded files;
- `credentials.env`: the setup password and the credential-encryption key, generated once. **Keep this file.**
  Without its key, the saved AI keys, JDE passwords and Jira tokens cannot be decrypted;
- `backend.log`, `frontend.log`.

An older `.preview-data/` folder (the former demo data) is not used and is left as it is.

`run_local_preview.sh --reset` throws all data away, after you type `yes`.

## Optional server settings

Put these in `.jade-data/server.env`, one `KEY=value` per line. Other keys are ignored.

| Setting | Use |
|---|---|
| `JDE_SMTP_HOST`, `JDE_SMTP_PORT`, `JDE_SMTP_USERNAME`, `JDE_SMTP_PASSWORD`, `JDE_SMTP_STARTTLS`, `JDE_MAIL_FROM`, `JDE_PUBLIC_URL` | E-mail invitations and password resets. Without them, Jade shows you the links to hand over. |
| `JDE_DATABASE_URL`, `JDE_DATABASE_SCHEMA` | Use PostgreSQL instead of the local SQLite database. |
| `JDE_BOOTSTRAP_CUSTOMER_NAME` | The name of the first customer on a fresh start. |
| `JDE_DISCOVERY_LIVE_ENABLED` | `false` locks every JD Edwards connection off. |
| `JDE_DISCOVERY_ALLOWED_HOSTS` | Limits which AIS hosts may be contacted. |
| `JDE_DISCOVERY_CA_BUNDLE` | A CA file for connections without an uploaded certificate. |
| `JDE_ANTHROPIC_BASE_URL` | An HTTPS gateway in front of the Anthropic API. |

## Reaching your JD Edwards system

Jade contacts the AIS server from the backend, so this Mac must reach the AIS address (VPN or firewall rule), and
the AIS server should accept only this Mac's source address. Set up the connection as described in
`GETTING_STARTED.md`, step 6.

## AI

The AI agents use the customer's own Anthropic API key, entered under **Administration › Agents & AI**. No Claude
login on the Mac is needed.
