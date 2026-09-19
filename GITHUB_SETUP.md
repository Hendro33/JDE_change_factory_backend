# Putting this into GitHub, and working from there

Two repositories, one for each project:

| Repo | What goes in it | Language |
|---|---|---|
| e.g. `jde-change-factory-engine` | the Python engine — agents, MCP server, gates | Python |
| e.g. `jde-change-factory-ui` | the React front end | TypeScript |

Substitute your own repo names below.

---

## Why two repos and not one

They have different languages, different build tools, different release
cadences, and — importantly — different risk profiles. The engine can
write to JD Edwards; the front end cannot and never will. Keeping them
apart means the thing with JDE write access has a small, reviewable
history of its own.

The seam between them is the REST contract already written down in
`src/services/api.ts` (`API_ENDPOINTS`). That file is the thing to keep
in step across the two repos.

---

## One-time setup

You need Git installed (`git --version`). On Windows, Git for Windows;
on Mac, it comes with Xcode command line tools.

### The Python engine

Unzip the starter kit, then from inside the `jde-change-factory-starter`
folder:

```
git init
git branch -M main
git add .
git commit -m "Initial commit: JDE Change Factory engine

Three-phase gated pipeline with Gate 2 backlog approval, exact-change
approval, scope enforcement and hash-chained evidence."
git remote add origin https://github.com/YOUR-ORG/jde-change-factory-engine.git
git push -u origin main
```

Before that first push, check what you are about to commit:

```
git status
```

You should NOT see `scope.json`, `backlog/`, `changes/`, `evidence/`
or `.venv/`. The `.gitignore` excludes them — those hold real customer
records and engagement configuration, and do not belong in a shared
repo. `scope.example.json` is the template and does belong.

### The React front end

From inside the `jde-change-factory-ui` folder:

```
git init
git branch -M main
git add .
git commit -m "Initial commit: JDE Change Factory front end

React + TypeScript + Vite prototype. Mock service layer behind a typed
API contract, customer-scoped, no backend dependency yet."
git remote add origin https://github.com/YOUR-ORG/jde-change-factory-ui.git
git push -u origin main
```

`git status` here should not show `node_modules/` or `dist/`.

### If the push asks for a password

GitHub stopped accepting account passwords over HTTPS. Either:

- install the [GitHub CLI](https://cli.github.com) and run `gh auth login`
  once — it handles credentials for you and is the easiest route; or
- use SSH remotes (`git@github.com:YOUR-ORG/repo.git`) with an SSH key.

Do not paste a personal access token into a chat window, including this
one. Set it up locally with `gh auth login` or your OS keychain.

---

## Working from GitHub after that

Open a terminal in either repo and run `claude`. Claude Code reads the
whole repository, so you no longer need to describe the code or paste
files — you can say things like:

- "Add the `HttpChangeFactoryApi` implementation for the FastAPI backend."
- "Make `scope.json` per-customer, matching the tenancy model in the UI README."
- "Why does `prove_the_gate.py` fail on step 3?"

It edits files in place. Review with `git diff`, then commit — either
yourself, or by asking Claude Code to do it.

### A suggested way of working

```
git checkout -b add-http-api-client
# work with Claude Code
git diff                      # read what changed before trusting it
git add -A && git commit -m "Add HTTP API client"
git push -u origin add-http-api-client
```

Then open a pull request on GitHub. For a two-person project this may
feel heavy, but for anything that touches the approval gates or the
scope checks it is worth it — those are the parts where a quiet mistake
matters, and a PR diff is the cheapest possible review.

### Worth protecting

In the engine repo, consider requiring a PR for changes to:

```
mcp_server/jde_mcp_server/backlog.py     Gate 2
mcp_server/jde_mcp_server/approval.py    exact-change approval
mcp_server/jde_mcp_server/scope.py       universal + engagement scope rules
.claude/hooks/approve_writes.py          the write approval hook
```

These four files are the safety model. Everything else can move fast.

Also add a CI check that runs `python3 prove_the_gate.py` on every pull
request — it already exits non-zero on failure, so it works as a gate
with no extra work. If someone weakens a control, the build fails before
anyone has to notice it by eye.

---

## What changes about our workflow

Once this is in GitHub, I can stop handing over zip files. Instead:

- **Claude Code, in your local clone** — for anything that touches code.
  It sees the whole repo, so no more pasting or re-explaining context.
- **This chat** — better suited to design work: the document, decisions
  about architecture, reviewing an approach before it gets built.

If you want me to read the repos directly from a chat like this one in
future, that needs a GitHub connector, and there isn't one available in
this workspace's directory right now. Worth checking again later, or
asking whoever administers your Claude setup whether one can be added.
