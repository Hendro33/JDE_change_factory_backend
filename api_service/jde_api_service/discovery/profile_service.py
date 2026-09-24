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
from .transport import SIMULATION_LABEL, allowed_hosts, live_allowed_by_deployment

HEALTH_CHECKS = ("reachability", "authentication", "environment", "approved_read")
_NON_MATERIAL = {"customer_contact", "cnc_contact"}


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
            "enabled_material_hash = NULL, updated_at = ?, updated_by = ? WHERE company_id = ?",
            (revision, new_hash, username.strip(), secret, cred_rev, now, actor, now, actor, company_id),
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
    return profile["credential_username"], credential_crypto.decrypt(profile["credential_secret"])


def credential_storage(profile: dict) -> str:
    secret = profile.get("credential_secret")
    if not secret:
        return "none"
    try:
        credential_crypto.decrypt(secret)
    except credential_crypto.CredentialUnreadable:
        return "unreadable"
    return "encrypted"


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
    if config.environment_type != "DEV":
        out.append("only a DEV environment can be used for discovery")
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
        if not live_allowed_by_deployment():
            out.append("live discovery is switched off for this deployment")
        else:
            from urllib.parse import urlparse

            if (urlparse(config.ais_base_url).hostname or "").lower() not in allowed_hosts():
                out.append("the AIS host is not in this deployment's destination allowlist")
    if credential_storage(profile) != "encrypted":
        out.append("no readable discovery credential")
    h = health(profile)
    for name in HEALTH_CHECKS:
        if h[name].state != "ok":
            out.append(f"{name.replace('_', ' ')} check is {h[name].state} for this revision")
    if profile["disabled"]:
        out.append("the connection is disabled; re-test it before enabling")
    return out


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
                              live_allowed_by_deployment=live_allowed_by_deployment())
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
        live_allowed_by_deployment=live_allowed_by_deployment(),
        updated_at=profile["updated_at"], updated_by=profile["updated_by"],
    )
