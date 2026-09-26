"""
The one way a Jade agent run starts: the customer's own AI connection, the
customer's assigned Start-up Packs, an isolated runtime, and a durable run
record.

    async with runtime.agent_run(company_id=..., driver=..., roles=[...]) as run:
        options = run.options(cwd=..., permission_mode=..., allowed_tools=[...], max_turns=...)
        async for event in run.stream(prompt, options):
            ...

  * Connection: ai.connection.resolve_for_run -- no fallback to the backend
    machine's ANTHROPIC_API_KEY, Claude Code login or default model.
  * Packs: ai.packs.snapshot, read ONCE here. The snapshot (revision + hash)
    is what the run uses and records; later edits cannot reach it.
  * Isolation: every run gets its own environment overrides (passed to the
    runtime subprocess only -- os.environ is never modified) and its own
    throw-away runtime configuration directory, so no inherited login,
    user settings, session or cache is used and concurrent runs for
    different customers share nothing. The runtime's own init report must
    name ANTHROPIC_API_KEY as the key source and the configured model, or
    the run stops before any model request is made.
  * Tools: a pack can only narrow what the driver (reviewed code) allows;
    agent_runtime.SCRUBBED_ENV, the project hooks and the backend's own
    tool checks still apply.

ClaudeAgentRuntime is the only adapter (the Claude Agent SDK driving the
Claude Code CLI). Company settings, packs, tool contracts and run records do
not depend on it: it receives a neutral RunSpec and yields neutral
RuntimeEvents.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional

from ..persistence.db import connection as db
from ..services import agent_runtime
from . import connection as ai_connection
from . import packs as ai_packs
from .connection import AiNotConfigured, ResolvedConnection

EXPECTED_KEY_SOURCE = "ANTHROPIC_API_KEY"

# Blanked in every run's runtime process: any other way of authenticating or
# choosing a provider/model that the host machine might carry.
_BLANKED = (
    "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_CUSTOM_HEADERS",
    "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_FOUNDRY_API_KEY",
)
# Every model alias the runtime could fall back to resolves to the run's main
# configured model; each subagent gets its own configured model explicitly.
_MODEL_VARS = ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
               "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL")


# Everything else inherited from the backend's own environment is neutralised
# (set to empty) in the runtime process: the SDK always starts from
# os.environ, and a host can carry credentials the runtime would otherwise
# prefer over the customer's key (proved with the real CLI: an inherited
# host session token was used while the runtime still REPORTED the API key
# as its source -- scripts/prove_runtime_isolation.py).
_INHERIT_EXACT = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ", "PWD",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
    "PYTHONPATH", "VIRTUAL_ENV", "SYSTEMROOT", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
}
_INHERIT_PREFIXES = ("LC_", "JDE_", "XDG_RUNTIME_DIR")


def _neutralised_host_env() -> dict[str, str]:
    import os

    return {k: "" for k in os.environ
            if k not in _INHERIT_EXACT and not k.startswith(_INHERIT_PREFIXES)}


class RuntimeMismatch(RuntimeError):
    """The runtime reported a credential source or model other than the configured one."""


# -- Neutral types -------------------------------------------------------------
@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    prompt: str
    tools: list[str]
    max_turns: Optional[int] = None
    model: Optional[str] = None


@dataclass
class RunSpec:
    model: str
    api_key: str = field(repr=False)
    base_url: str
    config_dir: str
    cwd: str
    permission_mode: str
    allowed_tools: list[str]
    disallowed_tools: list[str]
    max_turns: int
    max_budget_usd: float
    agents: list[AgentSpec]
    tool_servers: dict[str, Any]
    system_prompt_append: Optional[str] = None


@dataclass
class RuntimeEvent:
    kind: str  # "init" | "subagent_started" | "result" | "other"
    data: dict
    raw: Any = None


# -- The adapter ------------------------------------------------------------------
class ClaudeAgentRuntime:
    """Claude Agent SDK / Claude Code CLI."""

    name = "claude-agent-sdk"

    @staticmethod
    def environment(spec: RunSpec) -> dict[str, str]:
        env = {**_neutralised_host_env(), **agent_runtime.SCRUBBED_ENV, **{k: "" for k in _BLANKED},
               **{k: spec.model for k in _MODEL_VARS},
               "CLAUDE_CODE_SUBAGENT_MODEL": ""}
        env.update({
            "ANTHROPIC_API_KEY": spec.api_key,
            "ANTHROPIC_BASE_URL": spec.base_url,
            "CLAUDE_CONFIG_DIR": spec.config_dir,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # Subagents must finish inside the run: the CLI otherwise starts them
            # in the background by default and the run can end before they do.
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "DISABLE_AUTOUPDATER": "1",
        })
        return env

    def build_options(self, spec: RunSpec):
        import claude_agent_sdk as sdk

        # background=False: the driver needs each subagent's result before the
        # run ends (the CLI can otherwise launch it asynchronously and let the
        # main agent finish first -- observed with the real CLI).
        agents = {a.name: sdk.AgentDefinition(description=a.description, prompt=a.prompt, tools=list(a.tools),
                                              model=a.model or spec.model, maxTurns=a.max_turns, background=False)
                  for a in spec.agents}
        kwargs: dict[str, Any] = {}
        if spec.system_prompt_append:
            kwargs["system_prompt"] = {"type": "preset", "preset": "claude_code", "append": spec.system_prompt_append}
        return sdk.ClaudeAgentOptions(
            tools=list(agent_runtime.BASE_TOOLS), env=self.environment(spec), cwd=spec.cwd,
            permission_mode=spec.permission_mode, allowed_tools=spec.allowed_tools,
            disallowed_tools=spec.disallowed_tools, max_turns=spec.max_turns, max_budget_usd=spec.max_budget_usd,
            model=spec.model, fallback_model=None, agents=agents or None, mcp_servers=dict(spec.tool_servers),
            # Project settings only (the reviewed hooks in .claude/settings.json);
            # never the host user's settings or a local override.
            setting_sources=["project"], **kwargs)

    @staticmethod
    def translate(message) -> RuntimeEvent:
        kind = type(message).__name__
        if kind == "SystemMessage":
            data = getattr(message, "data", None) or {}
            sub = getattr(message, "subtype", "")
            if sub == "init":
                return RuntimeEvent("init", {"model": data.get("model"), "credential_source": data.get("apiKeySource"),
                                             "runtime_version": data.get("claude_code_version")}, message)
            if sub == "task_started":
                return RuntimeEvent("subagent_started", {"name": data.get("subagent_type")}, message)
        elif kind == "ResultMessage":
            return RuntimeEvent("result", {
                "is_error": bool(getattr(message, "is_error", False)), "text": getattr(message, "result", None),
                "usage": getattr(message, "usage", None), "model_usage": getattr(message, "model_usage", None),
                "cost_usd": getattr(message, "total_cost_usd", None), "num_turns": getattr(message, "num_turns", None),
                "duration_ms": getattr(message, "duration_ms", None)}, message)
        return RuntimeEvent("other", {}, message)

    def stream(self, prompt: str, options, query: Optional[Callable] = None):
        import claude_agent_sdk as sdk

        return (query or sdk.query)(prompt=prompt, options=options)


ADAPTER = ClaudeAgentRuntime()


# -- Cost ------------------------------------------------------------------------------
def rate_card_estimate(model_usage: Optional[dict], usage: Optional[dict], configured_model: str) -> Optional[float]:
    """USD estimate from reported token counts and Jade's versioned rate card."""
    rows = []
    if model_usage:
        for model, u in model_usage.items():
            rows.append((model, (u or {}).get("inputTokens", 0) + (u or {}).get("cacheReadInputTokens", 0)
                         + (u or {}).get("cacheCreationInputTokens", 0), (u or {}).get("outputTokens", 0)))
    elif usage:
        rows.append((configured_model, usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                     + usage.get("cache_creation_input_tokens", 0), usage.get("output_tokens", 0)))
    if not rows:
        return None
    total = 0.0
    for model, tin, tout in rows:
        price = ai_connection.MODELS.get(model) or next(
            (v for k, v in ai_connection.MODELS.items() if model.startswith(k)), None)
        if price is None:
            return None
        total += tin / 1e6 * price["input"] + tout / 1e6 * price["output"]
    return round(total, 6)


# -- A run -----------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentRun:
    def __init__(self, *, run_id: str, company_id: str, driver: str, story_id: Optional[str],
                 conn: ResolvedConnection, snapshots: dict[str, ai_packs.PackSnapshot], config_dir: str,
                 adapter: ClaudeAgentRuntime, activities: Optional[dict[str, str]] = None,
                 context: Optional[dict] = None) -> None:
        self.run_id, self.company_id, self.driver, self.story_id = run_id, company_id, driver, story_id
        # The model of each role is fixed here, at run start, from the configuration revision resolved once.
        self.models = {role: conn.model_for(role, (activities or {}).get(role)) for role in snapshots}
        self.main_model = self.models[next(iter(snapshots))]
        self.context = context  # the versioned context package this run was given (or None: no story)
        self.runtime_version: Optional[str] = None
        self.connection, self.packs, self.config_dir, self.adapter = conn, snapshots, config_dir, adapter
        self.documents_allowed = conn.document_policy == "permitted_content"
        self.knowledge_log: list[dict] = []  # provenance: what the knowledge tools listed/returned
        self.reported_model: Optional[str] = None
        self.credential_source: Optional[str] = None
        self.result: Optional[dict] = None
        self.finished = False
        self._knowledge = None

    # Pack-derived pieces -------------------------------------------------------------
    def agent_version(self, role: str) -> str:
        p = self.packs[role]
        return f"{p.pack_id}@r{p.revision}#{p.sha256[:12]}"

    def pack_prompt(self, role: str) -> str:
        return self.packs[role].prompt()

    def context_prompt(self) -> str:
        """The run's context package as a prompt block ('' when the run has no story)."""
        from . import context as ai_context

        return ai_context.prompt_block(self.context) if self.context else ""

    def _knowledge_server(self):
        if self._knowledge is None:
            from ..knowledge.tools import KnowledgeTools

            refs = sorted({r for p in self.packs.values() for r in p.content["knowledge"]})
            self._knowledge = KnowledgeTools(company_id=self.company_id, story_id=self.story_id, refs=refs,
                                             documents_allowed=self.documents_allowed, log=self.knowledge_log)
        return self._knowledge.sdk_server()

    def options(self, *, cwd: str, permission_mode: str, allowed_tools: list[str], max_turns: int,
                disallowed_tools: Optional[list[str]] = None, tool_servers: Optional[dict] = None,
                subagents: Optional[list[str]] = None, top_level: Optional[str] = None,
                top_level_in_system_prompt: bool = True):
        """subagents: roles reached via Task (each defined from its pack);
        top_level: a role whose pack instructions become the main agent's
        system prompt. A tool is available only if the driver allows it AND
        a pack of this run requests it within its role's ceiling."""
        roles = list(subagents or []) + ([top_level] if top_level else [])
        requested = {t for r in roles for t in self.packs[r].effective_tools(documents_allowed=self.documents_allowed)}
        driver_allowed = set(allowed_tools) | set(ai_packs.KNOWLEDGE_TOOLS)
        allowed = [t for t in dict.fromkeys([*allowed_tools, *ai_packs.KNOWLEDGE_TOOLS])
                   if (t == "Task" and subagents) or t in requested]
        # Hide (not merely deny) every project-server and knowledge tool this
        # run does not allow, so the model never sees them.
        from ..services.architecture_driver import PROJECT_SERVER_TOOLS

        hidden = [t for t in (*[f"mcp__jde-change-factory__{n}" for n in PROJECT_SERVER_TOOLS],
                              *ai_packs.KNOWLEDGE_TOOLS) if t not in allowed]
        servers = dict(tool_servers or {})
        if any(t in requested for t in ai_packs.KNOWLEDGE_TOOLS):
            servers["jade-knowledge"] = self._knowledge_server()
        agents = [AgentSpec(name=r, description=self.packs[r].content["description"], prompt=self.packs[r].prompt(),
                            tools=[t for t in self.packs[r].effective_tools(documents_allowed=self.documents_allowed)
                                   if t in driver_allowed],
                            max_turns=self.packs[r].content.get("limits", {}).get("max_turns"),
                            model=self.models[r])
                  for r in (subagents or [])]
        limits = self.connection.limits
        pack_turns = self.packs[top_level].content.get("limits", {}).get("max_turns") if top_level else None
        spec = RunSpec(
            model=self.main_model, api_key=self.connection.api_key, base_url=ai_connection.endpoint()[0],
            config_dir=self.config_dir, cwd=cwd, permission_mode=permission_mode, allowed_tools=allowed,
            disallowed_tools=list(dict.fromkeys([*(disallowed_tools or []), *hidden])),
            max_turns=min(x for x in (max_turns, int(limits.get("max_turns") or max_turns), pack_turns) if x),
            max_budget_usd=float(limits.get("max_usd_per_run") or ai_connection.DEFAULT_LIMITS["max_usd_per_run"]),
            agents=agents, tool_servers=servers,
            system_prompt_append=self.packs[top_level].prompt() if top_level and top_level_in_system_prompt else None)
        return self.adapter.build_options(spec)

    # Streaming -------------------------------------------------------------------------
    async def stream(self, prompt: str, options, query: Optional[Callable] = None) -> AsyncIterator[RuntimeEvent]:
        async for message in self.adapter.stream(prompt, options, query):
            event = self.adapter.translate(message)
            if event.kind == "init":
                self.reported_model = event.data.get("model")
                self.runtime_version = event.data.get("runtime_version")
                self.credential_source = event.data.get("credential_source")
                if self.credential_source != EXPECTED_KEY_SOURCE:
                    raise RuntimeMismatch(f"the agent runtime reported credential source {self.credential_source!r}, "
                                          "not this customer's API key; the run was stopped before any model request")
                if not (self.reported_model or "").startswith(self.main_model):
                    raise RuntimeMismatch(f"the agent runtime selected model {self.reported_model!r}, not the "
                                          f"configured {self.main_model}; the run was stopped")
            elif event.kind == "result":
                self.result = event.data
            yield event

    # Record ------------------------------------------------------------------------------
    def _insert(self, initiated_by: Optional[str]) -> None:
        with db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO ai_runs (run_id, company_id, driver, story_id, roles, status, provider, configured_model, "
                "connection_revision, credential_revision, packs, knowledge, initiated_by, started_at, runtime, models, "
                "context, notes) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)",
                (self.run_id, self.company_id, self.driver, self.story_id, json.dumps(sorted(self.packs)),
                 self.connection.provider, self.main_model, self.connection.revision,
                 self.connection.credential_revision,
                 json.dumps([{**p.record(), "model": self.models[r]} for r, p in self.packs.items()]),
                 initiated_by, _now(), self.adapter.name, json.dumps(self.models),
                 json.dumps([{k: self.context[k] for k in ("package_id", "version", "sha256")}] if self.context else []),
                 json.dumps(self._continuity_notes())))

    def _continuity_notes(self) -> list[str]:
        """A change of model or configuration between runs of the same story is
        stated on the run record, never silent."""
        if not self.story_id:
            return []
        with db() as conn:
            prev = conn.execute("SELECT run_id, models, connection_revision FROM ai_runs WHERE company_id = ? AND "
                                "story_id = ? AND status IN ('completed', 'failed') AND models != '{}' "
                                "ORDER BY started_at DESC LIMIT 1", (self.company_id, self.story_id)).fetchone()
        if prev is None:
            return []
        notes = []
        before = json.loads(prev["models"] or "{}")
        for role, model in self.models.items():
            if role in before and before[role] != model:
                notes.append(f"{role}: model changed since run {prev['run_id']} ({before[role]} -> {model})")
        if prev["connection_revision"] not in (None, self.connection.revision):
            notes.append(f"AI configuration revision changed since run {prev['run_id']} "
                         f"(r{prev['connection_revision']} -> r{self.connection.revision})")
        return notes

    def finish(self, status: str, error: Optional[str] = None) -> None:
        if self.finished:
            return
        self.finished = True
        r = self.result or {}
        reported_cost = r.get("cost_usd")
        estimate = rate_card_estimate(r.get("model_usage"), r.get("usage"), self.main_model)
        if reported_cost is not None:
            cost, basis = float(reported_cost), "estimate: cost reported by the agent runtime from token usage at list prices"
        elif estimate is not None:
            cost, basis = estimate, f"estimate: Jade rate card {ai_connection.RATE_CARD_VERSION} x reported tokens"
        else:
            cost, basis = None, "unknown: the runtime ended before reporting usage"
        models = sorted((r.get("model_usage") or {}).keys())
        usage = {"tokens": r.get("usage"), "modelUsage": r.get("model_usage"), "numTurns": r.get("num_turns"),
                 "durationMs": r.get("duration_ms"), "tokensBasis": "reported by the provider via the agent runtime",
                 "rateCardEstimateUsd": estimate, "rateCardVersion": ai_connection.RATE_CARD_VERSION}
        reported_model = self.reported_model
        if models:
            reported_model = ", ".join(models) if reported_model in models or not reported_model else \
                f"{reported_model} (usage: {', '.join(models)})"
        with db(immediate=True) as conn:
            conn.execute(
                "UPDATE ai_runs SET status = ?, reported_model = ?, credential_source = ?, knowledge = ?, usage = ?, "
                "cost_usd = ?, cost_basis = ?, error = ?, finished_at = ?, runtime = ? WHERE run_id = ?",
                (status, reported_model, self.credential_source or "not reported by the runtime",
                 json.dumps(self.knowledge_log[:200]), json.dumps(usage), cost, basis,
                 (error or "")[:1000] or None, _now(), self._runtime_label(), self.run_id))

    def _runtime_label(self) -> str:
        try:
            from claude_agent_sdk import __version__ as sdk_version
        except ImportError:  # pragma: no cover
            sdk_version = "?"
        return (f"{self.adapter.name} {sdk_version}"
                + (f" / Claude Code {self.runtime_version}" if self.runtime_version else ""))


def prepare(company_id: Optional[str], roles: list[str]) -> tuple[ResolvedConnection, dict[str, ai_packs.PackSnapshot]]:
    """Resolve everything a run needs, or raise AiNotConfigured."""
    conn = ai_connection.resolve_for_run(company_id)
    return conn, {role: ai_packs.snapshot(conn.company_id, role) for role in roles}


@asynccontextmanager
async def agent_run(*, company_id: Optional[str], driver: str, roles: list[str], story_id: Optional[str] = None,
                    initiated_by: Optional[str] = None, adapter: ClaudeAgentRuntime = ADAPTER,
                    activities: Optional[dict[str, str]] = None):
    """roles: the first is the main agent (its activity's model is the run's
    main model); the others are subagents with their own configured model.
    activities: override the activity a role runs as (e.g. the Technical
    Agent's verification runs)."""
    try:
        conn, snapshots = prepare(company_id, roles)
    except AiNotConfigured as exc:
        mark_blocked(company_id=company_id, driver=driver, roles=roles, story_id=story_id, reason=str(exc),
                     initiated_by=initiated_by)
        raise
    from . import context as ai_context

    pkg = ai_context.snapshot(conn.company_id, story_id) if story_id else None
    config_dir = tempfile.mkdtemp(prefix="jade-agent-")
    run = AgentRun(run_id=f"ai-{uuid.uuid4().hex[:12]}", company_id=conn.company_id, driver=driver, story_id=story_id,
                   conn=conn, snapshots=snapshots, config_dir=config_dir, adapter=adapter, activities=activities,
                   context=pkg)
    run._insert(initiated_by)
    try:
        yield run
    except BaseException as exc:
        run.finish("failed", f"{type(exc).__name__}: {exc}")
        raise
    else:
        run.finish("failed" if (run.result or {}).get("is_error") or run.result is None else "completed",
                   None if run.result and not run.result.get("is_error") else "the runtime produced no successful result")
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def mark_blocked(*, company_id: Optional[str], driver: str, roles: list[str], story_id: Optional[str],
                 reason: str, initiated_by: Optional[str] = None) -> None:
    """Record a run that was refused before it started (configuration missing)."""
    if not company_id:
        return
    with db(immediate=True) as conn:
        conn.execute("INSERT INTO ai_runs (run_id, company_id, driver, story_id, roles, status, packs, knowledge, error, "
                     "initiated_by, started_at, finished_at) VALUES (?, ?, ?, ?, ?, 'blocked', '[]', '[]', ?, ?, ?, ?)",
                     (f"ai-{uuid.uuid4().hex[:12]}", company_id, driver, story_id, json.dumps(sorted(roles)),
                      reason[:1000], initiated_by, _now(), _now()))


# -- Reading ------------------------------------------------------------------------------
def list_runs(company_id: str, limit: int = 50) -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM ai_runs WHERE company_id = ? ORDER BY started_at DESC LIMIT ?",
                            (company_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("roles", "packs", "knowledge", "usage", "models", "context", "notes"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        out.append(d)
    return out


def health(company_id: str) -> list[dict]:
    """Per role: configured, connection tested, last successful REAL run
    (the runtime itself reported this customer's key), disabled, failed."""
    from ..services import agent_settings

    view = ai_connection.view(company_id)
    assigned = ai_packs.assignments(company_id)
    disabled_roles = agent_settings.disabled_agents(company_id)
    runs = list_runs(company_id, limit=500)
    out = []
    for role, info in ai_packs.ROLES.items():
        mine = [r for r in runs if role in (r["roles"] or [])]
        real_ok = next((r for r in mine if r["status"] == "completed" and r["credential_source"] == EXPECTED_KEY_SOURCE
                        and r["provider"] == ai_connection.PROVIDER), None)
        last = mine[0] if mine else None
        pack_problem = None
        try:
            snap = ai_packs.snapshot(company_id, role)
        except AiNotConfigured as exc:
            snap, pack_problem = None, str(exc)
        conn_problem = None
        try:
            ai_connection.resolve_for_run(company_id)
        except AiNotConfigured as exc:
            conn_problem = str(exc)
        if role in disabled_roles:
            state = "disabled"
        elif conn_problem or pack_problem:
            state = "not configured"
        elif last and last["status"] in ("failed", "blocked"):
            state = "failed"
        elif real_ok:
            state = "working"
        elif view.get("tested"):
            state = "connection tested"
        else:
            state = "configured"
        out.append({
            "role": role, "label": info["label"], "state": state, "disabled": role in disabled_roles,
            "blockedReason": conn_problem or pack_problem,
            "configured": not conn_problem and not pack_problem, "connectionTested": bool(view.get("tested")),
            "connectionRevision": view.get("revision"),
            "activity": ai_connection.ROLE_ACTIVITY.get(role),
            "configuredModel": (view.get("activityModels") or {}).get(ai_connection.ROLE_ACTIVITY.get(role, ""),
                                                                      view.get("model")),
            "pack": snap.record() if snap else None, "assignment": assigned.get(role),
            "lastRun": last, "lastSuccessfulRealRun": real_ok,
        })
    return out


def require_ready(company_id: Optional[str], roles: list[str], *, driver: str, story_id: Optional[str] = None,
                  initiated_by: Optional[str] = None) -> None:
    """Checked by the API before accepting work, so a missing configuration
    is refused at once with its reason (and shows in Agent Health)."""
    try:
        prepare(company_id, roles)
    except AiNotConfigured as exc:
        mark_blocked(company_id=company_id, driver=driver, roles=roles, story_id=story_id, reason=str(exc),
                     initiated_by=initiated_by)
        raise


def reconcile_interrupted() -> int:
    """Runs still 'running' when the server starts were cut off by the restart."""
    with db(immediate=True) as conn:
        cur = conn.execute("UPDATE ai_runs SET status = 'failed', error = 'interrupted by a server restart', "
                           "finished_at = ? WHERE status = 'running'", (_now(),))
        return cur.rowcount
