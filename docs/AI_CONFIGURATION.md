# Customer AI configuration, Start-up Packs and request documents

What a customer Admin configures, what the backend enforces, and how it is
verified. Code: `api_service/jde_api_service/ai/` (connection, packs, runtime,
context) and `api_service/jde_api_service/knowledge/` (attachments,
extraction, knowledge tools).

## 1. AI connection (Admin › AI Connections)

* One **company-owned Anthropic API key** per customer, entered by an Admin of
  that customer. Jade users need no AI account. Jade sign-in, the AI key and
  the JDE discovery account are three separate credentials.
* The key is encrypted with the server key (`JDE_CREDENTIAL_KEY`), is
  write-only (only `…last4` is ever shown), can be replaced (new key revision)
  or revoked. It never appears in responses, logs, audit entries or errors.
* **Saving never contacts Anthropic.** "Test connection" is a separate action
  that must be confirmed as billable: one request, one output token.
* **Models per agent activity.** A default model plus optional overrides:

  | Activity | Jade agent roles |
  |---|---|
  | Functional Analysis | receive-agent, improve-agent, process-analyst |
  | Verification | check-agent; the Technical Agent's *verify* runs |
  | Architecture | architect |
  | Technical Build | technical-agent (*prepare* / *execute* runs) |

  Only combinations in `connection.RUNTIMES` / `ACTIVITY_MODELS` (reviewed code)
  can be selected. Today that is one runtime (Claude Agent SDK driving the
  Claude Code CLI) for one provider (Anthropic). No other provider is shown,
  and nothing falls back to another provider, key or model.
* **Document policy.**
  * *Metadata only*: agents see document names and readability, never their
    text. The read tool is not offered at all.
  * *Permitted document content*: agents whose Start-up Pack references
    documents may read their extracted text.
* **Limits.** Maximum USD per run (passed to the runtime as its budget),
  monthly USD budget (runs are refused once it is used up) and maximum turns.

## 2. Start-up Packs (Admin › Agent Configuration)

* The repository's agent definitions (`.claude/agents/*.md`, and the process
  analyst prompt) are imported at start-up as **Jade standard** templates.
  They are read-only, and a changed file becomes a new template revision.
* A customer copies a template, edits a **draft** (instructions, reusable
  skills, knowledge references, expected inputs/outputs, requested tools,
  max turns) and **publishes** it. Published revisions are immutable.
* **Assignment.** One published revision is assigned per role. Assigning an
  earlier revision is the rollback. A disabled pack, or a role with no pack,
  blocks that agent with the reason shown. Everything is audited.
* **Tools a pack can request.** Only tools within the role's ceiling
  (`packs.ROLES`, reviewed code) can be requested. The driver's own allow-list
  narrows them again, and anything not allowed is hidden from the model, not
  merely denied. Packs are text: nothing in them is executed, and approval
  gates stay in backend code.

## 3. What every agent run gets (ai/runtime.py)

* It resolves the customer's connection (`resolve_for_run`) and the assigned
  pack snapshots, **once, at run start**, together with the model of each
  role. Edits made during the run do not reach it.
* **Isolation per run.** The runtime subprocess receives the customer's key
  and model through its own environment. `os.environ` of the backend is never
  modified. Every other inherited variable is neutralised: **we observed the
  real CLI using an inherited host session token while reporting the API key
  as its source**, so blanking a few known variables is not enough. Each run
  also gets:
  * a throw-away `CLAUDE_CONFIG_DIR` (no login, user settings or session reuse);
  * project settings only (the reviewed hooks);
  * background subagents disabled (**observed**: the CLI otherwise runs
    subagents asynchronously and a run could end before they finish).
* **Init check.** The runtime's init report must name `ANTHROPIC_API_KEY` as
  its key source and the configured model, or the run stops before any model
  request.
* **Context package (ai/context.py).** This is a versioned, immutable,
  content-addressed snapshot built from the backend's own records:
  * the requirement as submitted and the current user story (acceptance
    criteria, rules, assumptions);
  * the recorded human approvals;
  * the architecture decision, with the design-baseline reference, verified
    evidence ids and gaps;
  * the request's document manifest;
  * the questions still open.

  It is handed to the agent as data. Hand-offs never depend on an earlier
  model's conversation. The package grants nothing.
* **Run record (`ai_runs`, shown in Agent Configuration).**
  * Driver and story.
  * Provider and runtime with versions (for example `claude-agent-sdk 0.2.157
    / Claude Code 2.1.277`).
  * Configured and reported models, per role.
  * Key source.
  * Connection and key revisions.
  * Pack revisions with their hashes.
  * The context package version.
  * Documents listed and read, with checksums and sections.
  * Token usage as reported, with cost labelled as an **estimate**.
  * Outcome.
  * Notes when a story's model or configuration changed since its previous
    run. A configuration change affects new runs only, and never silently.
* **Agent Health** is computed from this configuration and these records.
  "Working" requires a completed run that the runtime reported on this
  customer's key against Anthropic. Test-provider runs never count.

## 4. Documents on requests (Demand › Create Request)

* Up to 5 PDF/DOCX/TXT files, at most 10 MB each and 25 MB together. The
  type is checked from the content and must match the extension. Names are
  sanitised.
* Originals are kept privately in `<data>/request_attachments/`, with the
  uploader, time, type, SHA-256 and revision. The checksum is verified on
  every read. Download is limited to the customer's authorised users.
* Extraction runs in a separate, time- and memory-limited process with no
  server secrets. No embedded content is ever executed:
  * PDF: text only, per page;
  * DOCX: parsed with defusedxml, with sections from headings; macro files
    are refused and zip size and ratio are checked;
  * TXT: must be UTF-8.
* States are pending → extracting → ready | failed. Encrypted PDFs and
  scanned PDFs (no OCR) are reported as unreadable. The requirement is always
  kept, and a failed document can be retried or removed.
* **Uploading never calls a model.** Pending uploads that are never submitted
  are deleted after 24 h. Removing a document deletes its file and keeps the
  record of who removed it and when.
* **In refinement.** Agents reach documents only through the `jade-knowledge`
  tools, bound to the run's customer, story and pack knowledge references.
  Text comes back labelled as untrusted evidence with cite labels such as
  `[file.pdf, page 2]`. The story's citations are marked **verified** only if
  that exact section was returned in that run. A human edit can drop a
  citation but cannot mark one verified.
* **Knowledge Library (Admin › Knowledge Library).** Reference documents
  (with immutable revisions) that packs reference by exact revision. They
  follow the same extraction rules and the same document policy.

## 5. Verification — what is and is not shown

| Evidence | Real Anthropic? | How |
|---|---|---|
| `tests/test_ai_runtime.py`, `tests/test_request_documents.py` | no (mocked runtime) | connection/key handling, no provider call on save, blocking cases, forbidden tools, tampered/disabled packs, snapshot kept during a run, per-activity models, context packages, spawn-level env of two concurrent runs, attachments, policy, citations |
| `scripts/prove_runtime_isolation.py` | no (real CLI, loopback fake provider) | two concurrent customers: each request carried only its own key and model; host key/token never sent; separate config dirs; pack instructions reached the provider; no background subagents; blocked runs sent nothing |
| `e2e/ai/ai_flow.py` (frontend repo) | no (real app + real CLI, scripted fake provider) | the whole Admin/User flow in a browser, restart, new browser, cross-customer |
| `scripts/prove_ai_real_provider.py` | **yes, billable, needs approval** | see below |

**Real-provider test.** One synthetic requirement and a synthetic PDF, on
Claude Haiku 4.5 by default. It is capped at USD 0.50 per run and USD 1.00
for the month; the expected cost is about USD 0.05–0.30. The key comes from
the environment of that one command and is never printed:

    JADE_PROOF_ANTHROPIC_API_KEY=... python3 scripts/prove_ai_real_provider.py

`--rehearse` runs the same procedure against the loopback fake
(`scripts/fake_anthropic_provider.py`), which checks the procedure but says
nothing about Anthropic.

## 6. Limitations (current)

* Only the Anthropic API through the Claude Agent SDK is implemented, and
  evaluation of models against Anthropic is pending the approved real test.
* The CLI's own reported cost is at list prices, so it is an estimate and not
  an invoice.
* There is no OCR, no vector search (bounded selection by the pack's
  references), no DOCX tables-as-structure and no attachment added after
  submission.
* The Architect's own discovery tools are unchanged. The context package
  refers to the design baseline; it does not copy evidence payloads.
* The real-model proof scripts for the Architect, Technical and Process
  agents now require `JADE_PROOF_ANTHROPIC_API_KEY` (no machine login).
