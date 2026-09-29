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
| **Application Manager** | Places stories in a domain, approves them for delivery, approves the exact change set (the agents then apply it in DEV), applies and records only the items marked for a person, reconciles any item an agent stopped on, runs or records the test, finalises the as-built record. |
| **Administrator** | Sets up the customer: users and roles, business domains, engagement scope and approval policy, the AI key, Jira, the JD Edwards connection and agent execution (the DEV write user, the web client, the on/off switches). |

A CNC operator records CNC activation for technical (development object) changes.

## How a change reaches JD Edwards

The agents make the approved changes in the customer's DEV system themselves. A person does only what JD Edwards
cannot accommodate through AIS or the web client.

1. The AI agents turn the request into a user story. The Domain Owner approves it and the Application Manager
   approves it for delivery.
2. The Architect researches the customer's DEV environment through the customer's JD Edwards connection (approved,
   read-only AIS reads, with the read-only discovery user) and the Functional Agent proposes the exact configuration
   change set. Jade marks each item with its route: **agent** (AIS form requests, or the web client in an
   agent-driven browser) or **person**.
3. The Application Manager approves that exact change set. Jade reads each target live and binds the approval to it.
4. The agents apply each agent item in order (`api_service/jde_api_service/executors/`), signed in as the customer's
   dedicated DEV write user: the whole delivery gate runs again for every item (approval, authority, scope, DEV only,
   writes not paused), a live read before the change must show the approved-against state, the change is exactly the
   approved item, and a live read-back must show exactly the approved values. A mismatch or an unknown outcome stops
   the item for reconciliation; it is never retried blindly. Browser steps are screenshotted as evidence.
5. A person applies only the items marked for a person, or an agent item no agent can apply right now (agent execution
   switched off or not set up, or the agent stopped before saving anything), and records it; the hand-over is recorded.
6. The approved test Orchestration runs live on the customer's AIS server, or the person records the test result.
7. The as-built record is generated and finalised when every delivery checkpoint is complete.

Everything about a customer's JD Edwards -- AIS, web client and Web OMW addresses, certificates, the read-only and
the write user, environment and path code, and the per-capability on/off switches for agent execution -- is entered
in Administration > Systems & Connections > JDE and stored per customer, secrets encrypted. Nothing about a
customer's JD Edwards comes from the server's environment.

Technical changes (business functions, event rules) still follow the recorded route: the Technical Agent prepares the
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
