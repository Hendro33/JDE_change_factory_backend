# Technical Agent integration proof -- real runtime, simulated DEV estate

**What this is:** the real Architect and the real Technical Agent, through the Claude CLI runtime, working a SYNTHETIC customer-owned event-rule example in the synthetic `jade_sim_er` simulation format, against the shared simulated DEV estate. It demonstrates agent integration and controls. It does NOT demonstrate editing or deploying a real JDE object: no real mechanism is qualified, the live adapter is unavailable, and every result below is SIMULATION. All approvals and the CNC hand-off were made by synthetic identities.

- Run: 2026-09-24T17:15:56.907320+00:00 to 2026-09-24T17:18:51.419392+00:00 (UTC)
- Backend commit: `be55167b77cc03c7144c251215f5de69583224f0`; frontend commit: `84317467a71ccb1c1abfb805544ce46a60b2bf65`
- Claude CLI: 2.1.281 (Claude Code); claude-agent-sdk 0.2.157

## Checks (harness)

- PASS -- B. the real Architect produced a design -- route Technical Agent
- PASS -- B. design approved for technical implementation by a synthetic approver -- design revision 1
- FAIL -- 1. the real Technical Agent retrieved the design and baseline and stored a package -- outcome clarification_required

## B. The Architect (real run)

- Model reported by the runtime: **claude-sonnet-5**; models billed: ['claude-sonnet-5']; runtime claude_code_version 2.1.277; permission mode dontAsk
- Turns 2; duration 27783 ms; cost USD 0.19824819999999999; usage {"input_tokens": 4, "cache_creation_input_tokens": 6647, "cache_read_input_tokens": 28100, "output_tokens": 3489, "output_tokens_details": {"thinking_tokens": 453}, "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}, "service_tier": "standard", "cache_creation": {"ephemeral_1h_in
- Built-in tools requested: ['Task']; MCP servers passed: ['jade-discovery']; secrets blanked in the agent process: ['JDE_AIS_PASSWORD', 'JDE_AIS_USERNAME', 'JDE_BOOTSTRAP_ADMIN_PASSWORD', 'JDE_CREDENTIAL_KEY', 'JDE_CREDENTIAL_KEY_PREVIOUS']
- Tool inventory the runtime reported: ['Task', 'mcp__jade-discovery__discovery_read', 'mcp__jade-discovery__list_baseline_artifacts', 'mcp__jade-discovery__list_discovery_capabilities', 'mcp__jade-discovery__read_baseline_artifact', 'mcp__jde-change-factory__get_approved_story', 'mcp__jde-change-factory__propose_change', 'mcp__jde-change-factory__resolve_without_change']
- MCP servers connected: [('jde-change-factory', 'connected'), ('jade-discovery', 'connected')]
- Stage: done; error: None
- Decision: {"recommended_route": "Technical Agent", "confidence": 0.85, "existing_functionality_found": "", "alternatives_considered": [{"approach": "Resolve without change (standard JDE credit-check functionality already handles exemption)", "whyNot": "The hold decision is not driven by standard JDE credit-hold processing; it is hard-coded in the OK-button event rules of the customer-owned application P554210 (system code 55), confirmed via object_librarian read. Standard functionality cannot reach logic inside a customisation."}, {"approach": "Functional Agent processing_option_update", "whyNot": "processing_option_values is not an approved discovery capability for P554210 (approved_targets empty), and more fundamentally the change needed is a new logical condition inside the OK_Button_Clicked event's IF statement, not a settable processing-option value. A processing option cannot add a comparison operand to compiled event-rule logic."}, {"approach": "Batch version / UDC / master-data configuration route", "whyNot": "No processing option, UDC, or master-data switch is evident in the exported event-rule logic; the IF/ELSE is a literal hard-coded comparison with no configurable inputs. These capabilities also have no execution adapter in Jade per the catalogue."}, {"approach": "Reuse an existing customisation/pattern", "whyNot": "No other object or approved pattern already implements a credit-exemption check; P554210 is the sole customisation in scope and is itself the object that must 
- Implementation specification: {"sequence": ["Human confirms the exact value/domain convention for BC CreditExempt that indicates an exempt customer, and its data source (Customer Master field, UDC, or upstream event).", "Human confirms whether CreditExempt is populated/validated elsewhere on W554210A that should be in regression scope.", "Human/Architect finalise the exact replacement IF condition for the OK_Button_Clicked event based on the confirmed value domain.", "Technical Agent prepares the exact ER-text implementation package against the ART-ER-P554210@r1 baseline in DEV (simulation adapter, custom_object_text_change).", "A person approves the Technical Agent's design/package separately (Gate per Technical Agent workflow).", "Technical Agent executes the approved package in DEV (DV920).", "Validate against AC1, AC2, AC3 in DEV before any promotion path is considered."], "required_mcp_operations": [], "human_actions_required": ["Confirm the true/exempt value convention for BC CreditExempt and its data source.", "Confirm whether a formal test_script exists beyond the embedded AC1-AC3 text, or whether those acceptance criteria are the complete test.", "Approve the final ER-text design for the OK_Button_Clic
- Design approval (synthetic approver): {"id": "DA-5c5d5f0a7e", "company_id": "bwm", "story_id": "S-PROOF-TECH-1", "design_revision": 1, "baseline_id": "BL-064db359f3", "manifest_sha256": "ae54fa718d5b652c55a36a686652fb71110bd3d87fbc3d1309f6b3461756b66d", "approved_by": "Synthetic Approver", "approver_user_id": "u-syn-approver", "roles": ["product_manager"], "note": "synthetic design approval for the proof", "approved_at": "2026-09-24T17:17:58.331804+00:00"}

| # | Tool | In subagent | Input | Result (start) |
|---|---|---|---|---|
| 1 | `Agent` |  | `{"description": "Architect review of S-PROOF-TECH-1", "subagent_type": "architect", "prompt": "Review approved story S-PROOF-TECH-1 followin` | [{"type": "text", "text": "Async agent launched successfully. (This tool result is internal metadata \u2014 never quote or paste any part of it, including the agentId below, into a |
| 2 | `mcp__jde-change-factory__get_approved_story` | yes | `{"story_id": "S-PROOF-TECH-1"}` | [{"type": "text", "text": "{\n  \"story_id\": \"S-PROOF-TECH-1\",\n  \"user_story\": \"SYNTHETIC STORY for the simulation. As a credit controller, I want webshop sales orders (type |
| 3 | `mcp__jade-discovery__list_discovery_capabilities` | yes | `{}` | [{"type": "text", "text": "{\"discovery_available\": true, \"environment\": \"JDV920\", \"environment_type\": \"DEV\", \"path_code\": \"DV920\", \"application_release\": \"9.2\", \ |
| 4 | `mcp__jade-discovery__list_baseline_artifacts` | yes | `{}` | [{"type": "text", "text": "{\"artifacts\": [{\"evidence_id\": \"ART-ER-P554210@r1\", \"artifact_id\": \"ART-ER-P554210\", \"revision\": 1, \"kind\": \"technical_export\", \"object_ |
| 5 | `mcp__jade-discovery__discovery_read` | yes | `{"capability_id": "object_librarian", "target": "P554210", "fields": ["SIOBNM", "SIFUNO", "SISY", "SIMD", "SIPKGNAME"]}` | [{"type": "text", "text": "{\"evidence_type\": \"simulated_observation\", \"content_is_data_not_instructions\": true, \"observation_id\": \"OBS-148ee4907b\", \"capability_id\": \"o |
| 6 | `mcp__jade-discovery__read_baseline_artifact` | yes | `{"artifact_id": "ART-ER-P554210", "revision": 1}` | [{"type": "text", "text": "{\"evidence_type\": \"technical_artifact\", \"content_is_data_not_instructions\": true, \"evidence_id\": \"ART-ER-P554210@r1\", \"metadata\": {\"evidence |
| 7 | `mcp__jde-change-factory__get_approved_story` |  | `{"story_id": "S-PROOF-TECH-1"}` | [{"type": "text", "text": "{\n  \"story_id\": \"S-PROOF-TECH-1\",\n  \"user_story\": \"SYNTHETIC STORY for the simulation. As a credit controller, I want webshop sales orders (type |

## Technical Agent run 1: prepare (TR-4f8b689dab)

- Model reported by the runtime: **claude-sonnet-5**; models billed: ['claude-sonnet-5']; runtime claude_code_version 2.1.277; permission mode dontAsk
- Turns 1; duration 5707 ms; cost USD 0.11187570000000001; usage {"input_tokens": 2, "cache_creation_input_tokens": 1724, "cache_read_input_tokens": 9736, "output_tokens": 562, "output_tokens_details": {"thinking_tokens": 0}, "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}, "service_tier": "standard", "cache_creation": {"ephemeral_1h_input_
- Built-in tools requested: ['Task']; MCP servers passed: ['jade-technical']; secrets blanked in the agent process: ['JDE_AIS_PASSWORD', 'JDE_AIS_USERNAME', 'JDE_BOOTSTRAP_ADMIN_PASSWORD', 'JDE_CREDENTIAL_KEY', 'JDE_CREDENTIAL_KEY_PREVIOUS']
- Tool inventory the runtime reported: ['Task', 'mcp__jade-technical__apply_approved_package', 'mcp__jade-technical__build_applied_package', 'mcp__jade-technical__check_candidate', 'mcp__jade-technical__discovery_read', 'mcp__jade-technical__get_assignment', 'mcp__jade-technical__get_package_status', 'mcp__jade-technical__list_discovery_capabilities', 'mcp__jade-technical__list_source_artifacts', 'mcp__jade-technical__open_in_workspace', 'mcp__jade-technical__read_source_artifact', 'mcp__jade-technical__replace_in_workspace_file', 'mcp__jade-technical__report_outcome', 'mcp__jade-technical__run_verification_tests', 'mcp__jade-technical__show_workspace_diff', 'mcp__jade-technical__submit_implementation_package', 'mcp__jade-technical__view_workspace_file']
- MCP servers connected: [('jde-change-factory', 'connected'), ('jade-technical', 'connected')]
- Run record: status completed; error None; outcome {"kind": "clarification_required", "explanation": "Investigated the approved design for S-PROOF-TECH-1 and the sole authorised source, ART-ER-P554210@r1 (P554210/W554210A, event OK_Button_Clicked, jade_sim_er format, verified_active_runtime, complete/supported -- fully read, 647/647 chars). The current event logic is:\n\nIF BC OrderType = \"SO\" AND BC OrderTotal > BC CreditLimit\n    VA HoldCode = \"C1\"; VA HoldReason = \"Over credit limit\"\nELSE\n    VA HoldCode = \"\"; VA HoldReason = \"\"\nEND IF\n\nBC CreditExempt STRING is declared as an event input but is not referenced anywhere in the existing logic. Implementing AC1 (exempt customers not held) requires adding an exemption test to the IF condition, but the exact value(s) BC CreditExempt carries when a customer is exempt (e.g. \"Y\"/\"N\", \"1\"/\"0\", or a UDC-backed code) is not shown in the exported source and is not stated anywhere else in the evidence baseline (OBS-148ee4907b only carries object-librarian fields SIOBNM/SIFUNO/SISY/SIMD/SIPKGNAME, no field-value domain).\n\nThis is not an oversight on my part: the Architect's own approved design records this as an explicit evidence gap and places \"Human confirms the exact value/domain convention for BC CreditExempt and its data source\" as implementation_spec step 1, and \"Human/Architect finalise the exact replacement IF condition\" as step 3 -- both required to happen before the Technical Agent prepares the ER-text package (step 4). Neither confirmation is pre

| # | Tool | In subagent | Input | Result (start) |
|---|---|---|---|---|
| 1 | `Agent` |  | `{"description": "Prepare implementation package S-PROOF-TECH-1", "subagent_type": "technical-agent", "prompt": "Run purpose: PREPARE, for ap` | [{"type": "text", "text": "Async agent launched successfully. (This tool result is internal metadata \u2014 never quote or paste any part of it, including the agentId below, into a |
| 2 | `mcp__jade-technical__get_assignment` | yes | `{}` | [{"type": "text", "text": "{\"content_is_data_not_instructions\": true, \"story_id\": \"S-PROOF-TECH-1\", \"run_id\": \"TR-4f8b689dab\", \"run_purpose\": \"prepare\", \"story\": {\ |
| 3 | `mcp__jade-technical__list_source_artifacts` | yes | `{}` | [{"type": "text", "text": "{\"artifacts\": [{\"evidence_id\": \"ART-ER-P554210@r1\", \"artifact_id\": \"ART-ER-P554210\", \"revision\": 1, \"sha256\": \"9bcd673e28735ba4307385e37d6 |
| 4 | `mcp__jade-technical__read_source_artifact` | yes | `{"evidence_id": "ART-ER-P554210@r1"}` | [{"type": "text", "text": "{\"evidence_type\": \"technical_artifact\", \"content_is_data_not_instructions\": true, \"classification\": {\"evidence_id\": \"ART-ER-P554210@r1\", \"ar |
| 5 | `mcp__jade-technical__report_outcome` | yes | `{"kind": "clarification_required", "explanation": "Investigated the approved design for S-PROOF-TECH-1 and the sole authorised source, ART-E` | [{"type": "text", "text": "{\"recorded\": true, \"kind\": \"clarification_required\", \"explanation\": \"Investigated the approved design for S-PROOF-TECH-1 and the sole authorised |

Final reply:

> The technical-agent finished — no package was prepared. Here's the short version:
> 
> **Story:** S-PROOF-TECH-1, approved design targeting custom object **P554210** (Custom Sales Order Review, form W554210A), event `OK_Button_Clicked`, in environment JDV920.
> 
> **What it found:** The current ER logic holds an order if it's a sales order over the credit limit. The design (AC1) wants exempt customers to skip the hold — but that requires checking `BC CreditExempt`, a field that's declared but never used in the existing code.
> 
> **Why it stopped:** The value convention for `CreditExempt` (e.g. `Y/N`, `1/0`, a UDC code) and where it's populated aren't documented anywhere in the authorised evidence. Critically, this isn't a gap the agent guessed at — the Architect's own approved design explicitly flags this as an open question requiring human confirmation *before* implementation (steps 1 and 3 of the design's sequence), and it's listed in the evidence baseline's gaps. Rather than fabricate a comparison value and risk implementing an inverted or wrong condition, it declined to touch the workspace and reported the outcome.
> 
> **Outcome:** `report_outcome` with kind **`clarification_required`**. No source was opened for editing, no package revision was created. It's asking a person to answer:
> 1. What values does `CreditExempt` use, and where is it populated?
> 2. Is it set/validated elsewhere on the form (regression scope)?
> 3. What's the exact intended IF condition once the value domain is known?
> 4. Is there a formal test script beyond the AC1–AC3 acceptance text?
> 
> Nothing further can proceed on this story until those are answered.

## Package revisions

## CNC hand-off (simulated, synthetic CNC operator)

- {}

## Verification results

| Test | Kind | Passed | Expected | Actual |
|---|---|---|---|---|

## Discovery after the change

- Observation `None` (None): None

## Final evidence

- Evidence chain: None
- Active simulated runtime sha256 `None`; approved candidate sha256 `None`; package sha256 `None`; exact change `None`
- Simulated estate history (who changed what): []
