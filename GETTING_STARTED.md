# Getting Started — From Zip File to First Working Test

This is a literal, step-by-step script. Follow it in order. Every command
is written to be copied and pasted exactly as shown. Where Mac/Linux and
Windows commands differ, both are given.

Budget about 30–45 minutes for the first run-through.

---

## Before you start

You need:

- [ ] A Mac, Windows, or Linux computer
- [ ] A terminal application (Terminal on Mac, PowerShell on Windows, any terminal on Linux)
- [ ] About 30–45 minutes with no interruptions the first time through

You do **not** need to know how to code. You do need to be comfortable
copying a line of text and pasting it into a terminal window, and reading
the output.

---

## Step 1 — Check whether you already have Python

Open a terminal and run:

**Mac/Linux:**
```
python3 --version
```
**Windows (PowerShell):**
```
python --version
```

You need to see **3.10** or higher (e.g. `Python 3.11.4`). If you see an
error like "command not found", or a version below 3.10, install Python
from **https://www.python.org/downloads/** — download the installer for
your operating system, run it, and accept the defaults. On the Windows
installer, make sure the box that says **"Add Python to PATH"** is
checked before you click Install. Then close and reopen your terminal
and run the version check again.

---

## Step 2 — Check whether you already have Claude Code

Run:
```
claude --version
```

If you see a version number, skip to Step 3. If you see "command not
found", install it:

**Mac, Linux, or WSL:**
```
curl -fsSL https://claude.ai/install.sh | bash
```
**Windows PowerShell:**
```
irm https://claude.ai/install.ps1 | iex
```

Close and reopen your terminal, then confirm:
```
claude --version
claude doctor
```
`claude doctor` should report everything is fine. If it doesn't, fix
whatever it flags before continuing — Claude Code needs to genuinely
work before the rest of this matters.

You'll also need a paid Claude account (Pro, Max, Team, or Enterprise)
signed in — the free Claude.ai plan doesn't include Claude Code. The
first time you run `claude` in a folder, it opens your browser to sign
you in.

---

## Step 3 — Unzip the starter kit

Move the zip file (`jde-change-factory-starter-v4.zip`, or whatever the
latest one you were given is called) somewhere you'll keep working from
— for example a folder called `ConsultIQ` in your Documents.

Unzip it:
- **Mac:** double-click the zip file.
- **Windows:** right-click the zip file → "Extract All..." → Extract.
- **Linux:** `unzip jde-change-factory-starter-v4.zip`

You should now have a folder called `jde-change-factory-starter`.

---

## Step 4 — Open a terminal inside that folder

This is the step people most often get wrong — everything below only
works if your terminal is *inside* this specific folder.

**Mac:** open the folder in Finder, right-click inside it (not on a
file), choose **"New Terminal at Folder"**. If that option isn't there,
open Terminal normally and type `cd ` (with a trailing space), then drag
the folder from Finder into the Terminal window, then press Enter.

**Windows:** open the folder in File Explorer, hold Shift and
right-click inside it (not on a file), choose **"Open PowerShell window
here"**.

**Linux:** `cd path/to/jde-change-factory-starter`

Confirm you're in the right place:
```
ls
```
(Windows: `dir`)

You should see, among other things: `README.md`, `backlog_review.py`,
`prove_the_gate.py`, `scope.example.json`, a `mcp_server` folder, and a
`.claude` folder. If you don't see these, you're in the wrong folder —
go back and re-navigate.

**Keep this terminal window open and stay in this folder for every step
below.** If you close it, come back to Step 4 to get back here.

---

## Step 5 — Create a private space for this project's Python packages

This keeps this project's software separate from anything else on your
computer, so nothing conflicts.

```
python3 -m venv .venv
```
(Windows: `python -m venv .venv`)

Now switch into it:

**Mac/Linux:**
```
source .venv/bin/activate
```
**Windows PowerShell:**
```
.venv\Scripts\Activate.ps1
```

You'll know it worked because your terminal prompt now starts with
`(.venv)`. **You need to run this "activate" command again every time
you close and reopen your terminal for this project** — everything
after this step assumes you've done it.

If Windows shows a red error about "running scripts is disabled", run
this once, then retry the activate command:
```
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

---

## Step 6 — Install the project's dependencies

```
cd mcp_server
pip install -e .
cd ..
```

This will print a fair amount of text and end with something like
`Successfully installed jde-mcp-server-0.1.0 ...`. If it ends with a
red error instead, copy the last 10 lines of the error and get help
before continuing.

**If you see an error mentioning "externally-managed-environment" or
"PEP 668":** this means Step 5's virtual environment isn't actually
active. Go back to Step 5, run the `source .venv/bin/activate` (or
Windows `.venv\Scripts\Activate.ps1`) command again, confirm your
prompt now starts with `(.venv)`, then retry Step 6.

---

## Step 7 — Prove the safety controls actually work, before anything else

This is the most important step in this whole document. Run:

```
python3 prove_the_gate.py
```

You should see six lines, each starting with **PASS**, ending with:
```
ALL CHECKS PASSED -- the safety gate works as designed on this machine.
```

This script uses its own temporary test data and cleans up after
itself — it's safe to run now, before you've set anything up for real,
and safe to re-run any time you want reassurance. If anything says
**FAIL**, stop here and get help before going further — nothing past
this point should be trusted until this passes cleanly.

---

## Step 8 — Set up the company's engagement scope

Each company's engagement scope says exactly what may be touched for
that company's stories — see the design document's Appendix D
(Configuration Guidelines) and Appendix E (Development Guidelines) for
the full explanation. Nothing will run for a company until it has one.

A company Admin sets it in Jade under **Admin > ERP / JDE Landscape**.
It is stored as one file per company (the shape is shown in
`scope.example.json`) and read by the execution gate for that
company's stories only. Fill in, at minimum:

- the DEV environment binding, with isolation confirmed only once it
  has actually been checked
- `approved_versions` — at least one entry: the capability it uses, the
  exact application, a version name **that does not start with XJDE or
  ZJDE**, which processing option(s) on it are allowed, and what values
  they're allowed to be set to
- the approval policy — which roles may approve an exact change, and
  for how many hours an approval stays valid
- for a capability still at Needs spike, a dated spike experiment

**Until you have a real JDE story to run, it's fine to leave these as
clearly-fake placeholder values** — the next steps run in "mock mode",
which never touches a real JDE system, so nothing here can cause harm
yet. Fill it in properly before Step 3 of the design document's Section
10.2 build sequence (the point where you connect to a real AIS Server).

---

## Step 9 — Open the project in Claude Code

Still in the same terminal, in the same folder:
```
claude
```

The first time, this opens your browser to sign in. Once signed in,
you're in a Claude Code session with this project open. Claude Code
automatically reads the `.mcp.json` file in this folder, which tells it
about the JDE MCP server — you don't need to configure anything.

Confirm the tools loaded by typing this at the Claude Code prompt:
```
What MCP tools do you have available from jde-change-factory?
```

You should see it list tools including `propose_to_backlog`,
`get_approved_story`, `set_processing_option`, `resolve_without_change`,
and others. If it says it has no MCP tools, close Claude Code, confirm
you're still in the right folder (Step 4), and confirm Step 6 completed
without errors.

---

## Step 10 — Run your first story through Phase 1 (Capture & Refine)

Still inside the Claude Code session, paste a real (or realistic) raw
request — a Topdesk ticket, an email, a sentence someone said in a
meeting — and ask:

```
Use the receive-agent, then the improve-agent, then the check-agent to
process this raw request: [paste your ticket/note text here]
```

Watch what happens. A good outcome looks like: the story comes out the
other end either **proposed to the backlog** (if it was clear and
complete) or **bounced back with specific, named reasons** (if it
wasn't) — never silently accepted if it was vague. Try this with 3–4
different real requests, including at least one you know is genuinely
vague, to see the bounce-back behaviour for yourself.

---

## Step 11 — Review and decide in Phase 2 (Backlog & Approval)

This step is deliberately **not** done inside Claude Code — approving a
story is a human decision made outside the AI, on purpose (see the
design document, Section 3.5).

Open a **second terminal window**, navigate back to this same folder
(repeat Step 4), activate the virtual environment again (Step 5's
activate command), then:

```
python3 backlog_review.py list
```

This shows every story waiting for your decision. To see one in full:
```
python3 backlog_review.py show STORY_ID
```
(replace `STORY_ID` with the real ID shown in the list)

To approve it:
```
python3 backlog_review.py approve STORY_ID "Your Name" "why you approved it"
```

To reject it (a reason is required):
```
python3 backlog_review.py reject STORY_ID "Your Name" "why you rejected it"
```

---

## Step 12 — Run Phase 3 (Architect & Build) — in mock mode first

Back in your original Claude Code session:
```
Now act as the architect for STORY_ID, and if it routes to the
functional agent, carry that through too.
```

In mock mode, this exercises the entire pipeline — including the
Architect proposing an exact change and you approving it (Step 11's
script also has `list-changes`, `show-change`, and `approve-change`
commands, used the same way) — without ever touching a real JD Edwards
system. This is the safe way to rehearse the whole flow end to end
before Step 3/4/5 of the design document's Section 10.2, which is where
real JDE access gets connected.

---

## Step 13 — Look at what actually got recorded

Everything the pipeline does is written to plain files you can open
yourself:
- `backlog/STORY_ID.json` — the story and the human decision on it
- `changes/*.json` — every proposed and approved exact change
- `evidence/STORY_ID.json` — the full audit trail for that story

To double-check the evidence trail hasn't been tampered with, ask
Claude Code:
```
Run verify_evidence_chain for STORY_ID
```

---

## What to do next

- Repeat Steps 10–13 with several more real stories until you trust the
  output — this is the "prove Phase 1" and "prove Phase 2" exit
  criteria from the design document's Section 10.2.1/10.2.2.
- When you're ready to connect to a real JD Edwards DEV environment,
  go to the main `README.md` in this folder, "Week one, step by step",
  starting at Step 5 (JDE access) — that's the manual, no-AI validation
  spike this whole safety model depends on being done properly.
- If you ever want to re-confirm the safety controls after making any
  code changes, re-run `python3 prove_the_gate.py`.
