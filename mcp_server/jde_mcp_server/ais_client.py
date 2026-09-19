"""
A thin wrapper around the JD Edwards AIS Server's REST API.

This does NOT implement the real Form Service Request payload for
set_processing_option yet — that has to come from the manual validation
spike in Section 10.2, step 2 of the design document, where someone
records the exact sequence of field/button events against the
Processing Option Revisions form for your specific Tools Release. Drop
that recorded JSON into `FSR_SET_PROCESSING_OPTION` below once you have
it; until then, MOCK_MODE keeps everything runnable end to end.

Reference: Section 7.3 (MVP tool set), Appendix C.2 (Form Service
Requests, Performing AIS Form Service Calls).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from .config import settings
from .backlog import require_approved
from .scope import reject_if_oracle_owned_version, check_functional_scope, check_allowed_value
from .approval import require_exact_change, require_change_covers_test

# Fill this in with the exact, validated FSR event sequence from the
# manual spike (Section 10.2, step 2) before flipping MOCK_MODE off for
# the functional write path. Leave it empty until then -- the code below
# refuses to send a write in live mode without it, on purpose.
FSR_SET_PROCESSING_OPTION: dict | None = None


class AISClientError(RuntimeError):
    pass


class AISClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._http = httpx.Client(timeout=30.0)

    # ---- auth -------------------------------------------------------
    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        settings.require_live_config()
        resp = self._http.post(
            f"{settings.ais_base_url}/jderest/tokenrequest",
            json={
                "username": settings.ais_username,
                "password": settings.ais_password,
                "deployment": "AIS",
                "environment": settings.ais_environment,
                "role": settings.ais_role,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["userInfo"]["token"]
        # AIS tokens are typically valid for a configured session window;
        # refresh a little early to be safe.
        self._token_expiry = time.time() + 25 * 60
        return self._token

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "AIS-Auth-Token": self._ensure_token()}

    # ---- discovery (Section 7.2, proven since Tools Release 9.1.4.6) -
    # Deliberately NOT gated on story approval: Section 7.2 treats
    # discovery as universally safe, and the Improve Agent (Section
    # 5.3.2) needs it in Phase 1, long before a story reaches Phase 2
    # approval. The gate applies to writes and test execution below,
    # where an unapproved story could actually cause something to
    # happen in JDE -- not to read-only lookups.
    def get_processing_options(self, application: str, version: str) -> dict[str, Any]:
        if settings.mock_mode:
            return _mock_fixture("get_processing_options", application=application, version=version)
        resp = self._http.get(
            f"{settings.ais_base_url}/jderest/v3/processingOption/{application}/{version}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def get_object(self, object_name: str) -> dict[str, Any]:
        if settings.mock_mode:
            return _mock_fixture("get_object", object_name=object_name)
        resp = self._http.get(
            f"{settings.ais_base_url}/jderest/v2/discovery/objects/{object_name}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def get_version(self, application: str, version: str) -> dict[str, Any]:
        if settings.mock_mode:
            return _mock_fixture("get_version", application=application, version=version)
        resp = self._http.get(
            f"{settings.ais_base_url}/jderest/v2/discovery/objects/{application}/versions/{version}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    # ---- the one validated functional write (Section 7.3) -----------
    # GATED, four separate ways, all of which must pass:
    #   1. Gate 2 (Section 3.5) -- has a human approved this STORY at all
    #   2. Exact-change approval (Section 15.3/16.2) -- has a human
    #      approved THIS SPECIFIC operation, not just some change to
    #      this story. Fails closed if the operation doesn't match
    #      byte-for-byte what was approved.
    #   3. Universal rule (Appendix B.4) -- is this an Oracle-owned
    #      version (XJDE/ZJDE) that must never be written to directly
    #   4. Engagement scope (Appendix D.2) -- is this specific
    #      application/version/option/value combination one this
    #      customer has actually authorised
    # None of these substitutes for another. A story can be approved,
    # the exact change can be approved, and the write can still be an
    # Oracle template or an out-of-scope option -- all four must hold.
    def set_processing_option(
        self, story_id: str, change_id: str, application: str, version: str, option: str, value: str
    ) -> dict[str, Any]:
        require_approved(story_id)
        operation = {"tool": "set_processing_option", "story_id": story_id, "application": application, "version": version, "option": option, "value": value}
        require_exact_change(change_id, operation)
        reject_if_oracle_owned_version(version)
        scope_entry = check_functional_scope(application, version, option)
        check_allowed_value(scope_entry, value)
        if settings.mock_mode:
            return _mock_fixture(
                "set_processing_option",
                application=application,
                version=version,
                option=option,
                value=value,
                previous_value="MOCK-PREVIOUS-VALUE",
            )
        if FSR_SET_PROCESSING_OPTION is None:
            raise AISClientError(
                "No validated Form Service Request recorded yet for "
                "set_processing_option. Complete the manual spike in "
                "Section 10.2, step 2, and populate "
                "FSR_SET_PROCESSING_OPTION in ais_client.py before using "
                "this tool against a real environment."
            )
        payload = _fill_fsr_template(FSR_SET_PROCESSING_OPTION, application, version, option, value)
        resp = self._http.post(
            f"{settings.ais_base_url}/jderest/formservice",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    # ---- runtime / evidence -------------------------------------------
    # GATED: bound to the same approved change as the write it's meant
    # to verify (Section 17.1) -- not just "any test, on any approved
    # story." Running a different test than the one a human saw
    # approved would defeat the point of the exact-change approval.
    def run_orchestration(self, story_id: str, change_id: str, name: str, payload: dict) -> dict[str, Any]:
        require_approved(story_id)
        require_change_covers_test(change_id, name)
        if settings.mock_mode:
            return _mock_fixture("run_orchestration", name=name, payload=payload, result="PASS")
        resp = self._http.post(
            f"{settings.ais_base_url}/jderest/v2/orchestrator/{name}",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


def _fill_fsr_template(template: dict, application: str, version: str, option: str, value: str) -> dict:
    """Substitutes the four story-specific values into whatever shape the
    recorded FSR template turns out to have. Adjust this once you have a
    real recording -- the placeholder tokens below are a starting
    convention, not a fixed AIS requirement."""
    raw = json.dumps(template)
    raw = (
        raw.replace("{{APPLICATION}}", application)
        .replace("{{VERSION}}", version)
        .replace("{{OPTION}}", option)
        .replace("{{VALUE}}", value)
    )
    return json.loads(raw)


def _mock_fixture(tool_name: str, **kwargs) -> dict[str, Any]:
    """Deterministic canned responses so the rest of the pipeline
    (subagents, hooks, evidence capture) can be built and demoed before
    real JDE access exists. Swap MOCK_MODE off once Section 10.2 steps
    1-2 are done."""
    return {"mock": True, "tool": tool_name, "request": kwargs, "note": "MOCK_MODE response -- not real JDE data"}


client = AISClient()
