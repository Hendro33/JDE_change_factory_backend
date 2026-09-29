"""
Test the agent execution settings -- an explicit Admin action, like Test
Connection for discovery. Nothing is changed in JD Edwards:

  * AIS: the DEV write user signs in to the customer's AIS server with the
    DEV environment and its role, Jade checks the environment the session
    reports, and signs out.
  * Web client: Jade opens the web client in the browser executor and signs
    in as the write user (a screenshot is kept), then closes the browser.

A result counts only for the settings it was run against: any material
change afterwards marks it stale.
"""

from __future__ import annotations

import asyncio
import time

from . import settings


def test(company_id: str) -> dict:
    s = settings.load(company_id)
    if s is None:
        raise settings.SettingsNotFound("save the agent execution settings first")
    tested = settings.current_hash(s)
    results = {"ais_write_sign_in": _ais(company_id, s)}
    if s["config"].web_client_url:
        results["web_client"] = _web_client(company_id)
    else:
        results["web_client"] = ("failed", "no web client address is saved")
    for check, (state, detail) in results.items():
        settings.record_check(company_id, check, state, detail, tested_hash=tested)
    settings.audit(company_id, "tested", "; ".join(f"{c}: {st} -- {d}" for c, (st, d) in results.items()), "Jade")
    return {c: {"state": st, "detail": d} for c, (st, d) in results.items()}


def _ais(company_id: str, s: dict) -> tuple[str, str]:
    from ..discovery import profile_service, service as discovery_service, transport

    profile = profile_service.load(company_id)
    if profile is None:
        return "failed", "the customer's JD Edwards connection (AIS address) is not saved"
    config = profile["config"]
    try:
        username, password = settings.write_credential(company_id)
        client = transport.transport_for(company_id, config, live_transport=discovery_service.LIVE_HTTP_TRANSPORT,
                                         trust=profile_service.trust_for(company_id, config))
    except Exception as exc:  # noqa: BLE001 -- reported, nothing sent
        return "failed", str(exc)
    role = s["config"].write_role or config.role
    try:
        session = client.authenticate(username, password, config.environment, role)
    except transport.TransportError as exc:
        return "failed", f"the DEV write user could not sign in: {exc}"
    try:
        env = session.context.get("environment")
        if env and env != config.environment:
            return "failed", f"the session runs in {env}, not the configured DEV environment {config.environment}"
        if session.context.get("username") and session.context["username"].upper() == (
                profile.get("credential_username") or "").upper():
            return "failed", "the write user is the read-only discovery user; use a separate DEV write user"
        return "ok", (f"signed in as the DEV write user in {env or config.environment} with role {role} "
                      f"({'environment confirmed by the session' if env else 'the session did not report its environment'})")
    finally:
        client.logout(session)


def _web_client(company_id: str) -> tuple[str, str]:
    from . import browser

    ok, why = browser.available()
    if not ok:
        return "failed", why
    try:
        username, password = settings.write_credential(company_id)
        shots: list[dict] = []
        session = browser.Session(company_id, f"executions/{company_id}/settings-test/{int(time.time() * 1000)}", [],
                                  shots)
    except Exception as exc:  # noqa: BLE001
        return "failed", str(exc)

    async def run() -> None:
        await session.start()
        try:
            await session.sign_in(username, password)
        finally:
            await session.close()

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        return "failed", str(exc)
    return "ok", f"signed in to the web client as the DEV write user (screenshot {shots[-1]['storage_key']})"
