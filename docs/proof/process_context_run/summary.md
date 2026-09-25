# Real-model demonstration: process context in refinement and design

REAL model runs through the intended runtime (claude_agent_sdk -> Claude CLI). The framework is the SYNTHETIC fixture (SYN- ids, not APQC content). JDE is SIMULATED. Approvals and review decisions are made by a synthetic test identity ("Proof Reviewer (synthetic)"), never the owner.

- Run 2026-09-25T06:39:13.461686+00:00 to 2026-09-25T06:42:26.160302+00:00 (UTC); story `S-BW-WRITEOFF`
- Backend `5e6db55`, frontend `306a365`; 2.1.282 (Claude Code); claude-agent-sdk 0.2.157

## 1. Refinement process analysis (real model)

- Model (reported by the runtime): **claude-sonnet-5**; billed models ['claude-sonnet-5']; turns 18; cost USD 0.1127486; error False
- Claude Code runtime 2.1.277; allowed tools ['Task', 'mcp__jade-process__get_story', 'mcp__jade-process__browse_framework', 'mcp__jade-process__search_framework', 'mcp__jade-process__submit_process_findings']
- MCP tools in the runtime's inventory: ['mcp__jade-process__browse_framework', 'mcp__jade-process__get_story', 'mcp__jade-process__search_framework', 'mcp__jade-process__submit_process_findings']
- Tool calls, in order: ['mcp__jade-process__get_story', 'mcp__jade-process__browse_framework', 'mcp__jade-process__browse_framework', 'mcp__jade-process__browse_framework', 'mcp__jade-process__search_framework', 'mcp__jade-process__search_framework', 'mcp__jade-process__browse_framework', 'mcp__jade-process__browse_framework', 'mcp__jade-process__search_framework', 'mcp__jade-process__search_framework', 'mcp__jade-process__browse_framework', 'mcp__jade-process__browse_framework', 'mcp__jade-process__search_framework', 'mcp__jade-process__search_framework', 'mcp__jade-process__browse_framework', 'mcp__jade-process__browse_framework', 'mcp__jade-process__submit_process_findings']

Status: **completed** 

Summary: S-BW-WRITEOFF maps to Finance's inventory value adjustment posting process and, more loosely, warehouse work and reason-based reporting; it is missing an agreed threshold value, segregation-of-duties approval control, audit trail, on-hand quantity validation, and reconciliation to the general ledger, plus acceptance criteria testing approval blocking, rejection handling, audit trail visibility, and quantity limits.

Suggested processes (validated against the exact framework version):

- `SYN-6.1.2` Manage finance > Process credits and value adjustments > Post the inventory value adjustment (high): The story's AC2 requires that write-offs above a threshold be approved before posting; this is a Finance process for posting an inventory value adjustment, which is exactly what a write-off is.
- `SYN-4.1` Deliver bicycles > Plan warehouse work (medium): Recording a stock write-off with item, quantity and reason is warehouse-side operational work that belongs under the warehouse work process group.
- `SYN-6.2.1` Manage finance > Report financial performance > Report returns by dealer, model and reason (low): The story's goal ("so that Finance can see what was written off and why") implies reason-coded reporting analogous to the existing reason-based reporting process for returns.

Missing requirements:

- The approval threshold value and its basis must be agreed with Finance.
- A write-off must not be recorded for a quantity greater than the item's current on-hand inventory balance.
- Only authorised warehouse supervisors must be able to record a stock write-off.

Missing controls:

- An approver other than the person who recorded the write-off must approve write-offs above the threshold.
- The identity of the person who recorded the write-off and the identity and timestamp of the approver must be retained on the write-off record to provide an audit trail.
- Every posted write-off's inventory value adjustment must be reconciled to the general ledger as part of Finance's period-end reconciliation.

Missing acceptance criteria:

- A write-off above the threshold cannot post until it is approved by someone other than the person who recorded it.
- A rejected write-off is not posted and does not adjust the inventory balance.
- Each write-off record shows who recorded it, who approved it (if applicable), and when, for audit purposes.
- A write-off cannot be recorded for a quantity exceeding the item's current on-hand balance.

## 2. Reviewer decision (synthetic identity, through the API)

Mapping revision 1 (confirmed) by Proof Reviewer (synthetic):

- `PF-ebb071cb@v1:SYN-6.1.2` sha256 b41e895da020... Post the inventory value adjustment
- `PF-ebb071cb@v1:SYN-4.1` sha256 5f81b2483f85... Plan warehouse work
- `PF-ebb071cb@v1:SYN-6.2.1` sha256 5c65e333dfa4... Report returns by dealer, model and reason

Proposed story change (diff shown to the reviewer):

```diff
+ [business_rules] Control: An approver other than the person who recorded the write-off must approve write-offs above the threshold.
+ [business_rules] Control: Every posted write-off's inventory value adjustment must be reconciled to the general ledger as part of Finance's period-end reconciliation.
+ [business_rules] Control: The identity of the person who recorded the write-off and the identity and timestamp of the approver must be retained on the write-off record to provide an audit trail.
+ [business_rules] Requirement: A write-off must not be recorded for a quantity greater than the item's current on-hand inventory balance.
+ [business_rules] Requirement: Only authorised warehouse supervisors must be able to record a stock write-off.
+ [business_rules] Requirement: The approval threshold value and its basis must be agreed with Finance.
  [acceptance_criteria] AC1: A write-off records item, quantity and reason
  [acceptance_criteria] AC2: Write-offs above the threshold need approval before posting
+ [acceptance_criteria] AC3: A rejected write-off is not posted and does not adjust the inventory balance.
+ [acceptance_criteria] AC4: A write-off above the threshold cannot post until it is approved by someone other than the person who recorded it.
+ [acceptance_criteria] AC5: A write-off cannot be recorded for a quantity exceeding the item's current on-hand balance.
+ [acceptance_criteria] AC6: Each write-off record shows who recorded it, who approved it (if applicable), and when, for audit purposes.
```

Story revision 2 saved by Proof Reviewer (synthetic) (2026-09-25T06:39:58.277749+00:00), applying 10 finding(s); process references: mapping revision 1 ['SYN-6.1.2@v1', 'SYN-4.1@v1', 'SYN-6.2.1@v1']

## 3. Architect design (real model)

- Model (reported by the runtime): **claude-sonnet-5**; billed models ['claude-sonnet-5']; turns 2; cost USD 0.2697397; error False
- Claude Code runtime 2.1.277; allowed tools ['Task', 'mcp__jde-change-factory__get_approved_story', 'mcp__jde-change-factory__resolve_without_change', 'mcp__jde-change-factory__propose_change', 'mcp__jade-discovery__list_discovery_capabilities', 'mcp__jade-discovery__discovery_read', 'mcp__jade-discovery__list_baseline_artifacts', 'mcp__jade-discovery__read_baseline_artifact', 'mcp__jade-discovery__get_process_context']
- MCP tools in the runtime's inventory: ['mcp__jade-discovery__discovery_read', 'mcp__jade-discovery__get_process_context', 'mcp__jade-discovery__list_baseline_artifacts', 'mcp__jade-discovery__list_discovery_capabilities', 'mcp__jade-discovery__read_baseline_artifact', 'mcp__jde-change-factory__get_approved_story', 'mcp__jde-change-factory__propose_change', 'mcp__jde-change-factory__resolve_without_change']
- Tool calls, in order: ['Agent', 'mcp__jde-change-factory__get_approved_story', 'mcp__jade-discovery__get_process_context', 'mcp__jade-discovery__list_discovery_capabilities', 'mcp__jade-discovery__list_baseline_artifacts', 'mcp__jade-discovery__discovery_read', 'mcp__jade-discovery__read_baseline_artifact', 'mcp__jade-discovery__read_baseline_artifact', 'mcp__jade-discovery__get_process_context']

Stage: **done** 

- Route: **Clarification Required** (confidence 0.85); objects []
- Existing functionality: 
- get_process_context called: True; recorded in the baseline as consulted: True
- Design baseline `BL-5b1f1502af` (current) rests on process fingerprint 23269efae078c210... (mapping revision 1, map versions {'as_is': None, 'to_be': None}); the story's fingerprint now: 23269efae078c210...

Architect's process findings:

- affected processes: PF-ebb071cb@v1:SYN-6.1.2 (Post the inventory value adjustment): where the write-off posting and its GL reconciliation control belong, but no as-is/to-be map exists to design against.
- affected processes: PF-ebb071cb@v1:SYN-4.1 (Plan warehouse work): where write-off recording by the warehouse supervisor originates.
- affected processes: PF-ebb071cb@v1:SYN-6.2.1 (Report returns by dealer, model and reason): a confirmed process with no corresponding acceptance criterion in the current story.
- missing acceptance criteria: AC7: A user without the warehouse-supervisor role attempting to record a write-off is rejected with an authorisation error and no record is created.
- missing acceptance criteria: AC8: At period-end, all posted write-off inventory value adjustments can be reconciled to the GL, and any unreconciled items are flagged for review.
- missing acceptance criteria: AC9: Write-off records can be reported and filtered by dealer, model, and reason.
- missing controls: A control confirming that only users holding the warehouse-supervisor role/security profile can create a write-off record.
- missing controls: A period-end control confirming that every posted write-off inventory value adjustment has been reconciled to the GL.
- missing requirements: The write-off recording function must restrict entry to authorised warehouse supervisors only, per the story's own business rules, but this is not stated as a testable requirement.
- missing requirements: Posted write-offs must be reconciled to the general ledger as part of period-end reconciliation, per the story's GL reconciliation control, but this is not stated as a testable requirement.
- missing requirements: Write-off records must be reportable by dealer, model, and reason to satisfy confirmed process node SYN-6.2.1.

Gaps recorded in the baseline:

- missing: The write-off approval threshold value and its basis are not yet agreed with Finance, per the story's own business_rules.
- unavailable: Discovery is disabled for this company's current profile revision; no DEV inventory-adjustment, approval/workflow, security, or configuration facts could be confirmed.
- missing: As-is/to-be process maps are empty for all three confirmed process nodes (SYN-6.1.2, SYN-4.1, SYN-6.2.1).
- conflict: The story's business_rules state segregation-of-duties, GL reconciliation, and role-restriction controls, but acceptance criteria AC1-AC6 test none of the role-restriction or GL reconciliation controls, and no AC covers write-off reporting by reason/model despite SYN-6.2.1 being a confirmed relevant process.
- missing: Unsupported citation: The story's acceptance criteria are AC1 (record item/qty/reason), AC2 (threshold approval before posting), AC3 (rejected write-off not posted), AC4 (approver ≠ recorder above threshold), AC5 (qty ≤ on-hand), AC6 (audit trail), and the threshold value itself is explicitly unresolved with Finance
- missing: Unsupported citation: Discovery is not enabled for this company's current profile revision; no live DEV facts could be confirmed
- missing: Unsupported citation: The approved object_librarian target P554210 was blocked when read (discovery_read returned blocked:true)
- missing: Unsupported citation: Confirmed process nodes for this story are PF-ebb071cb@v1:SYN-6.1.2 (Post the inventory value adjustment), SYN-4.1 (Plan warehouse work), and SYN-6.2.1 (Report returns by dealer, model and reason), confirmed by Proof Reviewer (synthetic) on 2026-09-25
- missing: Unsupported citation: As-is/to-be process maps for all three confirmed nodes are empty (maps: {}, as_is: null, to_be: null)
- missing: Customer environment not investigated: discovery is not enabled for this company's current profile revision
- missing: object_librarian on P554210 was not read: discovery is not enabled for this company's current profile revision
