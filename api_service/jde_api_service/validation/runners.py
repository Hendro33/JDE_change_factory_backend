"""Live JDE adapters. No outcome is fabricated and writes are never retried."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from types import SimpleNamespace
from urllib.parse import urlparse

from ..discovery import certificates, transport
from ..executors.browser import Session as BrowserSession, available, spki_pins
from ..persistence import blob_store
from ..services import credential_crypto
from . import agents, service as s


class Blocked(RuntimeError):
    pass


def pem_for(env):
    cert = certificates.get(env["company_id"], env["certificate_sha"]) if env["certificate_sha"] else None
    if env["certificate_sha"] and not cert:
        raise Blocked("The saved certificate is missing")
    return cert["pem"] if cert else ""


def connect(env):
    if not env["ais_url"] or not env["username"] or not env.get("credential"):
        raise Blocked("Save the AIS endpoint and a dedicated test account in Administration > Validation")
    trust = transport.Trust(host=urlparse(env["ais_url"]).hostname or "", ca_pem=pem_for(env),
                            ca_sha256=env["certificate_sha"])
    client = transport.LiveAisTransport(env["company_id"], env["ais_url"], min(env["timeout_seconds"], 60), trust=trust)
    try:
        session = client.authenticate(env["username"], credential_crypto.decrypt(env["credential"]),
                                      env["jde_environment"], env["jde_role"])
        if session.context.get("environment") != env["jde_environment"]:
            client.logout(session)
            raise Blocked("AIS did not verify the configured JDE environment identity")
        if env["jde_role"] and session.context.get("role") != env["jde_role"]:
            client.logout(session)
            raise Blocked("AIS did not verify the configured test role")
        return client, session
    except BaseException:
        client._client.close()
        raise


def test_connection(env):
    client, session = connect(env)
    try:
        return {"status": "verified", "environment": session.context["environment"],
                "role": session.context.get("role"), "at": s.now(), "revision": env["revision"]}
    finally:
        client.logout(session)
        client._client.close()


def redact(value):
    if isinstance(value, dict):
        return {k: "[redacted]" if any(w in k.lower() for w in ("password", "token", "secret", "authorization"))
                else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def substitute(value, params):
    if isinstance(value, dict):
        return {k: substitute(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, params) for v in value]
    if isinstance(value, str):
        def replace(match):
            if match[1] not in params:
                raise Blocked(f"Missing test data parameter: {match[1]}")
            return params[match[1]]
        return re.sub(r"\$\{([A-Za-z0-9_]+)\}", replace, value)
    return value


def mutation(step):
    # Conservatively treat UI interactions and all AIS POSTs as potentially committing.
    return step["mutates"] or step["operation"] in ("click", "fill", "select", "press") or (
        step["operation"] == "ais" and step["method"] == "POST")


def check_step(env, step):
    if mutation(step) and (not env["allow_writes"] or not env["side_effects_isolated"]):
        raise Blocked("This action may commit a transaction; writes and downstream effects need explicit environment approval")
    if step["operation"] == "ais":
        path = step["path"]
        if path not in env["allowed_ais_paths"] or any(c in path for c in ("..", ":", "?", "#", "%", "\\")):
            raise Blocked("The AIS path is not explicitly allowed in this environment")
        if step["method"] == "GET" and path != "/defaultconfig" and not env["allow_writes"]:
            raise Blocked("Only default configuration reads are permitted without write authority")
    if step["operation"] in ("click", "fill", "select") and not step["target"]:
        raise Blocked("The approved action needs a target control label")


def assess(step, actual):
    observed = actual
    found = True
    if step["result_path"]:
        for key in step["result_path"].split("."):
            if isinstance(observed, dict):
                found = found and key in observed
                observed = observed.get(key)
            elif isinstance(observed, list) and key.isdigit() and int(key) < len(observed):
                observed = observed[int(key)]
            else:
                found = False
                observed = None
                break
    expected = step["expected_value"]
    if step["assertion"] == "human":
        outcome = "needs_review"
    elif not found:
        outcome = "failed"
    elif step["assertion"] == "exists":
        outcome = "passed" if observed is not None and observed != "" else "failed"
    elif step["assertion"] == "equals":
        outcome = "passed" if observed == expected else "failed"
    else:
        outcome = "passed" if expected is not None and str(expected) in str(observed) else "failed"
    return {"step_id": step["id"], "action": step["action"], "expected": step["expected"],
            "assertion": step["assertion"], "expected_value": expected, "observed": redact(observed), "outcome": outcome}


class ValidationBrowser(BrowserSession):
    """Reuse the restricted Chromium session without borrowing DEV delivery credentials."""
    def __init__(self, env, run, scenario_id, guard):
        self.company_id = env["company_id"]
        self.config = SimpleNamespace(web_client_url=env["web_url"])
        self.hosts = {urlparse(env["web_url"]).hostname.lower()}
        self.environment, self.role = env["jde_environment"], env["jde_role"]
        self.pins = spki_pins([pem_for(env)])
        self.key_prefix = f"validation/{env['company_id']}/{run['id']}"
        self.log, self.screenshots, self.forbidden = [], [], ("sign out", "logout", "log out", "promote", "deploy")
        self.steps, self.changed = 0, False
        self._pw = self._context = self.page = self._profile_dir = None
        self.blocked = []
        self.env, self.run, self.scenario_id, self.guard = env, run, scenario_id, guard

    def _count(self):
        self.guard()
        super()._count()
        if self.steps > self.env["max_actions"]:
            raise Blocked("Browser action budget exhausted")

    async def shot(self, step):
        self.guard()
        masks = [frame.locator(selector) for frame in self.page.frames
                 for selector in ["input[type=password]", *self.env["redaction_selectors"]]]
        data = await self.page.screenshot(full_page=False, mask=masks)
        eid = s.evidence(self.company_id, self.run["id"], self.scenario_id,
                         step.replace("DEV write user", "test account")[:180], "image/png", data, "browser")
        item = {"n": len(self.screenshots) + 1, "id": eid, "at": s.now()}
        self.screenshots.append(item)
        return item

    async def look(self):
        observation = await super().look()
        # Mask iframe fields and their values before passing observations to an agent.
        hidden_refs, secrets = set(), []
        for frame in self.page.frames:
            for selector in ["input[type=password]", *self.env["redaction_selectors"]]:
                for loc in await frame.locator(selector).all():
                    ref = await loc.get_attribute("data-jade-ref")
                    if ref:
                        hidden_refs.add(ref)
                    text = await loc.text_content() or ""
                    value = await loc.evaluate("el => el.value || ''")
                    secrets.extend(v for v in (text, value) if v)
        observation["controls"] = [c for c in observation["controls"] if c.get("ref") not in hidden_refs and c.get("type") != "password"]
        def scrub(value):
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, "[redacted]")
                return value
            if isinstance(value, list):
                return [scrub(v) for v in value]
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items()}
            return value
        observation = scrub(observation)
        return observation


async def browser_scenario(env, run, attempt, guard, record):
    ok, why = available()
    if not ok or not env["web_url"]:
        raise Blocked(why if not ok else "Save the JDE web client endpoint")
    if not env.get("browser_environment_selector") or not env.get("browser_role_selector"):
        raise Blocked("Configure the browser environment and role identity controls before browser testing")
    b = ValidationBrowser(env, run, attempt["scenario_id"], guard)
    await b.start()
    try:
        await b.sign_in(env["username"], credential_crypto.decrypt(env["credential"]))
        for selector, expected in ((env["browser_environment_selector"], env["jde_environment"]),
                                   (env["browser_role_selector"], env["jde_role"])):
            matches = [loc for frame in b.page.frames for loc in await frame.locator(selector).all()]
            if len(matches) != 1:
                raise Blocked("The browser session identity control is missing or ambiguous")
            value = await matches[0].evaluate("el => String(el.value || el.innerText || '').trim()")
            if value != expected:
                raise Blocked("The browser session does not match the configured environment and role")
        for raw in attempt["body"]["steps"]:
            guard()
            step = substitute(raw, env["parameters"])
            check_step(env, step)
            op = step["operation"]
            observation = await b.look()
            selection = None
            if op in ("click", "fill", "select"):
                controls = observation["controls"]
                target = step["target"].casefold()
                matches = [c for c in controls if target in [str(c.get(k, "")).casefold() for k in ("label", "text", "id", "name")]]
                if attempt["body"]["route"] == "computer_use":
                    record({"event": "agent_call", "step_id": step["id"], "role": "test-executor"})
                    selection = await agents.choose_control(env["company_id"], run["initiated_by"], observation, step)
                    ref = selection["result"].get("ref")
                    matches = [c for c in matches if c["ref"] == ref]
                if len(matches) != 1:
                    raise Blocked("The approved control could not be uniquely identified; review the binding")
                ref = matches[0]["ref"]
                record({"event": "dispatch", "step_id": step["id"], "may_write": True})
                if op == "click":
                    await b.click(ref)
                elif op == "fill":
                    await b.fill(ref, step["value"])
                else:
                    await b.select(ref, step["value"])
            elif op == "open":
                record({"event": "dispatch", "step_id": step["id"], "may_write": False})
                await b.open(step["target"] or step["value"] or env["web_url"])
            elif op == "press":
                record({"event": "dispatch", "step_id": step["id"], "may_write": True})
                await b.press(step["value"])
            elif op != "observe":
                raise Blocked("Unsupported browser action")
            if step["wait_seconds"]:
                await asyncio.sleep(step["wait_seconds"])
            guard()
            actual = await b.look()
            result = assess(step, actual["text"])
            result["evidence"] = [x["id"] for x in b.screenshots]
            result["agent"] = selection
            record({"event": "step", **result})
            if result["outcome"] == "failed":
                break
    finally:
        await b.close()


def ais_scenario(env, run, attempt, guard, record):
    client, session = connect(env)
    try:
        for raw in attempt["body"]["steps"]:
            guard()
            step = substitute(raw, env["parameters"])
            check_step(env, step)
            record({"event": "dispatch", "step_id": step["id"], "may_write": mutation(step)})
            actual = client._send(step["method"], step["path"], token=session.token, body=step["body"] or None)
            result = assess(step, actual)
            # Tokens/secrets are stripped before any response is persisted.
            eid = s.evidence(env["company_id"], run["id"], attempt["scenario_id"], "AIS observation", "application/json",
                             json.dumps(redact(actual)).encode(), "ais")
            result["evidence"] = [eid]
            record({"event": "step", **result})
            if result["outcome"] == "failed":
                break
    finally:
        client.logout(session)
        client._client.close()
