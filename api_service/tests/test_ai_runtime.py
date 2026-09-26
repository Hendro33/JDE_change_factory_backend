"""
Customer AI connection, Start-up Packs and the agent runtime -- all with a
MOCKED runtime/provider (nothing here contacts Anthropic). The real CLI is
exercised against a loopback fake provider by scripts/prove_runtime_isolation.py.
"""

from __future__ import annotations

import asyncio
import json
import os

import claude_agent_sdk as sdk
import pytest

from .conftest import headers

KEY_A = "sk-ant-api03-SYNTHETIC-A-aaaaaaaaaaaaaaaaaaaa"
KEY_B = "sk-ant-api03-SYNTHETIC-B-bbbbbbbbbbbbbbbbbbbb"


def _result(text="done", is_error=False):
    return sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=1,
                             session_id="s", total_cost_usd=0.0123, result=text,
                             usage={"input_tokens": 1000, "output_tokens": 100})


def _init(model, source="ANTHROPIC_API_KEY"):
    return sdk.SystemMessage(subtype="init", data={"model": model, "apiKeySource": source})


def _configure_via_api(client, customer="vdb", key=KEY_A, model="claude-sonnet-5", policy="metadata_only"):
    current = client.get("/admin/ai/connection", headers=headers(customer)).json()
    r = client.put("/admin/ai/connection", headers=headers(customer),
                   json={"model": model, "enabled": True, "documentPolicy": policy, "limits": {},
                         "expectedRevision": current.get("revision")})
    assert r.status_code == 200, r.text
    r = client.put("/admin/ai/connection/credential", headers=headers(customer), json={"apiKey": key})
    assert r.status_code == 200, r.text
    return r.json()


# -- Connection: storage, write-only key, no provider contact on save ------------------
def test_saving_never_contacts_the_provider_and_the_key_is_write_only(client, monkeypatch):
    from jde_api_service.ai import connection

    def boom(*a, **k):
        raise AssertionError("the provider was contacted")

    monkeypatch.setattr(connection, "_send_test_message", boom)
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", boom)
    view = _configure_via_api(client)
    assert view["credentialState"] == "stored (encrypted)" and view["credentialHint"] == "…" + KEY_A[-4:]
    for path in ("/admin/ai/connection", "/admin/ai/health", "/admin/ai/runs", "/admin/ai/packs"):
        body = client.get(path, headers=headers("vdb")).text
        assert KEY_A not in body and KEY_A[10:30] not in body
    from jde_api_service.persistence.db import connection as db

    with db() as conn:
        stored = conn.execute("SELECT credential_secret FROM ai_connections WHERE company_id = 'vdb'").fetchone()[0]
        audit = " ".join(r[0] for r in conn.execute("SELECT detail FROM ai_connection_audit"))
    assert KEY_A not in stored and KEY_A not in audit


def test_the_connection_test_must_be_confirmed_as_billable_and_is_recorded(client, monkeypatch):
    from jde_api_service.ai import connection

    calls = []
    monkeypatch.setattr(connection, "_send_test_message",
                        lambda key, model: calls.append((key, model)) or f"answered by {model}")
    _configure_via_api(client)
    r = client.post("/admin/ai/connection/test", headers=headers("vdb"), json={})
    assert r.status_code == 428 and "billed" in r.json()["detail"] and calls == []
    r = client.post("/admin/ai/connection/test", headers=headers("vdb"), json={"confirmBillable": True})
    assert r.status_code == 200 and r.json()["outcome"] == "ok" and r.json()["billable"] is True
    assert calls == [(KEY_A, "claude-sonnet-5")] and r.json()["connection"]["tested"] is True
    assert r.json()["connection"]["serverKeyConfigured"] is True
    # A new key invalidates the earlier test.
    _configure_via_api(client, key=KEY_B)
    assert client.get("/admin/ai/connection", headers=headers("vdb")).json()["tested"] is False


def test_only_admins_manage_the_connection_and_each_customer_sees_only_its_own(client, ellen_client):
    _configure_via_api(client, customer="vdb")
    # Ellen is a member of vdb only: another customer's configuration is refused outright.
    assert ellen_client.get("/admin/ai/connection", headers=headers("bwm")).status_code == 403
    assert client.get("/admin/ai/connection", headers=headers("bwm")).json()["configured"] is False


@pytest.mark.no_auto_ai
def test_no_fallback_to_the_machine_key_or_login_when_the_customer_has_no_connection(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-HOST-MACHINE-KEY-xxxxxxxxxx")
    started = []

    async def fake_query(*, prompt, options):
        started.append(1)
        yield _result()

    monkeypatch.setattr(sdk, "query", fake_query)
    r = client.post("/change-requests", headers=headers("vdb"),
                    json={"title": "t", "businessSource": "Business", "rawContent": "Synthetic request"})
    cid = r.json()["id"]
    r = client.post(f"/changes/{cid}/enhance", headers=headers("vdb"))
    assert r.status_code == 409 and "no AI connection" in r.json()["detail"]
    assert started == []
    health = {h["role"]: h for h in client.get("/admin/ai/health", headers=headers("vdb")).json()["roles"]}
    assert health["receive-agent"]["state"] == "not configured"
    runs = client.get("/admin/ai/runs", headers=headers("vdb")).json()
    assert runs and runs[0]["status"] == "blocked"


@pytest.mark.no_auto_ai
def test_a_revoked_key_blocks_the_next_run_safely(client, monkeypatch):
    from jde_api_service.ai import runtime

    _configure_via_api(client)
    from . import _ai

    _ai.configure("vdb")
    runtime.prepare("vdb", ["improve-agent"])  # works
    assert client.delete("/admin/ai/connection/credential", headers=headers("vdb")).status_code == 200
    with pytest.raises(runtime.AiNotConfigured, match="revoked"):
        runtime.prepare("vdb", ["improve-agent"])


# -- Runtime isolation --------------------------------------------------------------------
def test_each_run_gets_its_own_key_model_and_config_dir_without_touching_os_environ(isolated_dirs, monkeypatch):
    """Spawn level: the real SDK builds the real subprocess environment;
    only the process start itself is intercepted."""
    from claude_agent_sdk._internal.transport import subprocess_cli

    from jde_api_service.ai import runtime

    from . import _ai

    _ai.configure("cust-a", key=KEY_A, model="claude-sonnet-5")
    _ai.configure("cust-b", key=KEY_B, model="claude-haiku-4-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-HOST-MACHINE-KEY-xxxxxxxxxx")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "host-oauth")
    monkeypatch.setenv("SOME_HOST_SESSION_TOKEN_FILE", "/secret/path")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    monkeypatch.setattr(subprocess_cli.SubprocessCLITransport, "_find_cli", lambda self: "/bin/true")
    spawned = []

    class _Stop(Exception):
        pass

    async def fake_open_process(cmd, **kw):
        spawned.append({"cmd": cmd, "env": kw["env"]})
        await asyncio.sleep(0.05)  # let the other run spawn in between
        raise _Stop()

    monkeypatch.setattr(subprocess_cli.anyio, "open_process", fake_open_process)
    before = dict(os.environ)

    async def one(company):
        try:
            async with runtime.agent_run(company_id=company, driver="t", roles=["improve-agent"]) as run:
                opts = run.options(cwd=".", permission_mode="dontAsk", allowed_tools=["Task"], max_turns=5,
                                   subagents=["improve-agent"])
                async for _ in run.stream("x", opts):
                    pass
        except Exception:  # noqa: BLE001 -- the intercepted spawn
            pass

    async def both():
        await asyncio.gather(one("cust-a"), one("cust-b"))

    asyncio.run(both())
    assert dict(os.environ) == before
    envs = {s["env"]["ANTHROPIC_API_KEY"]: s for s in spawned}
    assert set(envs) == {KEY_A, KEY_B}
    a, b = envs[KEY_A], envs[KEY_B]
    assert a["env"]["ANTHROPIC_MODEL"] == "claude-sonnet-5" and b["env"]["ANTHROPIC_MODEL"] == "claude-haiku-4-5"
    assert "--model" in a["cmd"] and a["cmd"][a["cmd"].index("--model") + 1] == "claude-sonnet-5"
    assert b["cmd"][b["cmd"].index("--model") + 1] == "claude-haiku-4-5"
    assert a["env"]["CLAUDE_CONFIG_DIR"] != b["env"]["CLAUDE_CONFIG_DIR"]
    for s in (a, b):
        assert s["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "" and s["env"]["SOME_HOST_SESSION_TOKEN_FILE"] == ""
        assert s["env"]["ANTHROPIC_AUTH_TOKEN"] == "" and s["env"]["JDE_CREDENTIAL_KEY"] == ""
        assert s["env"]["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
        assert "--setting-sources=project" in s["cmd"]
        # Subagents run inside the run (the CLI defaults to background agents otherwise).
        assert s["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
        assert not os.path.exists(s["env"]["CLAUDE_CONFIG_DIR"])  # removed after the run


def test_the_runtime_is_stopped_if_it_reports_another_credential_or_model(isolated_dirs, monkeypatch):
    from jde_api_service.ai import runtime

    from . import _ai

    _ai.configure("vdb")

    async def run_with(first):
        async def q(*, prompt, options):
            yield first
            yield _result()

        async with runtime.agent_run(company_id="vdb", driver="t", roles=["improve-agent"]) as run:
            opts = run.options(cwd=".", permission_mode="dontAsk", allowed_tools=["Task"], max_turns=5,
                               subagents=["improve-agent"])
            async for _ in run.stream("x", opts, query=q):
                pass

    with pytest.raises(runtime.RuntimeMismatch, match="credential source"):
        asyncio.run(run_with(_init("claude-sonnet-5", source="/login managed key")))
    with pytest.raises(runtime.RuntimeMismatch, match="model"):
        asyncio.run(run_with(_init("claude-fable-5-1")))
    runs = runtime.list_runs("vdb")
    assert [r["status"] for r in runs[:2]] == ["failed", "failed"]


def test_a_run_record_shows_configuration_pack_revision_usage_and_cost_basis(isolated_dirs):
    from jde_api_service.ai import runtime

    from . import _ai

    _ai.configure("vdb", model="claude-sonnet-5")

    async def q(*, prompt, options):
        yield _init("claude-sonnet-5")
        yield _result()

    async def go():
        async with runtime.agent_run(company_id="vdb", driver="t", roles=["improve-agent"], story_id="S1") as run:
            opts = run.options(cwd=".", permission_mode="dontAsk", allowed_tools=["Task"], max_turns=5,
                               subagents=["improve-agent"])
            async for _ in run.stream("x", opts, query=q):
                pass

    asyncio.run(go())
    rec = runtime.list_runs("vdb")[0]
    assert rec["status"] == "completed" and rec["configured_model"] == "claude-sonnet-5"
    assert rec["reported_model"] == "claude-sonnet-5" and rec["credential_source"] == "ANTHROPIC_API_KEY"
    assert rec["packs"][0]["packId"] == "tpl-improve-agent" and len(rec["packs"][0]["sha256"]) == 64
    assert rec["cost_usd"] == pytest.approx(0.0123) and rec["cost_basis"].startswith("estimate")
    assert rec["usage"]["tokens"] == {"input_tokens": 1000, "output_tokens": 100}
    assert rec["usage"]["rateCardEstimateUsd"] == pytest.approx(1000 / 1e6 * 2 + 100 / 1e6 * 10)
    health = {h["role"]: h for h in runtime.health("vdb")}
    assert health["improve-agent"]["lastSuccessfulRealRun"]["run_id"] == rec["run_id"]


def test_the_monthly_budget_blocks_further_runs(isolated_dirs):
    from jde_api_service.ai import connection, runtime
    from jde_api_service.persistence.db import connection as db

    from . import _ai

    _ai.configure("vdb")
    connection.save("vdb", model="claude-sonnet-5", enabled=True, document_policy="metadata_only",
                    limits={"monthly_usd": 1.0}, expected_revision=1, actor="t")
    with db(immediate=True) as conn:
        conn.execute("INSERT INTO ai_runs (run_id, company_id, driver, roles, status, cost_usd, started_at) "
                     "VALUES ('x', 'vdb', 't', '[]', 'completed', 1.5, ?)", (runtime._now(),))
    with pytest.raises(runtime.AiNotConfigured, match="monthly AI budget"):
        runtime.prepare("vdb", ["improve-agent"])


# -- Start-up Packs ---------------------------------------------------------------------------
def _published(client, pack_id, customer="vdb"):
    p = next(p for p in client.get("/admin/ai/packs", headers=headers(customer)).json()["packs"] if p["packId"] == pack_id)
    return [r["revision"] for r in p["revisions"] if r["status"] == "published"]


def test_pack_lifecycle_draft_publish_assign_rollback_and_audit(client):
    r = client.post("/admin/ai/packs", headers=headers("vdb"), json={
        "role": "improve-agent", "name": "VdB improve", "fromPackId": "tpl-improve-agent",
        "fromRevision": _published(client, "tpl-improve-agent")[0]})
    assert r.status_code == 201, r.text
    pack = r.json()
    content = {**pack["content"], "skills": [{"name": "Logistics terms", "body": "Use the customer's terms."}]}
    assert client.put(f"/admin/ai/packs/{pack['packId']}/draft", headers=headers("vdb"),
                      json={"content": content}).status_code == 200
    assert client.post(f"/admin/ai/packs/{pack['packId']}/revisions/1/publish", headers=headers("vdb")).status_code == 200
    # Published revisions are immutable: the next edit opens revision 2.
    content2 = {**content, "instructions": content["instructions"] + "\nAlways ask about warehouses."}
    r = client.put(f"/admin/ai/packs/{pack['packId']}/draft", headers=headers("vdb"), json={"content": content2})
    assert r.json()["revision"] == 2 and r.json()["status"] == "draft"
    assert client.put("/admin/ai/assignments/improve-agent", headers=headers("vdb"),
                      json={"packId": pack["packId"], "revision": 2}).status_code == 422  # drafts cannot be assigned
    client.post(f"/admin/ai/packs/{pack['packId']}/revisions/2/publish", headers=headers("vdb"))
    for rev in (2, 1):  # assign, then roll back
        r = client.put("/admin/ai/assignments/improve-agent", headers=headers("vdb"),
                       json={"packId": pack["packId"], "revision": rev})
        assert r.status_code == 200 and r.json()["improve-agent"]["revision"] == rev
    from jde_api_service.ai import packs

    assert packs.snapshot("vdb", "improve-agent").revision == 1
    actions = [a["action"] for a in client.get("/admin/ai/audit", headers=headers("vdb")).json()["packs"]]
    assert {"pack_created", "draft_saved", "published", "assigned"} <= set(actions)
    # Templates change only through code review; other customers do not see this pack.
    assert client.put("/admin/ai/packs/tpl-improve-agent/draft", headers=headers("vdb"),
                      json={"content": content}).status_code == 422
    assert pack["packId"] not in [p["packId"] for p in client.get("/admin/ai/packs", headers=headers("bwm")).json()["packs"]]
    assert client.put("/admin/ai/assignments/improve-agent", headers=headers("bwm"),
                      json={"packId": pack["packId"], "revision": 1}).status_code == 422


def test_a_pack_cannot_grant_a_tool_outside_its_roles_reviewed_ceiling(client):
    r = client.post("/admin/ai/packs", headers=headers("vdb"), json={
        "role": "receive-agent", "fromPackId": "tpl-receive-agent",
        "fromRevision": _published(client, "tpl-receive-agent")[0]})
    pack = r.json()
    for tool in ("Bash", "mcp__jde-change-factory__set_processing_option", "mcp__jde-change-factory__propose_to_backlog"):
        r = client.put(f"/admin/ai/packs/{pack['packId']}/draft", headers=headers("vdb"),
                       json={"content": {**pack["content"], "capabilities": [tool]}})
        assert r.status_code == 422 and "not allowed" in r.json()["detail"]


def test_tampered_or_disabled_packs_block_and_ceilings_hold_even_if_the_database_is_edited(client):
    from jde_api_service.ai import packs
    from jde_api_service.persistence.db import connection as db

    r = client.post("/admin/ai/packs", headers=headers("vdb"), json={
        "role": "improve-agent", "fromPackId": "tpl-improve-agent",
        "fromRevision": _published(client, "tpl-improve-agent")[0]})
    pid = r.json()["packId"]
    client.post(f"/admin/ai/packs/{pid}/revisions/1/publish", headers=headers("vdb"))
    client.put("/admin/ai/assignments/improve-agent", headers=headers("vdb"), json={"packId": pid, "revision": 1})
    with db(immediate=True) as conn:
        row = conn.execute("SELECT content FROM agent_pack_revisions WHERE pack_id = ?", (pid,)).fetchone()
        content = json.loads(row[0])
        content["capabilities"] = content["capabilities"] + ["Bash"]
        conn.execute("UPDATE agent_pack_revisions SET content = ? WHERE pack_id = ?", (json.dumps(content), pid))
    with pytest.raises(packs.AiNotConfigured, match="integrity"):
        packs.snapshot("vdb", "improve-agent")
    with db(immediate=True) as conn:  # even with a recomputed hash, the ceiling filters it out
        conn.execute("UPDATE agent_pack_revisions SET sha256 = ? WHERE pack_id = ?", (packs.content_sha(content), pid))
    assert "Bash" not in packs.snapshot("vdb", "improve-agent").effective_tools(documents_allowed=True)
    assert client.put(f"/admin/ai/packs/{pid}/disabled", headers=headers("vdb"), json={"disabled": True}).status_code == 200
    with pytest.raises(packs.AiNotConfigured, match="disabled"):
        packs.snapshot("vdb", "improve-agent")
    client.delete("/admin/ai/assignments/improve-agent", headers=headers("vdb"))
    with pytest.raises(packs.AiNotConfigured, match="no Start-up Pack"):
        packs.snapshot("vdb", "improve-agent")


def test_an_in_progress_run_keeps_its_pack_revision_when_the_assignment_changes(client, monkeypatch):
    from jde_api_service.ai import packs, runtime

    from . import _ai

    _ai.configure("vdb")
    r = client.post("/admin/ai/packs", headers=headers("vdb"), json={
        "role": "improve-agent", "fromPackId": "tpl-improve-agent",
        "fromRevision": _published(client, "tpl-improve-agent")[0]})
    pid = r.json()["packId"]
    base = r.json()["content"]
    client.put(f"/admin/ai/packs/{pid}/draft", headers=headers("vdb"),
               json={"content": {**base, "instructions": "REVISION ONE instructions"}})
    client.post(f"/admin/ai/packs/{pid}/revisions/1/publish", headers=headers("vdb"))
    client.put("/admin/ai/assignments/improve-agent", headers=headers("vdb"), json={"packId": pid, "revision": 1})
    seen = {}

    async def q(*, prompt, options):
        seen["prompt_at_start"] = options.agents["improve-agent"].prompt
        # An Admin publishes and assigns revision 2 while this run is going.
        client.put(f"/admin/ai/packs/{pid}/draft", headers=headers("vdb"),
                   json={"content": {**base, "instructions": "REVISION TWO instructions"}})
        client.post(f"/admin/ai/packs/{pid}/revisions/2/publish", headers=headers("vdb"))
        client.put("/admin/ai/assignments/improve-agent", headers=headers("vdb"), json={"packId": pid, "revision": 2})
        yield _init("claude-sonnet-5")
        yield _result()

    async def go():
        async with runtime.agent_run(company_id="vdb", driver="t", roles=["improve-agent"]) as run:
            opts = run.options(cwd=".", permission_mode="dontAsk", allowed_tools=["Task"], max_turns=5,
                               subagents=["improve-agent"])
            async for _ in run.stream("x", opts, query=q):
                pass
            seen["prompt_at_end"] = run.pack_prompt("improve-agent")
            seen["version"] = run.agent_version("improve-agent")

    asyncio.run(go())
    assert "REVISION ONE" in seen["prompt_at_start"] and "REVISION ONE" in seen["prompt_at_end"]
    assert seen["version"].startswith(f"{pid}@r1#")
    assert runtime.list_runs("vdb")[0]["packs"][0]["revision"] == 1
    assert packs.snapshot("vdb", "improve-agent").revision == 2  # the NEXT run uses revision 2


def test_an_agent_disabled_by_the_admin_never_starts(client, monkeypatch):
    started = []

    async def q(*, prompt, options):
        started.append(1)
        yield _result()

    monkeypatch.setattr(sdk, "query", q)
    s = client.get("/admin/agent-settings", headers=headers("vdb")).json()
    r = client.put("/admin/agent-settings", headers=headers("vdb"),
                   json={"disabled": ["improve-agent"], "expectedRevision": s.get("revision")})
    assert r.status_code == 200, r.text
    cid = client.post("/change-requests", headers=headers("vdb"),
                      json={"title": "t", "businessSource": "Business", "rawContent": "Synthetic"}).json()["id"]
    assert client.post(f"/changes/{cid}/enhance", headers=headers("vdb")).status_code == 409
    assert started == []


# -- Per-activity models, context packages, continuity -------------------------------------
def test_activity_overrides_are_validated_and_only_supported_models_are_offered(client):
    _configure_via_api(client)
    view = client.get("/admin/ai/connection", headers=headers("vdb")).json()
    assert view["runtime"] == "claude-agent-sdk" and {a["id"] for a in view["activities"]} == {
        "functional_analysis", "verification", "architecture", "technical_build"}
    body = {"model": "claude-sonnet-5", "enabled": True, "documentPolicy": "metadata_only", "limits": {},
            "expectedRevision": view["revision"]}
    for bad in ({"architecture": "gpt-5"}, {"planning": "claude-opus-5"}):
        r = client.put("/admin/ai/connection", headers=headers("vdb"), json={**body, "activityModels": bad})
        assert r.status_code == 422
    r = client.put("/admin/ai/connection", headers=headers("vdb"),
                   json={**body, "activityModels": {"architecture": "claude-opus-5", "verification": "claude-haiku-4-5"}})
    assert r.status_code == 200 and r.json()["activityModels"] == {"architecture": "claude-opus-5",
                                                                     "verification": "claude-haiku-4-5"}
    health = {h["role"]: h["configuredModel"] for h in client.get("/admin/ai/health", headers=headers("vdb")).json()["roles"]}
    assert health["architect"] == "claude-opus-5" and health["check-agent"] == "claude-haiku-4-5"
    assert health["improve-agent"] == "claude-sonnet-5"


def test_each_agent_runs_on_its_activitys_model_and_the_run_records_it(isolated_dirs):
    from jde_api_service.ai import connection, runtime

    from . import _ai

    _ai.configure("vdb", model="claude-sonnet-5")
    connection.save("vdb", model="claude-sonnet-5", enabled=True, document_policy="metadata_only", limits=None,
                    expected_revision=1, actor="t", activity_models={"verification": "claude-haiku-4-5"})
    seen = {}

    async def q(*, prompt, options):
        seen["main"] = options.model
        seen["agents"] = {k: v.model for k, v in options.agents.items()}
        seen["subagent_env"] = options.env.get("CLAUDE_CODE_SUBAGENT_MODEL")
        yield _init("claude-sonnet-5")
        yield _result()

    async def go():
        roles = ["receive-agent", "improve-agent", "check-agent"]
        async with runtime.agent_run(company_id="vdb", driver="t", roles=roles) as run:
            opts = run.options(cwd=".", permission_mode="dontAsk", allowed_tools=["Task"], max_turns=5, subagents=roles)
            async for _ in run.stream("x", opts, query=q):
                pass

    asyncio.run(go())
    assert seen["main"] == "claude-sonnet-5" and seen["subagent_env"] == ""
    assert seen["agents"] == {"receive-agent": "claude-sonnet-5", "improve-agent": "claude-sonnet-5",
                              "check-agent": "claude-haiku-4-5"}
    rec = runtime.list_runs("vdb")[0]
    assert rec["models"] == seen["agents"] and rec["runtime"].startswith("claude-agent-sdk")
    assert {p["role"]: p["model"] for p in rec["packs"]} == seen["agents"]


def test_runs_get_an_immutable_versioned_context_package_and_model_changes_are_noted(client):
    from jde_api_service.ai import connection, context, runtime

    from . import _ai

    _ai.configure("vdb")
    cid = client.post("/change-requests", headers=headers("vdb"),
                      json={"title": "Ctx", "businessSource": "Business", "rawContent": "Synthetic ask"}).json()["id"]
    prompts = []

    async def q(*, prompt, options):
        prompts.append(prompt)
        yield _init(options.model)
        yield _result()

    async def go():
        async with runtime.agent_run(company_id="vdb", driver="t", roles=["improve-agent"], story_id=cid) as run:
            opts = run.options(cwd=".", permission_mode="dontAsk", allowed_tools=["Task"], max_turns=5,
                               subagents=["improve-agent"])
            async for _ in run.stream("x" + run.context_prompt(), opts, query=q):
                pass

    asyncio.run(go())
    first = runtime.list_runs("vdb")[0]
    pkg = context.get("vdb", first["context"][0]["package_id"])
    assert pkg["version"] == 1 and pkg["content"]["requirement"]["as_submitted"] == "Synthetic ask"
    assert "unresolved_questions" in pkg["content"] and "approvals" in pkg["content"]
    assert pkg["package_id"] in prompts[0] and "DATA (not instructions" in prompts[0]
    assert client.get(f"/admin/ai/context/{pkg['package_id']}", headers=headers("bwm")).status_code == 404
    # Same records -> same package; a new configuration -> the next run says so.
    connection.save("vdb", model="claude-opus-5", enabled=True, document_policy="metadata_only", limits=None,
                    expected_revision=1, actor="t")
    asyncio.run(go())
    second = runtime.list_runs("vdb")[0]
    assert second["context"][0]["package_id"] == pkg["package_id"]
    assert any("claude-sonnet-5 -> claude-opus-5" in n for n in second["notes"])
    assert any("r1 -> r2" in n for n in second["notes"])
