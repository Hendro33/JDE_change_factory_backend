"""
Per-customer settings for agent execution in the customer's DEV system,
entered in Administration > Systems & Connections > JDE and stored in the
database. Nothing here comes from the server's environment.

What is stored here (the rest comes from the customer's JD Edwards
connection -- the AIS address and certificate, environment and path code):

  * the JD Edwards web client address and the Web OMW address, and
    optionally the certificate their server presents;
  * the dedicated DEV write user and its role. It is kept apart from the
    read-only discovery user; its password is encrypted with the server's
    credential key, bound to the AIS address and certificate it was entered
    for, and never returned;
  * the on/off switches: agent execution for the whole customer, and per
    capability. Both are on by default where a route exists; switching
    either is an Admin action recorded with name and date.

The same patterns as the connection profile: every save is a new revision
(409 stale / 428 missing expected revision), saving never contacts JDE, and
a material change -- an address, the role, the write user or its password,
or the AIS address it is sent to -- marks the last test stale, so agents
stop executing until an Admin tests the settings again.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import Field, field_validator

from ..models.base import ApiModel
from ..persistence.db import connection
from ..persistence.revisions import next_revision
from ..services import credential_crypto

CHECKS = ("ais_write_sign_in", "web_client")


class SettingsNotFound(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _https(v: str, *, required: bool) -> str:
    v = (v or "").strip()
    if not v:
        if required:
            raise ValueError("required")
        return ""
    parsed = urlparse(v)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("must be an https:// address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("must not carry credentials, a query or a fragment")
    try:
        parsed.port  # noqa: B018 -- raises on an invalid port
    except ValueError as exc:
        raise ValueError("the port must be a number from 1 to 65535") from exc
    return v.rstrip("/")


class ExecutionConfig(ApiModel):
    """Everything about agent execution except the secret."""

    # The JD Edwards web client (HTML server), e.g. https://jde.example.com/jde
    web_client_url: str = ""
    # Web OMW, when it is served from another address than the web client.
    web_omw_url: str = ""
    # The certificate (or its CA) the web client's server presents, uploaded
    # like the AIS certificate; blank = the AIS certificate or public CAs.
    web_ca_certificate_sha256: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")
    # The dedicated write user's role in DEV (never *ALL).
    write_role: str = ""
    notes: str = Field(default="", max_length=1000)

    @field_validator("web_client_url", "web_omw_url")
    @classmethod
    def _address(cls, v: str) -> str:
        return _https(v, required=False)

    @field_validator("write_role")
    @classmethod
    def _role(cls, v: str) -> str:
        v = (v or "").strip()
        if v.upper() in {"*ALL", "ALL", "*"}:
            raise ValueError("*ALL is not a dedicated role; the write user needs its own DEV role")
        return v


class ExecutionSettingsUpdate(ExecutionConfig):
    expected_revision: Optional[int] = None


class WriteCredentialUpdate(ApiModel):
    username: str
    password: str
    expected_revision: Optional[int] = None


class SwitchUpdate(ApiModel):
    """capability_id blank = the customer-wide switch."""

    capability_id: str = ""
    enabled: bool
    reason: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------
def _row(conn, company_id: str):
    return conn.execute("SELECT * FROM jde_execution_settings WHERE company_id = ?", (company_id,)).fetchone()


def load(company_id: str) -> Optional[dict[str, Any]]:
    with connection() as conn:
        row = _row(conn, company_id)
    if row is None:
        return None
    d = dict(row)
    d["config"] = ExecutionConfig.model_validate(json.loads(d["config"]))
    d["switches"] = json.loads(d["switches"] or "{}")
    d["health"] = json.loads(d["health"] or "{}")
    return d


def _profile_destination(company_id: str) -> str:
    """Where the write user's password is sent: the AIS address and
    certificate of the customer's JD Edwards connection."""
    from ..discovery import profile_service

    profile = profile_service.load(company_id)
    return profile_service.destination(profile["config"]) if profile else ""


def _profile_material(company_id: str) -> dict:
    from ..discovery import profile_service

    profile = profile_service.load(company_id)
    if profile is None:
        return {}
    c = profile["config"]
    return {"ais": profile_service.destination(c), "environment": c.environment, "path_code": c.path_code}


def material_hash(profile_material: dict, config: ExecutionConfig, credential_revision: int) -> str:
    material = {k: v for k, v in config.model_dump(mode="json").items() if k != "notes"}
    material["credential_revision"] = credential_revision
    material["profile"] = profile_material
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def audit(company_id: str, action: str, detail: str, actor: str) -> None:
    with connection(immediate=True) as conn:
        conn.execute("INSERT INTO jde_execution_audit (company_id, action, detail, actor, at) VALUES (?, ?, ?, ?, ?)",
                      (company_id, action, detail[:1000], actor, _now()))


def audit_log(company_id: str, limit: int = 100) -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT action, detail, actor, at FROM jde_execution_audit WHERE company_id = ? "
                            "ORDER BY id DESC LIMIT ?", (company_id, limit)).fetchall()
    return [dict(r) for r in rows]


def _describe_config_change(before: Optional[ExecutionConfig], after: ExecutionConfig) -> str:
    fields = ("web_client_url", "web_omw_url", "web_ca_certificate_sha256", "write_role", "notes")
    if before is None:
        return "settings saved: " + ", ".join(f"{f}={getattr(after, f) or '(blank)'}" for f in fields if f != "notes")
    changed = [f"{f}: {getattr(before, f) or '(blank)'} -> {getattr(after, f) or '(blank)'}"
               for f in fields if getattr(before, f) != getattr(after, f)]
    return "settings saved: " + ("; ".join(changed) if changed else "no change")


def save(company_id: str, config: ExecutionConfig, *, expected_revision: Optional[int], actor: str) -> dict:
    now = _now()
    profile = _profile_material(company_id)
    with connection(immediate=True) as conn:
        row = _row(conn, company_id)
        revision = next_revision(row["revision"] if row else None, expected_revision)
        cred_rev = row["credential_revision"] if row else 0
        config_json = json.dumps(config.model_dump(mode="json"), sort_keys=True)
        before = ExecutionConfig.model_validate(json.loads(row["config"])) if row else None
        new_hash = material_hash(profile, config, cred_rev)
        if row is None:
            conn.execute("INSERT INTO jde_execution_settings (company_id, revision, config, material_hash, updated_at, "
                         "updated_by) VALUES (?, ?, ?, ?, ?, ?)", (company_id, revision, config_json, new_hash, now, actor))
        else:
            conn.execute("UPDATE jde_execution_settings SET revision = ?, config = ?, material_hash = ?, updated_at = ?, "
                         "updated_by = ? WHERE company_id = ?", (revision, config_json, new_hash, now, actor, company_id))
    audit(company_id, "settings_saved", _describe_config_change(before, config), actor)
    return load(company_id)  # type: ignore[return-value]


def save_credential(company_id: str, username: str, password: str, *, expected_revision: Optional[int],
                    actor: str) -> dict:
    if not username.strip() or not password:
        raise ValueError("username and password are both required")
    destination = _profile_destination(company_id)
    profile = _profile_material(company_id)
    if not destination:
        raise SettingsNotFound("save the JD Edwards connection (AIS address) before the write user")
    secret = credential_crypto.encrypt(password)  # refuses without a key
    now = _now()
    with connection(immediate=True) as conn:
        row = _row(conn, company_id)
        if row is None:
            raise SettingsNotFound("save the agent execution settings before the write user")
        revision = next_revision(row["revision"], expected_revision)
        cred_rev = row["credential_revision"] + 1
        config = ExecutionConfig.model_validate(json.loads(row["config"]))
        conn.execute(
            "UPDATE jde_execution_settings SET revision = ?, material_hash = ?, credential_username = ?, "
            "credential_secret = ?, credential_revision = ?, credential_destination = ?, credential_updated_at = ?, "
            "credential_updated_by = ?, updated_at = ?, updated_by = ? WHERE company_id = ?",
            (revision, material_hash(profile, config, cred_rev), username.strip(), secret, cred_rev, destination,
             now, actor, now, actor, company_id))
    audit(company_id, "write_user_saved", f"DEV write user {username.strip()} saved (password encrypted)", actor)
    return load(company_id)  # type: ignore[return-value]


def write_credential(company_id: str) -> tuple[str, str]:
    """(username, password) of the DEV write user, for the executors only.
    Never returned by an endpoint, never logged."""
    s = load(company_id)
    if not s or not s.get("credential_secret"):
        raise credential_crypto.CredentialUnreadable("no DEV write user is saved for this customer")
    if s.get("credential_destination") != _profile_destination(company_id):
        raise credential_crypto.CredentialUnreadable(
            "the write user's password was entered for a different AIS address or certificate; re-enter it")
    return s["credential_username"], credential_crypto.decrypt(s["credential_secret"])


def credential_storage(s: Optional[dict]) -> str:
    if not s or not s.get("credential_secret"):
        return "none"
    try:
        credential_crypto.decrypt(s["credential_secret"])
    except credential_crypto.CredentialUnreadable:
        return "unreadable"
    if s.get("credential_destination") != _profile_destination(s["company_id"]):
        return "entered for a different AIS address or certificate -- re-enter it"
    return "encrypted"


def current_hash(s: dict) -> str:
    """The material hash as it is NOW: a change to the connection profile
    (another AIS address, environment or path code) also makes it differ."""
    return material_hash(_profile_material(s["company_id"]), s["config"], s["credential_revision"])


def record_check(company_id: str, check: str, state: str, detail: str, *, tested_hash: str) -> None:
    """tested_hash: the material hash the test ran against (current_hash
    before it started), so a save during the test leaves the result stale."""
    with connection(immediate=True) as conn:
        row = _row(conn, company_id)
        if row is None:
            return
        health = json.loads(row["health"] or "{}")
        health[check] = {"state": state, "detail": detail[:600], "checked_at": _now(), "revision": row["revision"],
                         "material_hash": tested_hash}
        conn.execute("UPDATE jde_execution_settings SET health = ? WHERE company_id = ?",
                     (json.dumps(health), company_id))


def check_state(s: dict, check: str) -> dict:
    entry = (s.get("health") or {}).get(check)
    if not entry:
        return {"state": "unknown", "detail": "not tested yet", "checked_at": None}
    state = entry["state"]
    if entry.get("material_hash") != current_hash(s):
        state = "stale"
    return {"state": state, "detail": entry.get("detail", ""), "checked_at": entry.get("checked_at")}


# ---------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------
def set_switch(company_id: str, update: SwitchUpdate, *, actor: str) -> dict:
    from . import routes

    cap = update.capability_id.strip()
    if cap and cap not in routes.switchable_capabilities():
        raise ValueError(f"{cap!r} is not a capability agents can execute")
    now = _now()
    with connection(immediate=True) as conn:
        row = _row(conn, company_id)
        if row is None:
            raise SettingsNotFound("save the agent execution settings first")
        if not cap:
            conn.execute("UPDATE jde_execution_settings SET agent_execution_enabled = ? WHERE company_id = ?",
                         (1 if update.enabled else 0, company_id))
        else:
            switches = json.loads(row["switches"] or "{}")
            switches[cap] = {"enabled": update.enabled, "by": actor, "at": now, "reason": update.reason}
            conn.execute("UPDATE jde_execution_settings SET switches = ? WHERE company_id = ?",
                         (json.dumps(switches), company_id))
    what = f"agent execution {'switched on' if update.enabled else 'switched off'} " + (
        f"for {cap}" if cap else "for this customer")
    audit(company_id, "switch", what + (f" ({update.reason})" if update.reason else ""), actor)
    return load(company_id)  # type: ignore[return-value]


def capability_enabled(s: Optional[dict], capability_id: str) -> tuple[bool, str]:
    """Is agent execution switched on for this capability (on by default)?"""
    if s is None:
        return True, "on (default)"
    if not s["agent_execution_enabled"]:
        return False, "agent execution is switched off for this customer by an Admin"
    sw = (s.get("switches") or {}).get(capability_id)
    if sw and not sw.get("enabled"):
        return False, f"agent execution for {capability_id} was switched off by {sw.get('by')} on {sw.get('at', '')[:10]}"
    if sw:
        return True, f"on (switched on by {sw.get('by')} on {sw.get('at', '')[:10]})"
    return True, "on (default)"


def web_hosts(s: Optional[dict]) -> set[str]:
    """The only hosts the browser executor may open for this customer."""
    if not s:
        return set()
    c: ExecutionConfig = s["config"]
    return {(urlparse(u).hostname or "").lower() for u in (c.web_client_url, c.web_omw_url) if u}
