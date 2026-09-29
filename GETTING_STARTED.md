# Getting started — setting up a customer

This is the Administrator's route from an empty installation to a customer whose Domain Owners and Application
Managers can take a request through to a verified change on the customer's JD Edwards DEV system. Everything is done in
the browser and saved on Jade's backend.

To start Jade on your Mac, follow `docs/PREVIEW.md` first. On Azure, follow `docs/AZURE_DEPLOYMENT.md`.

## 1. Your own administrator account

1. Sign in as the temporary setup account (`setup@jade.local` locally; `JDE_BOOTSTRAP_ADMIN_EMAIL` on a server).
2. Use **Finish setup** at the top of the page: your e-mail, name and a password of at least 12 characters. The setup
   account is switched off, and you are signed out.
3. Sign in with your own account.

## 2. The customer and its people

**Administration › Organisation.**

- **Customer:** rename the first customer (it is created for you) or add more. Each customer's data is separate.
- **Users & roles:** invite people and give them roles: Domain Owner, Application Manager, CNC Operator, Dashboard
  Viewer, Admin. With a mail server configured, the invitation is e-mailed. Without one, Jade shows you the link to
  hand over yourself, and says so.

## 3. Business domains

**Administration › Business Model.** Create the customer's business domains, then give each Domain Owner their
domain under **Users & roles › Edit roles**. A domain without an owner shows "nobody can approve".

A process framework (an `.xlsx` workbook the customer is licensed to use) is optional. Without one, stories are not
mapped to processes, and the as-built record says so.

## 4. AI connection

**Administration › Agents & AI.**

1. Enter the customer's Anthropic API key. It is encrypted and never shown again; saving never contacts Anthropic.
2. Choose the model and the monthly budget, and **Save settings**.
3. **Test connection…** sends one small, billable request. A rejected key is reported as rejected.
4. Under **Agent configuration**, assign a Start-up Pack to each agent (Receive, Improve, Check, Architect, and the
   others you use). Agents without a pack do not run.

## 5. Jira (optional)

**Administration › Systems & Connections › Jira.** Site, project, status and field mapping, API token, then
**Test connection** and **Sync**.

## 6. The JD Edwards connection

**Administration › Systems & Connections › JDE.** Nothing contacts JD Edwards until you press **Test Connection**.

1. **Edit settings** and fill in:
   - connection name, the AIS HTTPS address (Jade appends `/jderest/…`), the exact JDE environment name and its
     purpose, the dedicated JDE role (never `*ALL`), and the application and Tools releases you expect;
   - the AIS server's certificate or its CA (`.pem` / `.crt`). Jade trusts it for this connection only, and
     certificate and host-name checks are never switched off;
   - the approved discovery reads: for example user defined codes `00/DT`, the processing options of the versions in
     scope (e.g. `P4210|CIQ0001`), and table browse of `F00941` with filter column `EMENHV` to establish the path code;
   - the network restriction (the backend's source address as AIS sees it, and evidence), the CNC's runtime
     attestation, the discovery window and the per-query limits.
2. **Save**, then enter the JDE user and password under **Credential**. The password is encrypted and bound to this
   address and certificate.
3. Import the evidence that the JDE user is narrowly privileged (for example a Security Workbench export) under
   **Import an approved export or document** as a *Reference document*. Then **Edit settings**, tick it under
   **Verification evidence documents**, and **Save**.
4. **Test Connection.** Jade signs in, reads the AIS server identity and signs out, and shows what JDE reported next
   to what you configured.
5. **Preview exact request**, then **Run Approved Sample Read** for each approved read. The F00941 read records the
   path code.
6. When **Readiness for discovery** is *ready*, **Enable Architect Discovery**.

The connection, its credential and its readiness are remembered across restarts. Any material change to the settings
switches discovery off until you test again. **Disable Connection** stops all requests at once.

### Agent execution (same page, below)

The agents make the approved changes in DEV themselves, with a **separate DEV write user**. Ask the customer's CNC for
a dedicated JD Edwards user and DEV role for Jade's changes (never `*ALL`, never the discovery user).

1. **Set up**: the JD Edwards web client address of DEV (e.g. `https://jde-dev.customer.example/jde`), Web OMW only
   if it has its own address, the web client's certificate if it is self-signed or private, and the write role.
   **Save settings**.
2. Enter the **DEV write user** and its password, **Save write user**. It is encrypted and never shown again.
3. **Test**: the write user signs in to AIS and to the web client (in the agents' browser); nothing is changed.
4. Both routes show **Ready**. Under **Governance › Agent execution** you can switch the agents off for the customer
   or per capability at any time; every switch is recorded with your name and the date.

## 7. Engagement scope and approval policy

**Administration › Governance › Edit.** This is what the delivery gate enforces for this customer's stories.

- **Approval policy:** which roles may approve an exact change (normally Application Manager), and for how many hours.
- **DEV environment binding:** environment, path code and business data source, with your confirmation and evidence
  that its OCM mappings cannot affect another environment.
- **Mechanisms allowed:** configuration changes and/or test Orchestrations.
- **Approved versions:** one line per version, e.g.
  `processing_option_update|document_and_order_types|P4210|CIQ0001|PDOCTYPE|SO|webshop order entry`.
  XJDE/ZJDE versions are always refused.
- **Approved tests:** one line per Orchestration, e.g. `ORCH_SO|creates_dev_transaction|creates one DEV sales order`.
- **Never-touch categories** are categories Jade must refuse. Tick only what must never change.
- For technical changes: the authorised object types, reserved product code and naming prefix.

## 8. The first story

1. **Domain Owner:** *Business Demand › New request*. Jade's agents write the user story.
2. **Application Manager:** place it in a business domain.
3. **Domain Owner:** *User Story Review*: confirm impact and benefit, then **Approve**.
4. **Application Manager:** *Backlog Review* › **Approve for Delivery**. The Architect researches DEV and proposes a
   solution with its exact change.
5. **Application Manager:** *Architecture Review* › **Approve exact change**. The current values are read live and
   shown, with who applies each item (the agents through AIS or the web client, or a person).
6. The agents apply the items in DEV and Jade reads each back live (*Delivery* shows the progress and screenshots of
   the agents' web-client steps). Apply and record only the items marked for a person; reconcile any item an agent
   stopped on.
7. **Run approved test** (the Orchestration runs live), or record the test result yourself.
8. *As-Built* › **Generate the as-built record** › **Finalise and complete the story**.

Every step shows on the story page as the single next step, with who it waits for.
