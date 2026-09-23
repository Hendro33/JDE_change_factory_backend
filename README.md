# JDE AI-Driven Change Factory — starter kit

**New to this kit? Start with `GETTING_STARTED.md`, not this file.** It's
a literal, copy-paste-able script from unzipping this folder to a first
working test, including installing everything and proving the safety
controls actually work on your machine. This README is the deeper
reference once you're up and running.

This is a working scaffold for the MVP described in the design document
(v11), Sections 3.5, 6.5, 7, 8 and 10. It runs today in **mock mode** —
no JDE access required — so you can build and rehearse the whole pipeline
before environment access is sorted out, and then flip one setting to
point it at a real AIS Server once the validation spikes are done.

It does not replace the design document; it is the first concrete
artefact it describes. **The most important thing to understand before
touching this code: the pipeline is built as three separately-provable
phases with a hard gate between each (Section 3.5), and every write
also has to clear this engagement's own scope file (Appendix D/E) on
top of that.** Phase 3 (the Architect and build agents) cannot write to
JDE or run a test for a story that hasn't been through Phase 2, and it
cannot write to anything the story's own company hasn't explicitly
authorised in its engagement scope, even for an approved story. None of this is a
suggestion in a prompt — it's enforced in `backlog.py` and `scope.py`,
which the write and test-execution tools check before doing anything.
Discovery stays unrestricted throughout, since it's read-only and
Phase 1 genuinely needs it too.

**Before you can run a real story, the story's company needs a saved
engagement scope and approval policy** (Jade: Admin > ERP / JDE
Landscape; the stored format is shown in `scope.example.json`) — there
is no "no restrictions configured" default; `set_processing_option`
refuses to run at all without them, and only a person whose role the
policy allows can approve an exact change.

## The three phases, and where each one lives

| Phase | What it does | Where |
|---|---|---|
| 1. Capture & Refine | Receive → Improve → Check turn a raw draft into a quality-gated story, with business impact and a rough complexity signal attached | `.claude/agents/receive-agent.md`, `improve-agent.md`, `check-agent.md` |
| 2. Backlog & Approval | A human reviews and approves or rejects — no agent does this | `backlog_review.py` (run it yourself) |
| 3. Architect & Build | Analyses an **approved** story and executes the change | `.claude/agents/architect.md`, `functional-agent.md` |

## What's here

```
mcp_server/
  jde_mcp_server/
    config.py                  Env-var driven settings, incl. MOCK_MODE
    ais_client.py               AIS REST calls -- mocked until you fill in
                                the validated Form Service Request (see below)
    backlog.py                   The Phase 2 gate as code (Section 3.5) --
                                read this file to see the actual control
    evidence.py                 capture_evidence -- one JSON file per story
    server.py                   Registers the MCP tools, incl. propose_to_backlog
                                and get_approved_story
backlog_review.py                Human-run CLI for Phase 2/exact-change approval --
                                list/show/approve/reject, list-changes/show-change/
                                approve-change/reject-change
prove_the_gate.py                Run this first -- proves every safety control
                                actually works, in one command (GETTING_STARTED.md)
GETTING_STARTED.md               Start here if this is your first time -- a literal,
                                step-by-step script from unzipping to a first test
scope.example.json               Shape of one company's stored engagement scope (Appendix D.2/E.2),
                                which the execution gate enforces -- normally edited in Jade
.claude/
  agents/
    receive-agent.md            Section 5.3.1 -- no JDE access
    improve-agent.md            Section 5.3.2 -- read-only Discovery, populates
                                business_impact + rough_complexity_signal
    check-agent.md               Section 5.3.3 -- only tool is propose_to_backlog
    architect.md                Section 4.3 -- calls get_approved_story first
    functional-agent.md         Section 4.4 -- Discovery + the one write tool
  hooks/
    approve_writes.py           Section 8.1 -- PreToolUse approval gate
  settings.json                 Wires the hook to the write-classified tools
.mcp.json                       Registers the MCP server with Claude Code
```

Not included yet, on purpose: a Technical Agent subagent (Section 4.5)
and its tools, and the closure/write-back tool from Section 4.8/7.8.
Add these once the Technical Agent feasibility spike (Section 10.2)
has validated a capability, and once you're ready to close the loop
back to the originating source.

## Try the gate before you try anything else

Before building a single story end to end, prove the gate actually
gates, in mock mode, right now:

```bash
cd mcp_server && pip install -e . && cd ..

python3 -c "
import sys; sys.path.insert(0, 'mcp_server')
from jde_mcp_server.ais_client import client
from jde_mcp_server.backlog import propose_to_backlog, StoryNotApproved

propose_to_backlog('S-TEST', 'test story', {'financial_impact': 'Low'}, 'Low')
try:
    client.set_processing_option('S-TEST', 'P4210', 'ZJDE0001', 'PDOCTYPE', 'SO')
    print('BUG: this should have been blocked')
except StoryNotApproved as e:
    print('Correctly blocked:', e)
"

python3 backlog_review.py list
python3 backlog_review.py approve S-TEST "your name" "test approval"

python3 -c "
import sys; sys.path.insert(0, 'mcp_server')
from jde_mcp_server.ais_client import client
print('Now allowed:', client.set_processing_option('S-TEST', 'P4210', 'ZJDE0001', 'PDOCTYPE', 'SO'))
"
rm -rf backlog  # clean up the test story
```

If that all runs as described, the gate is real, not aspirational.

## Week one, step by step

These map onto Section 10.2, reordered around proving each phase before
building the next. Do not build Phase 3 before you've proven Phase 1 —
that was the mistake the first version of this starter kit made.

**Step 1 — Prove Phase 1 in isolation (no JDE access needed).**
Run 10–15 real or realistic draft stories through Receive → Improve →
Check only, in Claude Code:
> "Use the receive-agent, then the improve-agent, then the check-agent
> to process this raw request: [paste a real Topdesk ticket]"

Check by hand: does Check bounce vague stories back with specific,
named failures? Are the business_impact fields honest (blank where the
source said nothing) rather than confidently invented? Do NOT move on
until you'd trust this output going into a real backlog.

**Step 2 — Build the Phase 2 habit (no JDE access needed).**
Once a story reaches the backlog, actually run `backlog_review.py list`
and `show`, and approve or reject it as a human would. Do this for real
stories from step 1. The exit criterion is behavioural, not technical:
someone has genuinely done this review, not just confirmed the script
runs.

**Step 3 — JDE access (not this repo's job).**
Get an AIS Server and Orchestrator Studio reachable against a DEV
pathcode, with a narrowly scoped service account (Section 8.2). Record
the exact Tools Release.

**Step 4 — Functional validation spike (manual, no AI).**
Record and test a real Form Service Request against the Processing
Option Revisions form; paste the result into `FSR_SET_PROCESSING_OPTION`
in `ais_client.py`.

**Step 5 — Technical Agent feasibility spike (manual, no AI, parallel with step 4).**
Section 7.6/7.7 — pick one Web OMW object type your Tools Release
supports and do the same recording exercise.

**Step 6 — Only now, wire up Phase 3 end to end.**
With approved stories from step 2 sitting in the backlog, run the
Architect and Functional Agent against one of them. This is the first
point where all three phases are connected.

**Step 7 — Flip mock mode off** once steps 4–5 are done, same as
before: set `JDE_MCP_MOCK_MODE=false` and fill in the AIS credentials.

**Step 8 — Run one real pilot story end to end**, all three phases,
against real JDE DEV.

## A note on the approval hook

Claude Code's hook JSON protocol changes across versions faster than
this kind of document does. Treat `approve_writes.py` and
`.claude/settings.json` as a starting point: check them against
whatever Claude Code version you're actually running before trusting
the approval gate in anything beyond local testing, and definitely
before a real customer pilot.

## A note on the mcp package version

This code targets whatever `mcp` SDK version is current when you set
this up — `server.py` tries the newer `MCPServer` class first and
falls back to the older `FastMCP` name so it works either way. Run
`pip show mcp` if something doesn't import, and check that version's
own docs for anything that's moved since.
