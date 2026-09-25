# Trying Jade on your Mac (local preview)

The preview runs the real backend and frontend on your own Mac. JD Edwards is **simulated**: nothing connects to any
JDE system. The process framework is a **synthetic fixture** (SYN- ids), not APQC content.

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
   gh repo clone Hendro33/JDE_change_factory_backend  -- -b claude/stage1-setup-and-safeguards
   gh repo clone Hendro33/JDE_change_factory_frontend -- -b claude/focused-gates-gtay96
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
