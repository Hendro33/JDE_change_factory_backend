# Validation workspace

## Personas and scope

The Business Domain Owner's story journey is unchanged. A separately assigned manual UAT task is available in My Work; it does not open the architecture or delivery workspace. Test Managers own scenarios, plans, test runs, result review, defects and testing conclusions. Application Managers separately confirm implemented deployments and approve or hold release. Administrators configure the customer, environments, accounts, policies, documents and agents.

Navigation:
- **Validation:** dashboard and filters, Test Library, Plans, Runs & UAT, Defects, Agents.
- **Application Management → Validation & release handoff:** deployment confirmations and release decisions. Existing CNC/deployment controls remain in their existing workspace.
- **Administration → Validation:** environment profiles, execution routes, encrypted accounts, trusted certificates, policies and storage health.
- **Administration → Agents & AI:** existing model, connection, budgets, versioned instructions/skills and knowledge-document assignments. Five new agent templates are available; an administrator must assign published customer packs before they run.

## Customer setup in the UI

1. In Users & roles assign **Test Manager** to named people. This role is not automatically granted to existing or bootstrap administrators. Keep Application Manager release authority separate. Enable independent reviews if required.
2. Add each required environment (DEV, QA, UAT, PREPROD or PROD). Customers select their actual environments in plans; there is no mandatory fixed stage sequence.
3. For automation save the HTTPS AIS and browser endpoints, dedicated account, exact JDE environment/role and any required CA certificate. Passwords are write-only and encrypted with the platform's existing key. Test the saved AIS connection.
4. Enable only the required execution routes. Browser execution also requires selectors that uniquely identify the logged-in environment and role; the observed values must exactly match the saved identity. Missing or mismatched identity blocks execution.
5. Approve exact AIS paths, non-secret test-data parameters, prerequisites, cleanup and downstream isolation. All potentially mutating browser actions and AIS POSTs require both write authorisation and side-effect isolation. A non-production label alone does not authorise transactions.
6. Set freshness, minimum evidence retention, independent review, optional release exceptions and optional Jira defect sync. Production is disabled by default and additionally needs an Application Manager's expiring authorisation for the exact plan, deployment and environment revision.
7. Configure the existing customer AI connection and assign the new Test Design, Regression Scope, Test Execution, Result Assessment and Validation Summary packs. Customer documentation stays in the existing knowledge repository and is referenced by those packs; no parallel document silo is introduced.
8. Test evidence storage. The UI reports worker, browser and encryption-key availability. Infrastructure operators provision the database, blob backend, network access, encryption key and Chromium; ordinary customer configuration does not require server files.

## Operating flow

Create/import a scenario draft with expected results and execution bindings. Approve it, then select approved versions in a plan. Dependencies must precede dependent scenarios. Explain uncovered acceptance criteria and exclusions. Approve the plan. An Application Manager records the exact implemented build and deployment evidence for the same story set. This records an actual deployment; it does not deploy anything.

Run all or selected scenarios. Dependency closure is included automatically. Manual UAT pauses dependent work and appears in the assigned user's My Work. Every submitted step requires an outcome and actual observation; optional file attachments and the observation record are preserved as evidence. Ambiguous automated observations need an explicit human assessment; the raw observations are retained. Review results, record a testing conclusion, then let the Application Manager make a separate release decision.

Failed assertions can create internal defects. Failed retests create new linked attempts; existing attempts and approved versions are not overwritten. Jira sync uses the existing customer connection and project, a stable label and reconciliation after uncertain creates. The Test Manager explicitly requests sync. Jira's status is read for reference; it never overwrites JADE's test outcome, closes a defect without a passing retest, or changes an existing business ticket's status. An uncertain create is searched for rather than repeated blindly.

Dashboard filters cover plan/story/domain/process text, connected application, environment, owner/creator and plan-version date. Customer scope is selected by the existing global customer selector. Reports export a ZIP containing a readable index, full versioned metadata and SHA-256-verified evidence. Evidence export remains limited to authorised users.

When a story has a validation plan, its existing as-built record includes the validation handoff and cannot be finalised without a current Application Manager release approval. Stories without a new validation plan retain their existing workflow. A Test Manager can withdraw a plan that was raised in error or is no longer needed (Plans → Withdraw this plan, with a reason, once no run of it is active). Its versions, runs, evidence and decisions are kept, but it no longer holds its stories' as-built records, cannot be approved, run or released, and leaves the dashboard; saving a new draft version reactivates it.

## Persistence and execution

Validation metadata uses the existing transactional document store (SQLite/PostgreSQL), scoped by customer. Existing document indexes and schema cover the new kinds; there is no destructive migration. Evidence uses the existing local/Azure blob-store abstraction. Existing backup/restore includes these documents and blobs. Backup refuses while a validation worker is executing; credential rotation includes environment credentials. Retention records a minimum preservation date; this increment does not automatically purge evidence.

The API starts a durable queue consumer by default. Set `JDE_VALIDATION_WORKER=false` on API replicas when running a dedicated worker with `python -m jde_api_service.validation.worker`. A worker must share database, blobs, encryption key and repository templates, and reach the configured endpoints. There is one active run/reservation per customer environment, including manual waits and unresolved writes. This deliberately reserves the whole environment instead of allowing concurrent tests to collide on shared test data. Additional replicas safely claim jobs under the shared database lock.

Leases survive API refreshes. Expired leases preserve partial evidence, mark execution interrupted and lock environments with uncertain writes until a Test Manager records reconciliation. Stop requests take effect at action boundaries; they cannot undo an already dispatched transaction. No automatic retry of potentially mutating requests occurs. Duration, action count, agent invocation count and the existing AI runtime's model/turn/cost controls (with token-usage reporting) are enforced. UI start requests retain idempotency keys across uncertain responses.

Source, plan, environment, policy, deployment or freshness changes invalidate current coverage. A historical pass is not sufficient for release. A later hold supersedes a prior approval. Exceptions require explicit policy and a recorded Application Manager rationale.

## Verification and deployment

- Full backend suite: 591 passed, one skipped at the integration checkpoint; additional focused validation/export/authority tests are included in `api_service/tests/test_validation*.py`.
- Actual Chromium tests exercise browser and agent-assisted routes against a local HTTPS fixture, including environment mismatch rejection and text redaction. The AI selection in that fixture is scripted, not a paid provider call.
- AIS tests use the real authenticated transport with HTTP responses supplied at the network boundary. Jira tests verify reconciliation after an uncertain create without duplicate issues.
- Reproducible real-browser UI proof: `python scripts/prove_validation_ui.py --frontend ../JDE_change_factory_frontend`. It creates an isolated synthetic customer and runs admin setup → scenario → plan → deployment confirmation → UAT → review → sign-off → release. It never loads customer production data or calls external ERP systems.
- Frontend: `npm run build` and `node --test tests/workflow.test.mjs`.
- GitHub CI runs the backend on SQLite and PostgreSQL, with Chromium installed. Local verification used SQLite; PostgreSQL verification belongs to that CI gate.

Live customer JDE, Jira and paid AI-provider acceptance is **not** claimed by fixture tests. Complete customer setup above, run a bounded non-production scenario, inspect actual evidence and confirm cleanup before enabling production smoke. The current AI provider is the existing configured Claude runtime; agent-assisted browser execution is constrained DOM control selection, not arbitrary desktop access or a new GPT provider adapter. Azure deployment is a separate operational step; pushing these branches runs CI and does not itself deploy Azure.
