# Jade — backend

Jade takes a business request or support ticket to a verified change on a customer's JD Edwards 9.x system. This
repository holds the backend: the API (`api_service`), the agents' tool server (`mcp_server`) and the agent
definitions (`.claude/agents`). The frontend is `JDE_change_factory_frontend`.

Jade serves many customers from one installation. Each customer has its own users and roles, business domains,
engagement scope, AI connection (its own Anthropic key and model), Jira connection and JD Edwards connection. Every
customer-scoped request is checked against the signed-in person's membership of that customer.

Nothing is simulated. There is no mock mode, no demo data and no fake provider. A new installation starts empty,
apart from one first customer and a temporary setup account.

## The three journeys

| Role | What they do in Jade |
|---|---|
| **Domain Owner** | Raises requests, reviews and approves the user story for their business domain, follows delivery. |
| **Application Manager** | Places stories in a domain, approves them for delivery, approves the exact change set, applies it in DEV and records each item, runs or records the test, finalises the as-built record. |
| **Administrator** | Sets up the customer: users and roles, business domains, engagement scope and approval policy, the AI key, Jira and the JD Edwards connection. |

A CNC operator records CNC activation for technical (development object) changes.

## How a change reaches JD Edwards

Jade never writes to JD Edwards. The approved change is applied in DEV by an authorised person, and Jade verifies it:

1. The AI agents turn the request into a user story. The Domain Owner approves it and the Application Manager
   approves it for delivery.
2. The Architect researches the customer's DEV environment through the customer's JD Edwards connection (approved,
   read-only AIS reads) and designs the solution. For a configuration change, the Functional Agent -- the JD Edwards
   configuration specialist -- then specifies the exact configuration change set in the same run, to the customer's
   configuration standards and the JD Edwards manuals in its Start-up Pack: UDC values, set-up tables, document and
   line types, order activity rules, processing options, batch version data selection and sequencing, each checked
   against the customer's approved configuration (Governance).
3. The Application Manager approves that exact change set. Jade reads each target's current state live and binds
   the approval to it.
4. A person applies the items in DEV, in order, and records each one. Jade re-checks the approval, scope and
   authority and reads each item back live. Only exactly the approved values are ever recorded as applied. When the
   connection cannot read an item (a batch version's data selection, for example), the person states it with an
   evidence reference, and the record says so.
5. The approved test Orchestration runs live on the customer's AIS server, or the person records the test result.
6. The as-built record is generated and finalised when every delivery checkpoint is complete.

Technical changes (business functions, event rules) follow the same principle: the Technical Agent prepares the
package; people apply, build and promote it through OMW and CNC, and record each step.

## Running it

- **On your Mac:** `docs/PREVIEW.md` (one script starts everything, with your data kept between runs).
- **First-time setup of a customer:** `GETTING_STARTED.md`.
- **On Azure:** `docs/AZURE_DEPLOYMENT.md` (Container Apps, Azure Database for PostgreSQL, Blob Storage, Key Vault).
- **Operating it:** `docs/OPERATIONS.md` (keys, backups, the JD Edwards connection, e-mail, rate limits).
- **AI connections and agent packs:** `docs/AI_CONFIGURATION.md`.

## Storage

The same code runs in both set-ups; configuration picks the back-end.

| What | Single server | Azure |
|---|---|---|
| Database | SQLite under `JDE_API_DATA_DIR` | PostgreSQL (`JDE_DATABASE_URL`) |
| Uploaded files (source exports, reference documents, workbooks, attachments) | local folder under `JDE_API_DATA_DIR` | Blob Storage (`JDE_BLOB_CONTAINER_URL`) |
| Secrets (credential key, SMTP password) | environment | Key Vault |

Credentials that customers enter in the app (AI keys, JDE passwords, Jira tokens) are encrypted with
`JDE_CREDENTIAL_KEY` and never shown again.

## Tests

```bash
cd api_service
pip install -e . -e ../mcp_server
pytest -q                                   # on SQLite
JDE_TEST_DATABASE_URL=postgresql://... pytest -q   # the same suite on PostgreSQL (one schema per test)
```

The tests run the real code. Only the external systems are replaced, at their network boundary: JD Edwards by a
fake AIS server answering over HTTP (`tests/fixtures/fake_ais.py`), Jira by a recording gateway, the mail server by
a recording SMTP double, and the language model where a test needs an agent's answer.
