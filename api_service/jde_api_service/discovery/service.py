"""
The discovery service: the only code that turns a request for evidence
into a JDE read.

Every request -- from the Architect's tools, an Admin's sample read, or a
Refresh Evidence action -- is validated HERE, outside the model, before
any network dispatch: the story's company (re-resolved from the backend's
own link), that company's profile, its revision, enabled state, DEV
environment, window, capability status, approved targets/fields/filters,
record limits, the circuit breaker and the one-at-a-time lock. A refusal
is logged as a blocked request and nothing is sent.

Results come back as structured, sanitised evidence with provenance. What
the external model may see is governed by the profile's data-sharing
policy; values it may not see are redacted, and the limitation is stated.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ..persistence.db import connection
from ..services import credential_crypto
from . import capabilities, profile_service, transport
from .models import ApprovedRead, JdeProfileConfig

# Tests may inject an httpx transport for the live adapter (never used in production).
LIVE_HTTP_TRANSPORT = None
MAX_GRANT_SECONDS = 2 * 3600
_VALUE_MAX = 200


class DiscoveryBlocked(RuntimeError):
    """Refused by policy before anything was sent."""


class DiscoveryFailed(RuntimeError):
    """Sent (or attempted) but the endpoint failed; no retry is made."""


@dataclass(frozen=True)
class DiscoveryGrant:
    """Who may read, for which story, under which profile revision, until
    when. Built by the backend from trusted records -- never by the model."""

    company_id: str
    story_id: Optional[str]
    domain_id: Optional[str]
    profile_revision: int
    agent_run_id: Optional[str]
    actor_user_id: Optional[str]
    expires_at: float
    purpose: str = "architect"


# ---------------------------------------------------------------------
# One request at a time per company, and what is in flight
# ---------------------------------------------------------------------
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_in_flight: dict[str, dict[str, Any]] = {}


def _lock_for(company_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(company_id, threading.Lock())


def in_flight(company_id: str) -> list[dict[str, Any]]:
    with _locks_guard:
        entry = _in_flight.get(company_id)
        return [dict(entry)] if entry else []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# Activity (sanitised) and observations (immutable)
# ---------------------------------------------------------------------
def _target_summary(capability_id: str, target: str, fields: list[str], filters: list[dict]) -> str:
    """Operation shape only: filter VALUES are never logged."""
    parts = [capability_id, target or "-"]
    if fields:
        parts.append("[" + ",".join(fields[:12]) + "]")
    if filters:
        parts.append("where " + " and ".join(f"{f.get('field')} {f.get('op')} ?" for f in filters[:6]))
    return " ".join(parts)[:300]


def log_activity(*, request_id: str, company_id: str, profile_revision: Optional[int], grant: Optional[DiscoveryGrant],
                 actor_user_id: Optional[str], operation: str, target: str, mode: Optional[str], started: float,
                 result_count: Optional[int], outcome: str, reason: str = "") -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO discovery_activity (request_id, company_id, profile_revision, actor_user_id, agent_run_id, "
            "story_id, operation, target, mode, started_at, duration_ms, result_count, outcome, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, company_id, profile_revision, actor_user_id or (grant.actor_user_id if grant else None),
             grant.agent_run_id if grant else None, grant.story_id if grant else None, operation, target, mode,
             datetime.fromtimestamp(started, timezone.utc).isoformat(), int((time.time() - started) * 1000),
             result_count, outcome, _sanitise_reason(reason)),
        )


def _sanitise_reason(reason: str) -> str:
    text = reason.replace("\n", " ")[:300]
    for marker in ("password", "token=", "jde-ais-auth"):
        if marker in text.lower():
            return "details withheld from the log (may contain a credential)"
    return text


def list_activity(company_id: str, limit: int = 100) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT a.*, u.display_name AS actor_name FROM discovery_activity a "
            "LEFT JOIN users u ON u.id = a.actor_user_id WHERE a.company_id = ? ORDER BY a.id DESC LIMIT ?",
            (company_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_observation(company_id: str, observation_id: str) -> Optional[dict]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM discovery_observations WHERE id = ? AND company_id = ?",
                           (observation_id, company_id)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["request"], d["evidence"] = json.loads(d["request"]), json.loads(d["evidence"])
    return d


# ---------------------------------------------------------------------
# Data-sharing policy
# ---------------------------------------------------------------------
def _redact(value: Any) -> str:
    if value in (None, ""):
        return "[blank]"
    text = str(value)
    kind = "number" if text.replace(".", "", 1).replace("-", "", 1).isdigit() else "text"
    return f"[redacted {kind}, {len(text)} chars]"


def model_may_see_values(policy: str, data_class: str) -> bool:
    if policy == "full":
        return True
    if policy == "configuration_and_artifacts":
        return data_class == "configuration"
    return False


def apply_sharing(policy: str, cap: capabilities.DiscoveryCapability, records: list[dict]) -> tuple[list[dict], dict]:
    allowed = model_may_see_values(policy, cap.data_class)
    cleaned = []
    for rec in records:
        row = {}
        for k, v in rec.items():
            v = None if v is None else str(v)[:_VALUE_MAX]
            row[k] = v if allowed else _redact(v)
        cleaned.append(row)
    note = ("values shared under the company's data-sharing policy" if allowed else
            f"values redacted: the company's data-sharing policy ({policy}) does not allow {cap.data_class.replace('_', ' ')} "
            "values in external-model prompts. Structure, counts and field names only.")
    return cleaned, {"policy": policy, "values_shared": allowed, "note": note}


# ---------------------------------------------------------------------
# Validation -- all of it before any network dispatch
# ---------------------------------------------------------------------
def _approved_read(config: JdeProfileConfig, capability_id: str) -> Optional[ApprovedRead]:
    return next((r for r in config.approved_reads if r.capability_id == capability_id), None)


def validate(grant: DiscoveryGrant, capability_id: str, target: str, fields: list[str], filters: list[dict],
             max_records: int, *, require_enabled: bool = True) -> tuple[dict, capabilities.DiscoveryCapability, list[str], int]:
    if time.time() > grant.expires_at:
        raise DiscoveryBlocked("the discovery grant for this run has expired")
    if grant.story_id is not None:
        from ..services.registry import get_customer_link_service

        linked = get_customer_link_service().customer_for(grant.story_id)
        if linked != grant.company_id:
            raise DiscoveryBlocked("the story is not linked to this company; another company's profile is never used")
    profile = profile_service.load(grant.company_id)
    if profile is None:
        raise DiscoveryBlocked("this company has no JDE discovery profile")
    config: JdeProfileConfig = profile["config"]
    if profile["disabled"]:
        raise DiscoveryBlocked("the JDE discovery connection is disabled")
    if require_enabled:
        if not profile_service.is_active(profile):
            raise DiscoveryBlocked("discovery is not enabled for the current profile revision")
        if profile["revision"] != grant.profile_revision:
            raise DiscoveryBlocked("the profile changed after this run started; a new run is needed")
    if config.environment_purpose not in ("development", "isolated_trial"):
        raise DiscoveryBlocked("discovery is only for development or an approved isolated trial environment")
    if not profile_service.window_open(config):
        raise DiscoveryBlocked("outside the approved discovery window")
    cap = capabilities.get(capability_id)
    if cap is None:
        raise DiscoveryBlocked(f"{capability_id!r} is not a discovery capability (discovery only reads)")
    if cap.base_status == "unavailable":
        raise DiscoveryBlocked(f"{capability_id} is unavailable: {cap.unavailable_reason} {cap.alternative}")
    read = _approved_read(config, capability_id)
    if read is None:
        raise DiscoveryBlocked(f"{capability_id} is not an approved read for this company; expanding scope needs an Admin")
    if require_enabled:
        status, detail = profile_service.capability_status(profile, capability_id)
        if status != "supported":
            raise DiscoveryBlocked(f"{capability_id} is {status}: {detail}")
    if cap.target_kind != "none" and target not in read.targets:
        raise DiscoveryBlocked(f"target {target!r} is not approved for {capability_id} (approved: {', '.join(read.targets)})")
    approved_fields = list(read.fields) or list(cap.fixed_fields)
    fields = list(fields) or approved_fields
    extra = [f for f in fields if f not in approved_fields]
    if extra:
        raise DiscoveryBlocked(f"fields not approved for {capability_id}: {', '.join(extra)}")
    for f in filters:
        if set(f) - {"field", "op", "value"}:
            raise DiscoveryBlocked("a filter has only field, op and value")
        if f.get("field") not in read.filter_fields:
            raise DiscoveryBlocked(f"filtering on {f.get('field')!r} is not approved for {capability_id}")
        if f.get("op") not in capabilities.FILTER_OPERATORS:
            raise DiscoveryBlocked(f"operator {f.get('op')!r} is not allowed")
        value = f.get("value")
        if not isinstance(value, (str, int, float)) or len(str(value)) > 60 or any(c in str(value) for c in "*%;|"):
            raise DiscoveryBlocked("filter values are single literal values of at most 60 characters, no wildcards")
    if len(filters) > 5:
        raise DiscoveryBlocked("at most 5 filters")
    limit = min(config.limits.max_records, capabilities.HARD_MAX_RECORDS)
    if not isinstance(max_records, int) or max_records < 1 or max_records > limit:
        raise DiscoveryBlocked(f"max_records must be 1..{limit} (no paging)")
    if config.connection_mode == "live" and transport.breaker_open(grant.company_id):
        raise DiscoveryBlocked("the connection's circuit breaker is open after repeated failures; try later")
    return profile, cap, fields, max_records


# ---------------------------------------------------------------------
# Session handling around ONE read
# ---------------------------------------------------------------------
def _dispatch(profile: dict, plan: capabilities.ReadPlan) -> tuple[transport.ReadResult, str]:
    config: JdeProfileConfig = profile["config"]
    try:
        client = transport.transport_for(profile["company_id"], config, live_transport=LIVE_HTTP_TRANSPORT,
                                         trust=profile_service.trust_for(profile["company_id"], config))
    except transport.DestinationNotAllowed as exc:
        raise DiscoveryBlocked(str(exc)) from exc
    try:
        username, password = profile_service.credential(profile["company_id"])
    except credential_crypto.CredentialUnreadable as exc:
        raise DiscoveryBlocked(f"no usable discovery credential: {exc}") from exc
    session = client.authenticate(username, password, config.environment, config.role)
    try:
        return client.read(plan, session), client.mode
    finally:
        client.logout(session)


def request_fingerprint(plan: capabilities.ReadPlan) -> str:
    """sha256 of exactly what is sent (method, path, body) -- never the
    credential or session token, which travel separately."""
    blob = json.dumps({"method": plan.method, "path": plan.path, "body": plan.body}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def execute_read(grant: DiscoveryGrant, capability_id: str, target: str = "", fields: Optional[list[str]] = None,
                 filters: Optional[list[dict]] = None, max_records: int = 10, *, refresh_of: Optional[str] = None,
                 require_enabled: bool = True, raw_out: Optional[list] = None) -> dict:
    """Validate, read once, and record. Returns the sanitised evidence.
    Raises DiscoveryBlocked (nothing sent) or DiscoveryFailed."""
    request_id = f"DR-{uuid.uuid4().hex[:12]}"
    fields, filters = list(fields or []), list(filters or [])
    started = time.time()
    summary = _target_summary(capability_id, target, fields, filters)
    profile = profile_service.load(grant.company_id)
    revision = profile["revision"] if profile else None
    mode = profile["config"].connection_mode if profile else None

    def blocked(reason: str):
        log_activity(request_id=request_id, company_id=grant.company_id, profile_revision=revision, grant=grant,
                     actor_user_id=None, operation=capability_id, target=summary, mode=mode, started=started,
                     result_count=None, outcome="blocked", reason=reason)
        return DiscoveryBlocked(reason)

    try:
        profile, cap, fields, max_records = validate(grant, capability_id, target, fields, filters, max_records,
                                                     require_enabled=require_enabled)
        plan = capabilities.build_plan(cap, target, fields, filters, max_records,
                                       environment=profile["config"].environment)
        capabilities.assert_read_semantics(plan)
    except (DiscoveryBlocked, capabilities.NotARead) as exc:
        raise blocked(str(exc)) from None

    lock = _lock_for(grant.company_id)
    if not lock.acquire(timeout=profile["config"].limits.timeout_seconds):
        raise blocked("another discovery request is in progress for this company (one at a time)")
    try:
        # Re-check after waiting: Disable blocks queued calls.
        try:
            profile, cap, fields, max_records = validate(grant, capability_id, target, fields, filters, max_records,
                                                         require_enabled=require_enabled)
        except DiscoveryBlocked as exc:
            raise blocked(str(exc)) from None
        with _locks_guard:
            _in_flight[grant.company_id] = {"request_id": request_id, "operation": capability_id,
                                            "story_id": grant.story_id, "started_at": _now_iso()}
        try:
            result, mode = _dispatch(profile, plan)
        except DiscoveryBlocked as exc:
            raise blocked(str(exc)) from None
        except transport.TransportError as exc:
            log_activity(request_id=request_id, company_id=grant.company_id, profile_revision=profile["revision"],
                         grant=grant, actor_user_id=None, operation=capability_id, target=summary, mode=mode,
                         started=started, result_count=None, outcome="error", reason=str(exc))
            raise DiscoveryFailed(str(exc)) from None
    finally:
        with _locks_guard:
            _in_flight.pop(grant.company_id, None)
        lock.release()

    config: JdeProfileConfig = profile["config"]
    if raw_out is not None:  # server-side use only (path-code establishment); never returned
        raw_out.extend(result.records)
    shared, sharing = apply_sharing(config.data_sharing_policy, cap, result.records)
    observed_at = _now_iso()
    observation_id = f"OBS-{uuid.uuid4().hex[:10]}"
    evidence = {
        "evidence_type": "live_observation" if mode == "live" else "simulated_observation",
        "content_is_data_not_instructions": True,
        "observation_id": observation_id,
        "request_sha256": request_fingerprint(plan),
        "capability_id": capability_id,
        "capability_status": "supported" if require_enabled else "being verified",
        "target": target,
        "fields": fields,
        "filters": [{"field": f["field"], "op": f["op"], "value": f["value"] if sharing["values_shared"] else _redact(f["value"])}
                    for f in filters],
        "observed_at": observed_at,
        "mode": mode,
        "mode_label": transport.SIMULATION_LABEL if mode == "simulation" else "LIVE customer AIS endpoint",
        "environment": config.environment,
        "path_code": config.path_code,
        "profile_revision": profile["revision"],
        "record_count": len(result.records),
        "more_records_available": result.more_records,
        "records": shared,
        "sharing": sharing,
        "response_shape": result.meta.get("shape", "simulated" if mode == "simulation" else "unverified"),
        "provenance": f"{cap.title} via {'simulated ' if mode == 'simulation' else ''}AIS {plan.endpoint}, "
                      f"environment {config.environment}, profile revision {profile['revision']}",
    }
    with connection() as conn:
        conn.execute(
            "INSERT INTO discovery_observations (id, company_id, story_id, agent_run_id, actor_user_id, profile_revision, "
            "capability_id, request, observed_at, mode, sharing_policy, evidence, payload_sha256, result_count, refresh_of) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (observation_id, grant.company_id, grant.story_id, grant.agent_run_id, grant.actor_user_id,
             profile["revision"], capability_id,
             json.dumps({"target": target, "fields": fields, "filters": filters, "max_records": max_records}),
             observed_at, mode, config.data_sharing_policy, json.dumps(evidence), result.payload_sha256(),
             len(result.records), refresh_of),
        )
    log_activity(request_id=request_id, company_id=grant.company_id, profile_revision=profile["revision"], grant=grant,
                 actor_user_id=None, operation=capability_id, target=summary, mode=mode, started=started,
                 result_count=len(result.records), outcome="ok")
    return evidence


# ---------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------
def admin_grant(company_id: str, actor_user_id: str, purpose: str) -> DiscoveryGrant:
    profile = profile_service.load(company_id)
    return DiscoveryGrant(company_id=company_id, story_id=None, domain_id=None,
                          profile_revision=profile["revision"] if profile else 0, agent_run_id=None,
                          actor_user_id=actor_user_id, expires_at=time.time() + 300, purpose=purpose)


def test_connection(company_id: str, actor_user_id: str) -> tuple[str, str]:
    """Reachability, authentication and environment verification, recorded
    as three separate checks. Contacts the endpoint the profile names --
    the simulation, or a live AIS the deployment allows."""
    request_id = f"DR-{uuid.uuid4().hex[:12]}"
    started = time.time()
    profile = profile_service.load(company_id)
    if profile is None:
        raise DiscoveryBlocked("save a discovery profile first")
    config: JdeProfileConfig = profile["config"]
    grant = admin_grant(company_id, actor_user_id, "test_connection")

    def log(outcome: str, reason: str = "", count: Optional[int] = None):
        log_activity(request_id=request_id, company_id=company_id, profile_revision=profile["revision"], grant=grant,
                     actor_user_id=actor_user_id, operation="test_connection", target=config.environment,
                     mode=config.connection_mode, started=started, result_count=count, outcome=outcome, reason=reason)

    problems = []
    if not config.routing_isolation_confirmed:
        problems.append("customer/CNC routing and isolation confirmation")
    if not config.privilege_confirmed:
        problems.append("customer confirmation of a narrowly privileged identity")
    if not profile_service.window_open(config):
        problems.append("an open discovery window")
    if problems:
        log("blocked", "missing: " + ", ".join(problems))
        raise DiscoveryBlocked("Test Connection needs " + ", ".join(problems) + " before Jade contacts the endpoint")
    if config.connection_mode == "live" and transport.breaker_open(company_id):
        log("blocked", "circuit breaker open")
        raise DiscoveryBlocked("the circuit breaker is open after repeated failures; try later")
    try:
        client = transport.transport_for(company_id, config, live_transport=LIVE_HTTP_TRANSPORT,
                                         trust=profile_service.trust_for(company_id, config))
    except transport.DestinationNotAllowed as exc:
        log("blocked", str(exc))
        raise DiscoveryBlocked(str(exc)) from None

    lock = _lock_for(company_id)
    if not lock.acquire(timeout=config.limits.timeout_seconds):
        log("blocked", "another discovery request is in progress")
        raise DiscoveryBlocked("another discovery request is in progress for this company (one at a time)")
    try:
        try:
            reach = client.check_reachability()
            profile_service.record_check(company_id, "reachability", "ok", f"{client.mode}: {reach}")
        except transport.TransportError as exc:
            profile_service.record_check(company_id, "reachability", "failed", str(exc))
            log("error", str(exc))
            return "failed", f"not reachable: {exc}"
        try:
            username, password = profile_service.credential(company_id)
        except credential_crypto.CredentialUnreadable as exc:
            profile_service.record_check(company_id, "authentication", "failed", f"no usable credential: {exc}")
            log("blocked", "no usable credential")
            return "failed", str(exc)
        try:
            session = client.authenticate(username, password, config.environment, config.role)
            profile_service.record_check(company_id, "authentication", "ok",
                                         f"session opened; requested environment {config.environment}, role {config.role}")
        except transport.TransportError as exc:
            profile_service.record_check(company_id, "authentication", "failed", str(exc))
            log("error", str(exc))
            return "failed", f"authentication failed: {exc}"
        try:
            plan = capabilities.build_plan(capabilities.CAPABILITIES["environment_info"], "", [], [], 1,
                                           environment=config.environment)
            server_defaults = (client.read(plan, session).records or [{}])[0]
        except transport.TransportError as exc:
            profile_service.record_check(company_id, "environment", "failed", f"server defaults unreadable: {exc}")
            log("error", str(exc))
            return "failed", f"the AIS server defaults could not be read: {exc}"
        finally:
            client.logout(session)
    finally:
        lock.release()

    profile_service.record_check(company_id, "environment_info", "ok", "defaultconfig read (server defaults)",
                                 capability_id="environment_info")
    state, detail, facets = verify_environment(config, server_defaults, session.context, client.mode)
    profile_service.record_check(company_id, "environment", state, detail, facets=facets)
    if state != "ok":
        log("error" if state == "failed" else "blocked", detail, 1)
        return "failed", detail
    profile_service.clear_disabled(company_id)
    log("ok", "", 1)
    return "ok", detail


def _release_digits(value: str) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def verify_environment(config: JdeProfileConfig, server_defaults: dict, session: dict, mode: str
                       ) -> tuple[str, str, dict]:
    """Identity verification against the documented AIS contract.

    Sources, never mixed up:
      * configured    -- what the profile says (the Admin's intent);
      * session       -- the token-request response for the session Jade
        opened with an explicit environment and role: environment, role,
        jasserver, userInfo.appsRelease;
      * server level  -- defaultconfig: the AIS server's DEFAULT environment
        and role (recorded, never evidence of the session) and the release
        the server reports;
      * JDE data      -- the path code, only from the environment master
        (F00941) through an approved read; never from the environment name;
      * attested      -- what AIS cannot show (OCM routing and isolation).
    Names are compared exactly: JPS920 and PS920 are different until JDE
    evidence says otherwise, and a difference is shown, never corrected."""
    items, missing, mismatch = [], [], []
    live = mode == "live"

    def item(name: str, status: str, source: str, detail: str, configured=None, reported=None) -> None:
        items.append({"item": name, "status": status, "source": source, "detail": detail,
                      "configured": configured, "reported": reported})
        if status == "missing":
            missing.append(f"{name}: {detail}")
        elif status == "mismatch":
            mismatch.append(f"{name}: {detail}")

    env = session.get("environment")
    if env is None:
        item("session environment", "missing", "AIS token response",
             "the token-request response did not state the session's environment; it cannot be verified",
             config.environment, None)
    elif str(env) != config.environment:
        item("session environment", "mismatch", "AIS token response",
             f"configured {config.environment!r}, the authenticated session reports {env!r}. Jade does not alias or "
             "substitute environment names: this stays blocked until JDE evidence establishes which environment is "
             "meant and the configuration is corrected by an Admin", config.environment, env)
    else:
        item("session environment", "verified", "AIS token response", f"session reports {env!r}", config.environment, env)
    role = session.get("role")
    if role is None:
        item("session role", "missing", "AIS token response", "the token-request response did not state the session's role",
             config.role, None)
    elif str(role).strip().upper() in {"*ALL", "ALL", "*"}:
        item("session role", "mismatch", "AIS token response",
             f"the session runs with role {role!r}, which is not acceptable for Jade discovery: a dedicated, restricted "
             "role is required. Authentication works, but the connection is not ready", config.role, role)
    elif str(role) != config.role:
        item("session role", "mismatch", "AIS token response", f"configured {config.role!r}, session reports {role!r}",
             config.role, role)
    else:
        item("session role", "verified", "AIS token response", f"session reports {role!r}", config.role, role)
    apps = session.get("apps_release")
    exp_digits, got_digits = _release_digits(config.expected_application_release), _release_digits(apps or "")
    if apps is None:
        item("application release", "missing", "AIS token response (userInfo.appsRelease)",
             "userInfo.appsRelease was not returned; the application release cannot be verified",
             config.expected_application_release, None)
    elif not (got_digits.startswith(exp_digits) or exp_digits.startswith(got_digits)):
        item("application release", "mismatch", "AIS token response (userInfo.appsRelease)",
             f"expected {config.expected_application_release!r}, session reports {apps!r}", config.expected_application_release, apps)
    else:
        item("application release", "verified", "AIS token response (userInfo.appsRelease)", f"session reports {apps!r}",
             config.expected_application_release, apps)
    attested = config.runtime_attestation_confirmed and bool(config.runtime_attestation_evidence.strip())
    release_keys = {k: v for k, v in server_defaults.items() if k not in DEFAULT_IDENTITY_KEYS and k != "used_as_evidence"}
    if live:
        exp = _release_digits(config.expected_tools_release)
        matching = {k: v for k, v in release_keys.items() if exp and _release_digits(str(v)).startswith(exp)}
        if matching:
            k, v = next(iter(matching.items()))
            item("Tools / server release", "verified", f"AIS server defaults ({k}; server level, not the session)",
                 f"server reports {v!r}", config.expected_tools_release, v)
        elif release_keys:
            item("Tools / server release", "mismatch", "AIS server defaults (server level)",
                 f"expected {config.expected_tools_release!r}, server reports "
                 + ", ".join(f"{k} {v!r}" for k, v in release_keys.items()), config.expected_tools_release, release_keys)
        else:
            item("Tools / server release", "missing", "AIS server defaults",
                 "the server did not report a release", config.expected_tools_release, None)
        item("path code", "pending", "JDE environment master (F00941) through an approved read",
             "not established yet: AIS does not report the path code, and Jade never derives it from the environment "
             "name. It is established by an approved read of F00941 for the session environment"
             + (f" (CNC statement recorded, not accepted as proof: {config.runtime_attestation_evidence.strip()})" if attested else ""),
             config.path_code or None, None)
    else:
        for label, value in (("Tools / server release", config.expected_tools_release), ("path code", config.path_code)):
            item(label, "attested" if attested else "missing", "customer/CNC attestation (SIMULATION)",
                 f"{value!r}: {config.runtime_attestation_evidence.strip()}" if attested else
                 f"no attestation of the {label}", value, None)
    routing = config.routing_isolation_confirmed and bool(config.isolation_evidence.strip())
    item("OCM data-source routing and isolation", "attested" if routing else "missing",
         "customer/CNC attestation (not observable through AIS)",
         config.isolation_evidence.strip() if routing else "no customer/CNC attestation of OCM routing and isolation")
    notes = ["defaultconfig describes server defaults only; it is recorded but never used as evidence of the session"]
    default_env, default_role = server_defaults.get("defaultEnvironment"), server_defaults.get("defaultRole")
    if default_env and default_env != config.environment:
        notes.append(f"the server's default environment is {default_env!r} and the configured environment is "
                     f"{config.environment!r}; Jade always requests {config.environment!r} explicitly and compares it "
                     "exactly with what the session reports")
    if default_role and str(default_role).upper() == "*ALL":
        notes.append("the server's default role is *ALL; Jade always requests the configured dedicated role explicitly")
    facets = {
        "configured": {"environment": config.environment, "role": config.role,
                       "application_release": config.expected_application_release,
                       "tools_release": config.expected_tools_release, "path_code": config.path_code or None},
        "session_context": {**session, "source": "AIS v2 token-request response"},
        "server_defaults": {**server_defaults, "used_as_evidence": False},
        "attested": {"runtime": config.runtime_attestation_evidence.strip() if attested else None,
                     "ocm_routing_isolation": config.isolation_evidence.strip() if routing else None},
        "items": items, "missing_evidence": missing, "notes": notes,
        "mode": mode, "contract_basis": "Oracle AIS REST API v2 tokenrequest and defaultconfig (documented fields); "
                                        "response shapes to be confirmed against the customer's release",
    }
    facets["expected"] = facets["configured"]  # earlier readers
    if mismatch:
        return "failed", "identity does not match: " + "; ".join(mismatch), facets
    if missing:
        return "unknown", "identity not verifiable -- missing evidence: " + "; ".join(missing), facets
    verified = [i["item"] for i in items if i["status"] == "verified"]
    attested_items = [i["item"] for i in items if i["status"] == "attested"]
    return "ok", (f"{mode}: verified: {', '.join(verified)}"
                  + (f"; customer-attested: {', '.join(attested_items)}" if attested_items else "")), facets


DEFAULT_IDENTITY_KEYS = {"defaultEnvironment", "defaultRole", "defaultJasServer"}


PATH_CODE_TABLE, PATH_CODE_ENV_FIELD, PATH_CODE_FIELD = "F00941", "EMENHV", "EMPATHCD"


def _bound_sample(profile: dict, capability_id: str, target: Optional[str], fields: Optional[list[str]],
                  filters: Optional[list[dict]], max_records: int) -> tuple:
    """The one request a sample read may send: the approved capability, one
    approved target, EXACTLY the approved columns, approved filters and at
    most the profile's record limit. Nothing about the AIS endpoint or the
    request body comes from the browser."""
    read = _approved_read(profile["config"], capability_id)
    if read is None:
        raise DiscoveryBlocked(f"{capability_id} is not an approved read")
    cap = capabilities.get(capability_id)
    if target is None:  # callers that predate explicit selection
        target = read.targets[0] if read.targets else ""
    if cap is not None and cap.target_kind != "none" and not target:
        raise DiscoveryBlocked(f"select one of the approved targets for {capability_id}")
    approved = list(read.fields) or list(cap.fixed_fields if cap else [])
    if fields and list(fields) != approved:
        raise DiscoveryBlocked(f"a sample read returns exactly the approved columns ({', '.join(approved)})")
    return read, cap, target, approved, list(filters or []), max_records


def sample_read_preview(company_id: str, capability_id: str, *, target: Optional[str] = None,
                        fields: Optional[list[str]] = None, filters: Optional[list[dict]] = None,
                        max_records: int = 1) -> dict:
    """Exactly what Run Approved Sample Read would send -- computed, never sent."""
    profile = profile_service.load(company_id)
    if profile is None:
        raise DiscoveryBlocked("save a discovery profile first")
    _, _, target, approved, filters, max_records = _bound_sample(profile, capability_id, target, fields, filters, max_records)
    grant = admin_grant(company_id, "preview", "sample_read_preview")
    _, cap, approved, max_records = validate(grant, capability_id, target, approved, filters, max_records, require_enabled=False)
    plan = capabilities.build_plan(cap, target, approved, filters, max_records, environment=profile["config"].environment)
    capabilities.assert_read_semantics(plan)
    return {"capability_id": capability_id, "target": target, "fields": approved, "filters": filters,
            "max_records": max_records, "method": plan.method, "path": plan.path,
            "url": profile["config"].ais_base_url + plan.path, "body": plan.body,
            "request_sha256": request_fingerprint(plan), "mode": profile["config"].connection_mode,
            "note": "Computed only; nothing was sent. The session token is added at send time and never shown."}


def sample_read(company_id: str, actor_user_id: str, capability_id: str, *, target: Optional[str] = None,
                fields: Optional[list[str]] = None, filters: Optional[list[dict]] = None, max_records: int = 1) -> dict:
    """Run ONE approved read on ONE explicitly selected approved target,
    bounded by the approved columns, filters and record limit, to confirm the
    capability works against this endpoint. Separate from Test Connection.
    Success makes the capability 'supported' for this profile revision. An
    approved read of the environment master (F00941) also establishes the
    session environment's path code from JDE itself."""
    profile = profile_service.load(company_id)
    if profile is None:
        raise DiscoveryBlocked("save a discovery profile first")
    h = profile_service.health(profile)
    if any(h[c].state != "ok" for c in ("reachability", "authentication")):
        raise DiscoveryBlocked("run Test Connection successfully for this profile revision first")
    _, cap, target, approved, filters, max_records = _bound_sample(profile, capability_id, target, fields, filters, max_records)
    items = {i["item"]: i for i in (h["environment"].facets or {}).get("items", [])}
    if (items.get("session role") or {}).get("status") != "verified":
        raise DiscoveryBlocked("the session role is not verified as the configured dedicated role (a *ALL or "
                               "mismatched role never reads); fix the JDE account and run Test Connection again")
    environment_master = capability_id == "table_browse" and target == PATH_CODE_TABLE
    if h["environment"].state != "ok" and not (
            environment_master and all(i["status"] in ("verified", "attested", "pending") or n == "session environment"
                                       for n, i in items.items())):
        raise DiscoveryBlocked("identity is not verified for this profile revision: " + (h["environment"].detail or "")
                               + (" -- only the approved environment-master (F00941) read may run to establish it"
                                  if environment_master or items.get("session environment", {}).get("status") == "mismatch"
                                  else ""))
    grant = admin_grant(company_id, actor_user_id, "sample_read")
    raw: list = []
    try:
        evidence = execute_read(grant, capability_id, target, approved, filters, max_records, require_enabled=False,
                                raw_out=raw)
    except (DiscoveryBlocked, DiscoveryFailed) as exc:
        profile_service.record_check(company_id, "approved_read", "failed", f"{capability_id}: {exc}",
                                     capability_id=capability_id)
        raise
    profile_service.record_check(company_id, "approved_read", "ok",
                                 f"{capability_id} on {target or 'environment'}: {evidence['record_count']} record(s)",
                                 capability_id=capability_id)
    if (capability_id == "table_browse" and target == PATH_CODE_TABLE
            and {PATH_CODE_ENV_FIELD, PATH_CODE_FIELD} <= set(approved)):
        _record_path_code(company_id, profile, raw, evidence["observation_id"])
    return evidence


def _record_path_code(company_id: str, profile: dict, rows: list[dict], observation_id: str) -> None:
    session_env = ((profile_service.health(profile)["environment"].facets or {}).get("session_context") or {}).get("environment")
    config: JdeProfileConfig = profile["config"]
    if not session_env:
        profile_service.record_check(company_id, "path_code", "unknown",
                                     "the session environment is not known yet; run Test Connection first")
        return
    match = [r for r in rows if str(r.get(PATH_CODE_ENV_FIELD, "")).strip() == session_env]
    if not match:
        profile_service.record_check(company_id, "path_code", "failed",
                                     f"F00941 returned no row for the session environment {session_env!r}",
                                     facets={"observation_id": observation_id})
        return
    path_code = str(match[0].get(PATH_CODE_FIELD, "")).strip()
    facets = {"environment": session_env, "path_code": path_code, "observation_id": observation_id,
              "source": "JDE environment master F00941 (approved read)"}
    if config.path_code and config.path_code != path_code:
        profile_service.record_check(company_id, "path_code", "failed",
                                     f"JDE reports path code {path_code!r} for {session_env!r}; the profile expects "
                                     f"{config.path_code!r}. Not corrected automatically", facets=facets)
    else:
        profile_service.record_check(company_id, "path_code", "ok",
                                     f"JDE reports path code {path_code!r} for environment {session_env!r} "
                                     f"({observation_id})", facets=facets)


def grant_for_story(story_id: str, company_id: str, *, agent_run_id: Optional[str], actor_user_id: Optional[str],
                    purpose: str = "architect") -> tuple[Optional[DiscoveryGrant], str]:
    """Company and domain from the backend's own records; the company's
    current profile if discovery is active. Returns (grant, reason) --
    grant is None when discovery cannot be offered, with the reason."""
    from ..services.registry import get_customer_link_service, get_domain_review_service

    linked = get_customer_link_service().customer_for(story_id)
    if linked != company_id:
        return None, "the story is not linked to this company"
    review = get_domain_review_service().get(story_id)
    domain_id = review.business_domain_id if review else None
    profile = profile_service.load(company_id)
    if profile is None:
        return None, "this company has no JDE discovery profile"
    if not profile_service.is_active(profile):
        return None, "discovery is not enabled for this company's current profile revision"
    window_end = datetime.fromisoformat(profile["config"].discovery_window.ends_at).timestamp()
    expires = min(window_end, time.time() + MAX_GRANT_SECONDS)
    return DiscoveryGrant(company_id=company_id, story_id=story_id, domain_id=domain_id,
                          profile_revision=profile["revision"], agent_run_id=agent_run_id,
                          actor_user_id=actor_user_id, expires_at=expires, purpose=purpose), ""


def refresh_grant(story_id: str, company_id: str, actor_user_id: str) -> tuple[Optional[DiscoveryGrant], str]:
    grant, reason = grant_for_story(story_id, company_id, agent_run_id=None, actor_user_id=actor_user_id,
                                    purpose="refresh")
    return grant, reason

