# Running Jade on your Mac

This runs the real backend and frontend on your own Mac, always from the latest `main` of both repositories (the
launcher updates them on every start). The footer shows the frontend and backend commits that are running.

- **Your real customers:** create them in **Admin › Customer Setup › New customer**. A real customer only ever uses
  live connections; nothing is simulated for it.
- **Demo customers** (BicycleWorks and three others) hold test data. They carry a DEMO badge and a banner on every
  page, and only they may use a simulated JDE. The process framework in BicycleWorks is a synthetic fixture.

## One-time setup

1. Install Homebrew, if you don't have it. Follow the one command on https://brew.sh.
2. In Terminal, install the tools:

   ```bash
   brew install git python@3.12 node gh
   ```

3. Sign in to GitHub. The repositories are private.

   ```bash
   gh auth login
   ```

4. Get both repositories side by side:

   ```bash
   mkdir -p ~/jade && cd ~/jade
   gh repo clone Hendro33/JDE_change_factory_backend
   gh repo clone Hendro33/JDE_change_factory_frontend
   ```

## Start (every time)

```bash
~/jade/JDE_change_factory_backend/scripts/run_local_preview.sh
```

The first start installs everything into a private environment, which takes a few minutes. It also adds the
demonstration data once. Later starts reuse the same data, accounts and passwords. Leave the Terminal window open,
and stop the preview with Ctrl-C.

- **Address:** http://localhost:5173, in Safari or Chrome on the same Mac.
- **Sign-in:** the demo users are local, throwaway accounts. Their passwords are generated once on your Mac and are
  never printed or committed. To see them:

  ```bash
  cat ~/jade/JDE_change_factory_backend/.preview-data/credentials.env
  ```

  | User | Password variable | Role |
  |---|---|---|
  | `admin@e2e.local` | `ADMIN_PW` | Admin and product manager |
  | `do@e2e.local` | `DO_PW` | Domain Owner for Customer Service |
  | `cnc@e2e.local` | `CNC_PW` | CNC operator |

- **Optional:** the "Run refinement process analysis (real agent)" and Architect buttons need the Claude Code CLI
  installed and signed in on the Mac (`npm install -g @anthropic-ai/claude-code`, then `claude`). These buttons use
  your model usage. Everything else works without it.

## Walkthrough (about 15 minutes)

1. **Delivery › Process & Maps › S-BW-RETURNS.** Check the following:
   - The agent suggestions are labelled *scripted stand-in*.
   - The confirmed processes are pinned to framework v1.
   - The **Story refinement** section shows revision r2, where the demo admin applied two findings.
   - The as-is and to-be maps: dashed amber steps are assumptions, and solid green steps are confirmed practice.
2. Follow the journey bar through Design and Implementation (SIMULATED, verified) to the **As-built record**. Then:
   - **Generate new version**;
   - **Finalise this version**;
   - **Download Markdown**.
3. Back on **Process & Maps**, in **Story refinement**:
   - tick a proposed finding and choose **Preview story changes**;
   - check the diff and **Apply**;
   - look at the new revision r3 and at the Design section: the design is now flagged for reassessment, so a new
     as-built draft cannot be finalised.
4. **Delivery › As-built Records › S-BW-RETURNTYPE**, a Functional-route story. **Generate** a record: it shows the
   recorded change (order type S3 → CR) and its read-back from the simulated DEV. Then **Finalise** it.
5. **Admin › Process Framework.**
   - The status says official APQC content is **not loaded**.
   - To try a framework update, choose the workbook
     `~/jade/JDE_change_factory_backend/fixtures/process_framework/SYNTHETIC_bicycleworks_process_framework_v2.xlsx`,
     pick "New version of…", then **Validate and preview** and **Activate**. The story keeps its v1 references,
     which are marked "changed".
   - To load real content, import your authorised APQC workbook as "APQC content the customer is authorised to use",
     with its licence reference.
6. Open a private window, sign in as `do@e2e.local` and open the same story. It shows the same saved data. The
   Domain Owner can review and edit maps in their own domain.

## Your customer: configuration in the app

Everything below is done in the browser and saved on Jade's backend:
- **Admin › Customer Setup:** the customer's name, Tools release and JDE environment; links to all its settings.
- **Admin › Integrations:** the JDE connection (address, environment, role, user and password, approved reads,
  Test Connection, sample read) and Jira (site, project, statuses, fields, API token, Test Connection, Sync).
- **Admin › Business Domains, Process Framework, Agents** (switch an agent on or off for this customer), **Users**.
- **Admin › ERP / JDE Landscape:** engagement scope and approval policy.

## First read-only JDE connection (prepared; not yet authorised)

Nothing below contacts JDE until you press **Test Connection**, and no business read runs until you press **Run Approved
Sample Read** yourself. Neither has been authorised yet.

Open **Admin › Integrations**, then the **JDE connection** panel, and choose **Edit settings**. Everything you enter is
stored on Jade's backend. **Save** never contacts JDE.

| Field | What to enter |
|---|---|
| Connection name | Any label |
| Mode | **Live** (read-only; there is no fallback to simulation) |
| AIS HTTPS address | `https://141.144.202.25:7077`. Jade appends `/jderest/...` itself |
| JDE environment | The exact name the session should report: `JPS920`. Jade compares names exactly and never treats `JPS920` and `PS920` as the same |
| Environment purpose / approval reference | As approved by the customer |
| JDE role | The **dedicated** role of the dedicated user. `*ALL` is refused |
| Application release / Tools / server release | What you expect. Test Connection records what JDE reports next to it |
| Path code | **Leave blank.** Jade never derives it from the environment name. It is set only by JDE's answer to the F00941 read (below) |
| Dedicated JDE account | The user and role, who verified them, when and how, both confirmations, and at least one linked evidence document (for example a Security Workbench export imported as a reference document). A statement or a "read-only" label alone does not count |
| Network restriction | The backend's source address as JDE sees it, confirmation that AIS accepts only that address, and the evidence (for example the OCI security-list rule) |
| Approved discovery reads | **User defined code values**, target `00/DT`, columns `DRSY, DRRT, DRKY, DRDL01`. To establish the path code, also add **Table browse**, target `F00941`, columns `EMENHV, EMPATHCD`, filter column `EMENHV` |
| Window | Today to a few days ahead (at most 31 days) |
| Records per query / timeout | `5` / `15` |

After saving, enter the JDE user and password under **Credential**. The password is encrypted on the server and never
shown again.

**Server-managed trust.** These settings are made on the machine that runs the backend, never in the browser. Create
`~/jade/JDE_change_factory_backend/.preview-data/server.env` containing:

```
JDE_DISCOVERY_LIVE_ENABLED=true
JDE_DISCOVERY_ALLOWED_HOSTS=141.144.202.25
JDE_DISCOVERY_CA_BUNDLE=/path/to/ais-server-certificate.pem
```

Then restart the preview. Every AIS request uses that file for certificate checks: sign-in, defaultconfig, sample reads and
sign-out. The certificate must match the address (here an IP address entry in the certificate's alternative names). If the
file is missing, unreadable or not a certificate, live access stays **off**, the backend log says why, and the panel shows
it. Jade never falls back to unverified TLS. On Python 3.13 or newer, strict certificate checks can also reject a
certificate that lacks standard extensions; the panel shows the TLS error, and the fix belongs to the certificate, not
to Jade. The backend machine itself must reach `141.144.202.25:7077`.

**Supervised order:**
1. **Test Connection.** Jade signs in, reads the AIS server identity (`defaultconfig`) and signs out. It runs no UBE, batch
   job or business query. The **Identity** status shows configured and reported values side by side: environment, role,
   application release and Tools / server release. A different environment name, or a `*ALL` role, is shown as a mismatch
   and is never corrected. Sign-in can work while the connection stays **not ready**.
2. **Preview exact request**, then **Run Approved Sample Read** (only once you approve it): `udc_values`, target `00/DT`,
   max records `5`, no filter. The preview shows the method, address, body and checksum the server will send. The run is
   refused if anything differs from the approved read.
3. Optional, if approved: the F00941 read with filter `EMENHV = JPS920` establishes the path code from JDE.
4. **Enable Architect Discovery** unlocks only when all five statuses are satisfied: Connectivity, Identity, JDE
   authorisation, Network restriction and Jade runtime safeguards.

JDE writes stay simulated throughout.

## Prerequisites for a shared (hosted) preview

These use the agreed Azure direction. The earlier S1-3 document proposed Render; that was superseded. None of these
is authorised yet.

1. Owner authorisation and an Azure subscription with a monthly budget.
2. A resource group, a region, and one owner account with the rights to create resources.
3. Code changes already identified:
   - a Dockerfile;
   - SQLite and JSON files → PostgreSQL;
   - uploaded files and original workbooks → Blob Storage;
   - secrets → Key Vault (managed identity).
4. A server-side Anthropic API key with a spending limit, for agent runs in the hosted backend.
5. Secrets created in Key Vault only:
   - the credential-encryption key;
   - the bootstrap admin;
   - the API key.
6. Access restriction in front of the site, because there is no MFA yet: Entra ID, or an IP allowlist. Invitation
   links are sent by hand until an email provider is chosen.
7. Backups, and one restore drill before any real customer data.

JDE stays simulated: no VPN and no customer connection.
