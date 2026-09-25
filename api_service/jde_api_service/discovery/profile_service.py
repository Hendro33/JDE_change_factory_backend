"""
Company-specific JDE discovery profile: versioned metadata in SQLite, the
credential encrypted server-side, and the verification/enable state.

Rules this module enforces:

  * Saving never contacts JDE.
  * Every save is a new revision (409 stale, 428 missing expected revision),
    kept in jde_profile_revisions.
  * A MATERIAL change -- anything but the contact names, or a new credential
    -- marks every earlier check stale and switches discovery off until the
    connection is re-tested, an approved sample read succeeds and an Admin
    enables discovery again.
  * The secret is never returned, logged or put in an error message.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from ..persistence.db import connection
from ..persistence.revisions import next_revision
from ..services import credential_crypto
from . import capabilities
from .models import CapabilityView, CheckResult, JdeProfileConfig, JdeProfileView
from .transport import (SIMULATION_LABEL, Trust, allowed_hosts, live_allowed_by_deployment, live_status_detail,
                        tls_trust)

HEALTH_CHECKS = ("reachability", "authentication", "environment", "approved_read")
_NON_MATERIAL = {"customer_contact", "cnc_contact", "connection_name"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def material_hash(config: JdeProfileConfig, credential_revision: int) -> str:
    material = {k: v for k, v in config.model_dump(mode="json").items() if k not in _NON_MATERIAL}
    material["credential_revision"] = credential_revision
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


class ProfileNotFound(RuntimeError):
    pass


def _row(conn, company_id: str):
    return conn.execute("SELECT * FROM jde_profiles WHERE company_id = ?", (company_id,)).fetchone()


def load(company_id: str) -> Optional[dict[str, Any]]:
    """The raw current profile, with config parsed. Internal use only."""
    with connection() as conn:
        row = _row(conn, company_id)
    if row is None:
        return None
    d = dict(row)
    d["config"] = JdeProfileConfig.model_validate(json.loads(d["config"]))
    d["health"] = json.loads(d["health"] or "{}")
    d["capability_checks"] = json.loads(d["capability_checks"] or "{}")
    return d


def save(company_id: str, config: JdeProfileConfig, *, expected_revision: Optional[int], actor: str) -> dict:
    """Store a new revision. Never contacts JDE."""
    now = _now()
    with connection(immediate=True) as conn:
        row = _row(conn, company_id)
        revision = next_revision(row["revision"] if row else None, expected_revision)
        cred_rev = row["credential_revision"] if row else 0
        new_hash = material_hash(config, cred_rev)
        config_json = json.dumps(config.model_dump(mode="json"), sort_keys=True)
        if row is None:
            conn.execute(
                "INSERT INTO jde_profiles (company_id, revision, config, material_hash, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?)", (company_id, revision, config_json, new_hash, now, actor),
            )
        else:
            changed = new_hash != row["material_hash"]
            conn.execute(
                "UPDATE jde_profiles SET revision = ?, config = ?, material_hash = ?, updated_at = ?, updated_by = ?"
                + (", discovery_enabled = 0, enabled_material_hash = NULL" if changed else "")
                + " WHERE company_id = ?",
                (revision, config_json, new_hash, now, actor, company_id),
            )
        conn.execute(
            "INSERT INTO jde_profile_revisions (company_id, revision, config, material_hash, credential_revision, "
            "saved_at, saved_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (company_id, revision, config_json, new_hash, cred_rev, now, actor),
        )
    return load(company_id)  # type: ignore[return-value]


def save_credential(company_id: str, username: str, password: str, *, expected_revision: Optional[int],
                    actor: str) -> dict:
    """A new credential is a material change: the profile gets a new
    revision and must be re-verified."""
    if not username.strip() or not password:
        raise ValueError("username and password are both required")
    secret = credential_crypto.encrypt(password)  # refuses without a key
    now = _now()
    with connection(immediate=True) as conn:
        row = _row(conn, company_id)
        if row is None:
            raise ProfileNotFound("save the connection profile before its credential")
        revision = next_revision(row["revision"], expected_revision)
        cred_rev = row["credential_revision"] + 1
        config = JdeProfileConfig.model_validate(json.loads(row["config"]))
        new_hash = material_hash(config, cred_rev)
        conn.execute(
            "UPDATE jde_profiles SET revision = ?, material_hash = ?, credential_username = ?, credential_secret = ?, "
            "credential_revision = ?, credential_updated_at = ?, credential_updated_by = ?, discovery_enabled = 0, "
            "enabled_material_hash = NULL, updated_at = ?, updated_by = ?, credential_destination = ? WHERE company_id = ?",
            (revision, new_hash, username.strip(), secret, cred_rev, now, actor, now, actor, destination(config),
             company_id),
        )
        conn.execute(
            "INSERT INTO jde_profile_revisions (company_id, revision, config, material_hash, credential_revision, "
            "saved_at, saved_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (company_id, revision, row["config"], new_hash, cred_rev, now, actor),
        )
    return load(company_id)  # type: ignore[return-value]


def credential(company_id: str) -> tuple[str, str]:
    """(username, password) for the discovery transport only. Raises if
    absent or unreadable. Never returned by any endpoint."""
    profile = load(company_id)
    if not profile or not profile.get("credential_secret"):
        raise credential_crypto.CredentialUnreadable("no discovery credential is saved for this company")
    if not credential_bound(profile):
        raise credential_crypto.CredentialUnreadable(
            "the saved password was entered for a different AIS address or certificate; re-enter it for this one")
    return profile["credential_username"], credential_crypto.decrypt(profile["credential_secret"])


def credential_storage(profile: dict) -> str:
    secret = profile.get("credential_secret")
    if not secret:
        return "none"
    try:
        credential_crypto.decrypt(secret)
    except credential_crypto.CredentialUnreadable:
        return "unreadable"
    if not credential_bound(profile):
        return "entered for a different address or certificate -- re-enter it"
    return "encrypted"


def trust_for(company_id: str, config: JdeProfileConfig) -> Trust:
    """This company's connection trust, from its own saved settings: the
    host of the AIS address and the uploaded certificate (if any)."""
    from urllib.parse import urlparse

    from . import certificates

    host = (urlparse(config.ais_base_url).hostname or "").lower()
    if not config.ca_certificate_sha256:
        return Trust(host=host)
    cert = certificates.get(company_id, config.ca_certificate_sha256)
    if cert is None:
        return Trust(host=host, ca_sha256=config.ca_certificate_sha256, ca_missing=True)
    return Trust(host=host, ca_pem=cert["pem"], ca_sha256=cert["sha256"])


def destination(config: JdeProfileConfig) -> str:
    """Where a password is sent: scheme, host, port and the trusted certificate."""
    from urllib.parse import urlparse

    u = urlparse(config.ais_base_url)
    port = u.port or (443 if u.scheme == "https" else 80)
    return f"{u.scheme}://{(u.hostname or '').lower()}:{port}#ca={config.ca_certificate_sha256 or 'default'}"


def credential_bound(profile: dict) -> bool:
    """A saved password only goes to the address and certificate it was
    entered for. Simulation never sends it anywhere."""
    config: JdeProfileConfig = profile["config"]
    if config.connection_mode != "live" or not profile.get("credential_secret"):
        return True
    return profile.get("credential_destination") == destination(config)


def record_check(company_id: str, check: str, state: str, detail: str, *, capability_id: Optional[str] = None,
                 facets: Optional[dict] = None) -> None:
    with connection(immediate=True) as conn:
        row = _row(conn, company_id)
        if row is None:
            return
        entry = {"state": state, "checked_at": _now(), "detail": detail[:600],
                 "profile_revision": row["revision"], "material_hash": row["material_hash"], "facets": facets or {}}
        if capability_id:
            checks = json.loads(row["capability_checks"] or "{}")
            checks[capability_id] = entry
            conn.execute("UPDATE jde_profiles SET capability_checks = ? WHERE company_id = ?",
                         (json.dumps(checks), company_id))
        health = json.loads(row["health"] or "{}")
        health[check] = entry
        conn.execute("UPDATE jde_profiles SET health = ? WHERE company_id = ?", (json.dumps(health), company_id))


def _current(entry: Optional[dict], profile: dict) -> CheckResult:
    if not entry:
        return CheckResult()
    state = entry["state"]
    if entry.get("material_hash") != profile["material_hash"]:
        state = "stale"
    return CheckResult(state=state, checked_at=entry.get("checked_at"), detail=entry.get("detail", ""),
                       profile_revision=entry.get("profile_revision"), facets=entry.get("facets") or {})


def health(profile: dict) -> dict[str, CheckResult]:
    return {name: _current(profile["health"].get(name), profile) for name in HEALTH_CHECKS}


def capability_status(profile: Optional[dict], capability_id: str) -> tuple[str, str]:
    cap = capabilities.get(capability_id)
    if cap is None:
        return "unavailable", "unknown capability"
    if cap.base_status == "unavailable":
        return "unavailable", cap.unavailable_reason
    if profile is None:
        return "unverified", "no discovery profile"
    check = _current(profile["capability_checks"].get(capability_id), profile)
    if check.state == "ok":
        suffix = " (simulation)" if profile["config"].connection_mode == "simulation" else ""
        return "supported", f"approved sample read succeeded {check.checked_at}{suffix}"
    if check.state == "stale":
        return "unverified", "verified for an earlier profile revision; run the approved sample read again"
    if check.state == "failed":
        return "unverified", f"last approved sample read failed: {check.detail}"
    return "unverified", "not yet confirmed against this AIS: run an approved sample read"


def capability_views(profile: Optional[dict]) -> list[CapabilityView]:
    approved = {r.capability_id: r for r in (profile["config"].approved_reads if profile else [])}
    views = []
    for cap in capabilities.CAPABILITIES.values():
        status, detail = capability_status(profile, cap.capability_id)
        read = approved.get(cap.capability_id)
        views.append(CapabilityView(
            capability_id=cap.capability_id, title=cap.title, description=cap.description, status=status,
            status_detail=detail, data_class=cap.data_class, target_kind=cap.target_kind, approved=read is not None,
            approved_targets=read.targets if read else [], approved_fields=read.fields if read else [],
            approved_filter_fields=read.filter_fields if read else [], alternative=cap.alternative,
        ))
    return views


def window_open(config: JdeProfileConfig, now: Optional[datetime] = None) -> bool:
    if config.discovery_window is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (datetime.fromisoformat(config.discovery_window.starts_at) <= now
            < datetime.fromisoformat(config.discovery_window.ends_at))


def enable_blockers(profile: dict) -> list[str]:
    """Everything that stops discovery being enabled now, in plain words."""
    config: JdeProfileConfig = profile["config"]
    out = []
    if not config.routing_isolation_confirmed or not config.isolation_evidence.strip():
        out.append("the customer/CNC has not confirmed the network route and isolation (with evidence)")
    if not config.privilege_confirmed or not config.privilege_statement.strip():
        out.append("the customer has not confirmed the JDE identity is narrowly privileged (read-only role)")
    if not config.runtime_attestation_confirmed or not config.runtime_attestation_evidence.strip():
        out.append("the CNC has not attested the Tools release and path code the environment runs on "
                   "(the AIS contract does not report them)")
    if not config.approved_reads:
        out.append("no approved read operations")
    if config.discovery_window is None:
        out.append("no discovery window")
    elif not window_open(config):
        out.append("the discovery window is not open now")
    if config.connection_mode == "live":
        trust = trust_for(profile["company_id"], config)
        if not live_allowed_by_deployment(trust):
            out.append(live_status_detail(trust))
        elif trust.host not in allowed_hosts(trust):
            out.append("the AIS host is not a permitted destination (narrowed by the server operator)")
    if credential_storage(profile) != "encrypted":
        out.append("no readable discovery credential")
    h = health(profile)
    for name in HEALTH_CHECKS:
        if h[name].state != "ok":
            out.append(f"{name.replace('_', ' ')} check is {h[name].state} for this revision")
    if profile["disabled"]:
        out.append("the connection is disabled; re-test it before enabling")
    if config.connection_mode == "live":
        groups, _ = readiness(profile)
        for g in groups:
            for i in g["items"]:
                if i["required"] and not i["satisfied"]:
                    text = f"{g['label']}: {i['label']} -- {i['detail']}"
                    if text not in out:
                        out.append(text)
    return out


def _evidence_titles(company_id: str, ids: list[str]) -> list[str]:
    from . import artifacts

    out = []
    for ref in ids:
        a = artifacts.get(company_id, ref.partition("@r")[0])
        out.append(f"{ref}: {a['meta'].get('title') or a['meta'].get('file_name') or a['kind']}" if a else f"{ref}: NOT FOUND")
    return out


def prerequisites(profile: dict) -> list[dict]:
    """Every prerequisite for Architect discovery, saying what kind of
    assurance it is: customer attestation (Jade cannot check it), machine
    verified (Jade checked it for this revision), configuration, or
    server-managed (deployment trust controls, not editable in Admin)."""
    config: JdeProfileConfig = profile["config"]
    h = health(profile)
    live = config.connection_mode == "live"

    def item(pid, label, kind, ok, detail, required=True):
        return {"id": pid, "label": label, "kind": kind, "satisfied": bool(ok), "detail": detail, "required": required}

    evidence = _evidence_titles(profile["company_id"], config.evidence_artifact_ids)
    out = [
        item("purpose", "Environment purpose stated", "customer_attestation", True,
             "development environment" if config.environment_purpose == "development" else
             f"isolated trial environment -- approval: {config.trial_approval_reference}"),
        item("routing_isolation", "Network route and data isolation confirmed by the customer/CNC", "customer_attestation",
             config.routing_isolation_confirmed and config.isolation_evidence.strip(),
             config.isolation_evidence.strip() or "not confirmed"),
        item("privilege", "JDE identity is narrowly privileged (read-only role)", "customer_attestation",
             config.privilege_confirmed and config.privilege_statement.strip(), config.privilege_statement.strip() or "not confirmed"),
        item("runtime", "Tools release and path code attested by the CNC", "customer_attestation",
             config.runtime_attestation_confirmed and config.runtime_attestation_evidence.strip(),
             config.runtime_attestation_evidence.strip() or "not attested (AIS does not report them)"),
        item("evidence", "Evidence documents linked", "customer_attestation", bool(evidence) and not any("NOT FOUND" in e for e in evidence),
             "; ".join(evidence) or "none linked (optional, recommended)", required=False),
        item("approved_reads", "Approved read operations defined", "configuration", bool(config.approved_reads),
             f"{len(config.approved_reads)} capability(ies)"),
        item("window", "Authorisation window open now", "configuration", window_open(config),
             (f"{config.discovery_window.starts_at} to {config.discovery_window.ends_at}" if config.discovery_window
              else "no window")),
        item("credential", "Credential saved and readable on the server", "configuration",
             credential_storage(profile) == "encrypted", credential_storage(profile)),
    ]
    for name, label in (("reachability", "Endpoint reachable from the backend"),
                        ("authentication", "Authentication accepted (session opened and logged out)"),
                        ("environment", "Session environment, role and release match the profile"),
                        ("approved_read", "Approved sample read succeeded")):
        c = h[name]
        out.append(item(name, label, "machine_verified", c.state == "ok",
                        f"{c.state} for this revision" + (f" -- {c.detail}" if c.detail else "")))
    if live:
        for s_ in server_prerequisites(config):
            out.append({**s_, "kind": "server_managed", "required": True})
    out.append(item("not_disabled", "Connection not disabled", "configuration", not profile["disabled"],
                    "disabled -- run Test Connection to re-check" if profile["disabled"] else "active"))
    return out


def server_prerequisites(config: JdeProfileConfig, company_id: str = "") -> list[dict]:
    """What must hold for a live connection: live access not locked by the
    server operator, the saved address as the permitted destination, usable
    TLS trust (an uploaded certificate or public CAs), and the server's
    credential-encryption key. All but the key come from the settings."""
    from ..services import credential_crypto

    trust = trust_for(company_id, config)
    trust_ok, trust_detail = tls_trust(trust)
    permitted = trust.host in allowed_hosts(trust)
    return [
        {"id": "live_enabled", "label": "Live access available", "satisfied": live_allowed_by_deployment(trust),
         "detail": live_status_detail(trust)},
        {"id": "allowlist", "label": f"AIS host {trust.host} is the permitted destination", "satisfied": permitted,
         "detail": "the saved AIS address" if permitted else "the server operator limits destinations to other hosts"},
        {"id": "tls_trust", "label": "TLS certificate trust (never unverified)", "satisfied": trust_ok,
         "detail": trust_detail},
        {"id": "encryption_key", "label": "Credential-encryption key configured on the server",
         "satisfied": credential_crypto.is_configured(),
         "detail": "configured" if credential_crypto.is_configured() else "JDE_CREDENTIAL_KEY is not set on the server"},
    ]


def _item(pid, label, kind, ok, detail, required=True):
    return {"id": pid, "label": label, "kind": kind, "satisfied": bool(ok), "detail": detail, "required": required}


def _identity_items(profile: dict) -> dict:
    return {i["item"]: i for i in ((health(profile)["environment"].facets or {}).get("items") or [])}


def _account_problems(profile: dict) -> list[str]:
    """Why the dedicated-account verification does not (yet) count."""
    from . import artifacts

    config: JdeProfileConfig = profile["config"]
    acc = config.dedicated_account
    problems = []
    user = (profile.get("credential_username") or "").strip()
    if not acc.username or acc.username.strip().upper() != user.upper():
        problems.append(f"the verified user ({acc.username or 'none'}) is not the user Jade signs in with ({user or 'none'})")
    if not acc.role or acc.role != config.role:
        problems.append(f"the verified role ({acc.role or 'none'}) is not the configured role ({config.role})")
    if not acc.verified_by.strip() or not acc.verified_on.strip() or not acc.method:
        problems.append("who verified it, when and how is not recorded")
    if not acc.permits_approved_reads or not acc.rejects_prohibited_operations:
        problems.append("the verification does not confirm both that the approved reads are permitted and that "
                        "prohibited operations are rejected by JDE")
    docs = [a for a in acc.evidence_artifact_ids if artifacts.get(profile["company_id"], a.partition("@r")[0])]
    if not docs:
        problems.append("no evidence document is linked (a statement or a read-only label alone is not proof)")
    return problems


def readiness(profile: dict) -> tuple[list[dict], bool]:
    """Separately visible readiness groups. For a LIVE connection every
    required item must be satisfied before discovery can be enabled; in
    simulation the live-only items are marked not applicable."""
    from jde_mcp_server.config import settings as mcp_settings

    config: JdeProfileConfig = profile["config"]
    live = config.connection_mode == "live"
    h = health(profile)
    ids = _identity_items(profile)

    def ident(name, label):
        i = ids.get(name)
        if not live and name == "path code":
            return _item("path_code", label, "machine_verified", True, "not applicable in simulation", required=False)
        if name == "path code":
            pc = _current(profile["health"].get("path_code"), profile)
            return _item("path_code", label, "machine_verified", pc.state == "ok",
                         pc.detail or "not established: needs the approved F00941 environment-master read")
        return _item(name.replace(" ", "_").replace("/", ""), label, "machine_verified",
                     bool(i) and i["status"] in (("verified",) if live else ("verified", "attested")),
                     (i["detail"] if i else "not checked yet: run Test Connection"))

    na = "not applicable in simulation"
    trust = trust_for(profile["company_id"], config)
    trust_ok, trust_detail = tls_trust(trust)
    reach = h["reachability"]
    connectivity = [
        _item("live_enabled", "Live access available (not locked by the server operator)", "configuration",
              live_allowed_by_deployment(trust) or not live, live_status_detail(trust) if live else na, required=live),
        _item("tls_trust", "TLS certificate trust (uploaded certificate or public CAs; never unverified)", "configuration",
              trust_ok or not live, trust_detail if live else na, required=live),
        _item("allowlist", "AIS host is the permitted destination", "configuration",
              (not live) or trust.host in allowed_hosts(trust),
              (f"{trust.host}: the saved AIS address" if trust.host in allowed_hosts(trust)
               else f"{trust.host} is outside the destinations the server operator allows") if live else na,
              required=live),
        _item("tls_verified", "Endpoint reached from the backend over verified TLS", "machine_verified",
              reach.state == "ok" and (not live or "verified TLS" in reach.detail), reach.detail or "not checked yet"),
    ]
    account_problems = _account_problems(profile) if live else []
    nr = config.network_restriction
    identity = [
        _item("authentication", "Signed in as the configured user (session opened and logged out)", "machine_verified",
              h["authentication"].state == "ok", h["authentication"].detail or "not checked yet"),
        ident("session environment", "Authenticated environment captured and equal to the configured environment"),
        ident("session role", "Authenticated role captured, dedicated (never *ALL)"),
        ident("application release", "Application release reported by the session"),
        ident("Tools / server release", "Tools / server release reported by JDE"),
        ident("path code", "Path code established from JDE (never from the environment name)"),
    ]
    authz = [
        _item("dedicated_account", "Dedicated user and role, JDE permissions independently verified", "evidence",
              (not live) or not account_problems, "; ".join(account_problems) if live else na, required=live),
        _item("approved_read", "The approved sample read succeeds within its bounds", "machine_verified",
              h["approved_read"].state == "ok", h["approved_read"].detail or "not run"),
        _item("privilege_statement", "Customer statement that the identity is narrowly privileged", "customer_attestation",
              config.privilege_confirmed and config.privilege_statement.strip(),
              config.privilege_statement.strip() or "not confirmed", required=not live),
    ]
    network = [
        _item("source_restriction", "AIS access restricted to the backend's source address", "customer_attestation",
              (not live) or (nr.restricted_to_source and nr.backend_source_address.strip()
                             and (nr.evidence.strip() or nr.evidence_artifact_ids)),
              (f"{nr.backend_source_address or 'no source address'}: {nr.evidence or 'no evidence'}" if live else na),
              required=live),
        _item("routing_isolation", "Network route and data isolation confirmed by the customer/CNC", "customer_attestation",
              config.routing_isolation_confirmed and config.isolation_evidence.strip(),
              config.isolation_evidence.strip() or "not confirmed"),
    ]
    safeguards = [
        _item("writes_disabled", "JDE writes disabled (execution stays simulated)", "server_managed",
              bool(mcp_settings.mock_mode), "simulated execution only" if mcp_settings.mock_mode
              else "JDE_MCP_MOCK_MODE is off: live execution is possible on this server", required=live),
        _item("approved_reads", "Approved reads defined (exact targets, columns, record limit)", "configuration",
              bool(config.approved_reads), f"{len(config.approved_reads)} approved read(s), at most "
                                           f"{config.limits.max_records} records each"),
        _item("window", "Authorisation window open now", "configuration", window_open(config),
              (f"{config.discovery_window.starts_at} to {config.discovery_window.ends_at}" if config.discovery_window
               else "no window")),
        _item("credential", "Credential encrypted on the server", "configuration",
              credential_storage(profile) == "encrypted", credential_storage(profile)),
        _item("not_disabled", "Connection not disabled", "configuration", not profile["disabled"],
              "disabled -- run Test Connection to re-check" if profile["disabled"] else "active"),
    ]
    groups = [
        {"id": "connectivity", "label": "Connectivity", "items": connectivity},
        {"id": "identity", "label": "Identity", "items": identity},
        {"id": "jde_authorization", "label": "JDE authorisation", "items": authz},
        {"id": "network_restriction", "label": "Network restriction", "items": network},
        {"id": "runtime_safeguards", "label": "Jade runtime safeguards", "items": safeguards},
    ]
    for g in groups:
        req = [i for i in g["items"] if i["required"]]
        g["satisfied"] = all(i["satisfied"] for i in req)
    return groups, all(g["satisfied"] for g in groups)


def _host(config: JdeProfileConfig) -> str:
    from urllib.parse import urlparse

    return (urlparse(config.ais_base_url).hostname or "").lower()


def set_enabled(company_id: str, *, actor: str, expected_revision: Optional[int]) -> dict:
    with connection(immediate=True) as conn:
        row = _row(conn, company_id)
        if row is None:
            raise ProfileNotFound("no discovery profile")
        if expected_revision is not None and expected_revision != row["revision"]:
            from ..persistence.revisions import RevisionConflict

            raise RevisionConflict(row["revision"])
    profile = load(company_id)
    blockers = enable_blockers(profile)  # type: ignore[arg-type]
    if blockers:
        raise ValueError("discovery cannot be enabled: " + "; ".join(blockers))
    with connection(immediate=True) as conn:
        conn.execute(
            "UPDATE jde_profiles SET discovery_enabled = 1, enabled_material_hash = material_hash, enabled_by = ?, "
            "enabled_at = ? WHERE company_id = ? AND revision = ?", (actor, _now(), company_id, profile["revision"]),
        )
    return load(company_id)  # type: ignore[return-value]


def set_disabled(company_id: str, *, actor: str) -> dict:
    """Kill switch: no new or queued call proceeds, and every check is
    cleared so re-enabling needs a fresh test."""
    with connection(immediate=True) as conn:
        if _row(conn, company_id) is None:
            raise ProfileNotFound("no discovery profile")
        conn.execute(
            "UPDATE jde_profiles SET disabled = 1, discovery_enabled = 0, enabled_material_hash = NULL, "
            "disabled_by = ?, disabled_at = ?, health = '{}', capability_checks = '{}' WHERE company_id = ?",
            (actor, _now(), company_id),
        )
    return load(company_id)  # type: ignore[return-value]


def clear_disabled(company_id: str) -> None:
    """A fresh Test Connection is the way back from Disable."""
    with connection() as conn:
        conn.execute("UPDATE jde_profiles SET disabled = 0 WHERE company_id = ?", (company_id,))


def is_active(profile: Optional[dict]) -> bool:
    return bool(profile and profile["discovery_enabled"] and not profile["disabled"]
                and profile["enabled_material_hash"] == profile["material_hash"])


def view(company_id: str) -> JdeProfileView:
    profile = load(company_id)
    if profile is None:
        return JdeProfileView(company_id=company_id, configured=False, capabilities=capability_views(None),
                              live_allowed_by_deployment=live_allowed_by_deployment(), **_static_view_parts(None))
    username = profile.get("credential_username") or ""
    masked = (username[:2] + "•" * max(3, len(username) - 2)) if username else None
    config: JdeProfileConfig = profile["config"]
    return JdeProfileView(
        company_id=company_id, configured=True, revision=profile["revision"], config=config,
        credential_configured=bool(profile.get("credential_secret")), credential_username_masked=masked,
        credential_storage=credential_storage(profile), health=health(profile),
        capabilities=capability_views(profile), discovery_enabled=is_active(profile),
        enabled_by=profile.get("enabled_by"), enabled_at=profile.get("enabled_at"),
        disabled=bool(profile["disabled"]), disabled_by=profile.get("disabled_by"),
        disabled_at=profile.get("disabled_at"), enable_blockers=enable_blockers(profile),
        mode_label=SIMULATION_LABEL if config.connection_mode == "simulation" else "LIVE customer AIS endpoint",
        live_allowed_by_deployment=live_allowed_by_deployment(trust_for(company_id, config)),
        updated_at=profile["updated_at"], updated_by=profile["updated_by"],
        prerequisites=prerequisites(profile), server_prerequisites=server_prerequisites(config, company_id),
        certificate=_certificate_view(company_id, config), credential_bound=credential_bound(profile),
        readiness=readiness(profile)[0], ready=readiness(profile)[1],
        **_static_view_parts(config),
    )


def _certificate_view(company_id: str, config: JdeProfileConfig) -> Optional[dict]:
    from urllib.parse import urlparse

    from . import certificates

    if not config.ca_certificate_sha256:
        return None
    summary = certificates.get_summary(company_id, config.ca_certificate_sha256)
    if summary is None:
        return {"sha256": config.ca_certificate_sha256, "missing": True}
    return {**summary, "coversHost": certificates.covers_host(summary, (urlparse(config.ais_base_url).hostname or ""))}


def _static_view_parts(config: Optional[JdeProfileConfig]) -> dict:
    from .capabilities import AUTH_ENDPOINTS, HARD_MAX_RECORDS, READ_ENDPOINTS
    from .models import AUTH_METHODS, MAX_TIMEOUT_SECONDS, MAX_WINDOW_DAYS

    urls = {}
    if config is not None:
        urls = {"token_request": config.ais_base_url + AUTH_ENDPOINTS["token_request"][1],
                "logout": config.ais_base_url + AUTH_ENDPOINTS["logout"][1],
                **{k: config.ais_base_url + p for k, (_, p) in READ_ENDPOINTS.items()}}
    return {"ceilings": {"max_records": HARD_MAX_RECORDS, "max_timeout_seconds": MAX_TIMEOUT_SECONDS,
                         "max_window_days": MAX_WINDOW_DAYS, "concurrent_requests": 1, "max_filters": 5},
            "auth_methods": AUTH_METHODS, "request_urls": urls}
