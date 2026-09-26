"""
A customer's company-owned AI connection (Anthropic API only, for now).

  * Configured by the customer's Admin in Jade; stored per company in SQLite.
  * The API key is encrypted with the server's credential key
    (services/credential_crypto.py, JDE_CREDENTIAL_KEY -- managed outside the
    database), write-only: never returned, logged or put in an error message.
    It can be replaced (new credential revision) or revoked.
  * Saving never contacts the provider. The connection test is a separate,
    explicit action that sends one minimal, billable request.
  * resolve_for_run() is the only way an agent run obtains a key. Anything
    missing or revoked raises AiNotConfigured: there is no fallback to the
    backend machine's own key, login or default model, nor to another
    company's connection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..persistence.db import connection
from ..persistence.revisions import next_revision
from ..services import credential_crypto

PROVIDER = "anthropic"
PROVIDER_LABEL = "Anthropic API (company API key)"
API_BASE_URL = "https://api.anthropic.com"
# Demonstrations and tests only: a fake provider on THIS machine's loopback
# interface. Anything else is refused, so keys can never be redirected
# elsewhere; runs against it are recorded as test-provider runs and never
# count as real evidence.
TEST_PROVIDER_ENV = "JADE_AI_TEST_PROVIDER_URL"
TEST_PROVIDER = "anthropic-test-provider"


def endpoint() -> tuple[str, bool]:
    """(base URL, is_test_provider)."""
    import os
    from urllib.parse import urlparse

    raw = os.environ.get(TEST_PROVIDER_ENV, "").strip()
    if not raw:
        return API_BASE_URL, False
    u = urlparse(raw)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost") or u.path not in ("", "/"):
        raise AiNotConfigured(f"{TEST_PROVIDER_ENV} may only point at http://127.0.0.1:<port>; nothing was sent")
    return raw.rstrip("/"), True

# Versioned rate card (USD per million tokens, Anthropic first-party list
# prices). Used only to label a run's cost as an ESTIMATE; the runtime's own
# reported cost is recorded separately when it provides one.
RATE_CARD_VERSION = "anthropic-list-2026-06-24"
MODELS: dict[str, dict[str, Any]] = {
    "claude-opus-5": {"label": "Claude Opus 5", "input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"label": "Claude Sonnet 5", "input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"label": "Claude Haiku 4.5", "input": 1.00, "output": 5.00},
    "claude-fable-5-1": {"label": "Claude Fable 5.1", "input": 10.00, "output": 50.00},
}
DOCUMENT_POLICIES = ("metadata_only", "permitted_content")
DEFAULT_LIMITS = {"max_usd_per_run": 2.0, "monthly_usd": 50.0, "max_turns": 40}
LIMIT_BOUNDS = {"max_usd_per_run": (0.01, 100.0), "monthly_usd": (0.0, 10000.0), "max_turns": (1, 100)}


class AiNotConfigured(RuntimeError):
    """Real agent execution is blocked: the reason is safe to show."""


class InvalidConfig(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(conn, company_id: str):
    return conn.execute("SELECT * FROM ai_connections WHERE company_id = ?", (company_id,)).fetchone()


def _audit(conn, company_id: str, action: str, actor: str, detail: str = "") -> None:
    conn.execute("INSERT INTO ai_connection_audit (company_id, action, detail, actor, at) VALUES (?, ?, ?, ?, ?)",
                 (company_id, action, detail, actor, _now()))


def _clean_limits(raw: Optional[dict]) -> dict:
    out = dict(DEFAULT_LIMITS)
    for k, v in (raw or {}).items():
        if k not in LIMIT_BOUNDS:
            raise InvalidConfig(f"unknown limit: {k}")
        lo, hi = LIMIT_BOUNDS[k]
        try:
            num = int(v) if k == "max_turns" else float(v)
        except (TypeError, ValueError):
            raise InvalidConfig(f"{k} must be a number") from None
        if not lo <= num <= hi:
            raise InvalidConfig(f"{k} must be between {lo} and {hi}")
        out[k] = num
    return out


def save(company_id: str, *, model: str, enabled: bool, document_policy: str, limits: Optional[dict],
         expected_revision: Optional[int], actor: str) -> dict:
    """Store the configuration (new revision). Never contacts the provider."""
    if model not in MODELS:
        raise InvalidConfig(f"unsupported model: {model}")
    if document_policy not in DOCUMENT_POLICIES:
        raise InvalidConfig(f"unsupported document policy: {document_policy}")
    clean = _clean_limits(limits)
    now = _now()
    with connection(immediate=True) as conn:
        row = _row(conn, company_id)
        revision = next_revision(row["revision"] if row else None, expected_revision)
        if row is None:
            conn.execute(
                "INSERT INTO ai_connections (company_id, provider, model, revision, enabled, document_policy, limits, "
                "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (company_id, PROVIDER, model, revision, int(enabled), document_policy, json.dumps(clean), now, actor))
        else:
            conn.execute(
                "UPDATE ai_connections SET model = ?, revision = ?, enabled = ?, document_policy = ?, limits = ?, "
                "updated_at = ?, updated_by = ? WHERE company_id = ?",
                (model, revision, int(enabled), document_policy, json.dumps(clean), now, actor, company_id))
        _audit(conn, company_id, "configuration_saved", actor,
               f"revision {revision}: model {model}, enabled {enabled}, documents {document_policy}, limits {clean}")
    return view(company_id)


def save_credential(company_id: str, api_key: str, *, actor: str) -> dict:
    api_key = (api_key or "").strip()
    if len(api_key) < 20 or any(c.isspace() for c in api_key):
        raise InvalidConfig("that does not look like an Anthropic API key")
    secret = credential_crypto.encrypt(api_key)  # refuses without the server key
    hint = f"…{api_key[-4:]}"
    now = _now()
    with connection(immediate=True) as conn:
        row = _row(conn, company_id)
        if row is None:
            raise InvalidConfig("save the AI connection settings before its API key")
        cred_rev = row["credential_revision"] + 1
        conn.execute(
            "UPDATE ai_connections SET credential_secret = ?, credential_hint = ?, credential_revision = ?, "
            "credential_updated_at = ?, credential_updated_by = ?, credential_revoked_at = NULL, "
            "credential_revoked_by = NULL WHERE company_id = ?", (secret, hint, cred_rev, now, actor, company_id))
        _audit(conn, company_id, "credential_replaced" if row["credential_secret"] else "credential_saved", actor,
               f"credential revision {cred_rev} ({hint})")
    return view(company_id)


def revoke_credential(company_id: str, *, actor: str) -> dict:
    now = _now()
    with connection(immediate=True) as conn:
        row = _row(conn, company_id)
        if row is None or not row["credential_secret"]:
            raise InvalidConfig("no API key is stored")
        conn.execute(
            "UPDATE ai_connections SET credential_secret = NULL, credential_revoked_at = ?, credential_revoked_by = ? "
            "WHERE company_id = ?", (now, actor, company_id))
        _audit(conn, company_id, "credential_revoked", actor, f"credential revision {row['credential_revision']}")
    return view(company_id)


def _credential_state(row) -> str:
    if row is None:
        return "not configured"
    if row["credential_secret"]:
        try:
            credential_crypto.decrypt(row["credential_secret"])
        except credential_crypto.CredentialUnreadable:
            return "unreadable (the server's encryption key changed)"
        return "stored (encrypted)"
    return "revoked" if row["credential_revoked_at"] else "not set"


def view(company_id: str) -> dict:
    """Everything the Admin screen shows -- never the key."""
    with connection() as conn:
        row = _row(conn, company_id)
        audit = conn.execute("SELECT action, detail, actor, at FROM ai_connection_audit WHERE company_id = ? "
                             "ORDER BY id DESC LIMIT 20", (company_id,)).fetchall()
    try:
        test_provider = endpoint()[1]
    except AiNotConfigured:
        test_provider = True
    base = {"provider": PROVIDER, "providerLabel": PROVIDER_LABEL, "testProvider": test_provider,
            "models": [{"id": k, **v} for k, v in MODELS.items()], "rateCardVersion": RATE_CARD_VERSION,
            "documentPolicies": list(DOCUMENT_POLICIES), "defaultLimits": DEFAULT_LIMITS,
            "audit": [dict(a) for a in audit]}
    if row is None:
        return {**base, "configured": False, "credentialState": "not configured", "tested": False}
    tested = (row["last_test_outcome"] == "ok" and row["last_test_connection_revision"] == row["revision"]
              and row["last_test_credential_revision"] == row["credential_revision"])
    return {**base, "configured": True, "model": row["model"], "revision": row["revision"],
            "enabled": bool(row["enabled"]), "documentPolicy": row["document_policy"],
            "limits": json.loads(row["limits"]), "credentialState": _credential_state(row),
            "credentialHint": row["credential_hint"] if row["credential_secret"] else None,
            "credentialRevision": row["credential_revision"], "credentialUpdatedAt": row["credential_updated_at"],
            "credentialUpdatedBy": row["credential_updated_by"], "credentialRevokedAt": row["credential_revoked_at"],
            "lastTest": {"at": row["last_test_at"], "outcome": row["last_test_outcome"],
                         "detail": row["last_test_detail"], "connectionRevision": row["last_test_connection_revision"],
                         "credentialRevision": row["last_test_credential_revision"]} if row["last_test_at"] else None,
            "tested": tested, "updatedAt": row["updated_at"], "updatedBy": row["updated_by"],
            "monthSpendUsd": month_spend(company_id)}


def month_spend(company_id: str) -> float:
    start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    with connection() as conn:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM ai_runs WHERE company_id = ? AND started_at >= ?",
                           (company_id, start)).fetchone()
    return round(float(row["s"] or 0), 4)


@dataclass(frozen=True)
class ResolvedConnection:
    company_id: str
    provider: str
    model: str
    revision: int
    credential_revision: int
    document_policy: str
    limits: dict
    api_key: str = field(repr=False)  # never printed


def resolve_for_run(company_id: Optional[str]) -> ResolvedConnection:
    """The company's own connection, or AiNotConfigured. No fallbacks."""
    if not company_id:
        raise AiNotConfigured("no customer is attached to this run; AI agents only run for a customer")
    with connection() as conn:
        row = _row(conn, company_id)
    if row is None:
        raise AiNotConfigured("this customer has no AI connection. An Admin sets it up under Admin > AI Connections")
    if not row["enabled"]:
        raise AiNotConfigured("this customer's AI connection is switched off (Admin > AI Connections)")
    if not row["credential_secret"]:
        state = "revoked" if row["credential_revoked_at"] else "not set"
        raise AiNotConfigured(f"this customer's Anthropic API key is {state}; an Admin enters it under Admin > AI Connections")
    try:
        key = credential_crypto.decrypt(row["credential_secret"])
    except credential_crypto.CredentialUnreadable:
        raise AiNotConfigured("the stored API key cannot be decrypted (the server's encryption key changed); "
                              "an Admin must enter it again") from None
    limits = json.loads(row["limits"])
    if limits.get("monthly_usd") is not None and month_spend(company_id) >= float(limits["monthly_usd"]):
        raise AiNotConfigured(f"this customer's monthly AI budget (USD {limits['monthly_usd']}) is used up; "
                              "an Admin can raise it under Admin > AI Connections")
    if row["model"] not in MODELS:
        raise AiNotConfigured(f"the configured model {row['model']} is not supported")
    _url, is_test = endpoint()
    return ResolvedConnection(company_id=company_id, provider=TEST_PROVIDER if is_test else row["provider"],
                              model=row["model"],
                              revision=row["revision"], credential_revision=row["credential_revision"],
                              document_policy=row["document_policy"], limits=limits, api_key=key)


# -- Explicit connection test ---------------------------------------------------
TEST_EXPLANATION = ("Sends one minimal request to the configured model (a one-word prompt, at most 1 output "
                    "token). It is billed to this API key: a fraction of a US cent.")


def _send_test_message(api_key: str, model: str) -> str:
    """One minimal Messages API request through the official SDK. Separate
    function so tests can replace it; nothing else calls the provider."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, base_url=endpoint()[0], max_retries=0, timeout=30.0)
    try:
        resp = client.messages.create(model=model, max_tokens=1, messages=[{"role": "user", "content": "ping"}])
    except anthropic.AuthenticationError:
        raise ConnectionTestFailed("the API key was rejected (authentication failed)") from None
    except anthropic.PermissionDeniedError:
        raise ConnectionTestFailed("the API key is not allowed to use this model or workspace") from None
    except anthropic.NotFoundError:
        raise ConnectionTestFailed(f"the model {model} was not found for this key") from None
    except anthropic.RateLimitError:
        raise ConnectionTestFailed("rate limited or out of credit; try again later") from None
    except anthropic.APIConnectionError:
        raise ConnectionTestFailed("could not reach api.anthropic.com from the machine running Jade's backend") from None
    except anthropic.APIStatusError as exc:
        raise ConnectionTestFailed(f"the API answered HTTP {exc.status_code}") from None
    return f"answered by {resp.model} ({resp.usage.input_tokens} input / {resp.usage.output_tokens} output tokens)"


class ConnectionTestFailed(RuntimeError):
    pass


def test_connection(company_id: str, *, actor: str) -> dict:
    with connection() as conn:
        row = _row(conn, company_id)
    if row is None or not row["credential_secret"]:
        raise InvalidConfig("save the settings and an API key first")
    key = credential_crypto.decrypt(row["credential_secret"])
    try:
        detail, outcome = _send_test_message(key, row["model"]), "ok"
    except ConnectionTestFailed as exc:
        detail, outcome = str(exc), "failed"
    with connection(immediate=True) as conn:
        conn.execute(
            "UPDATE ai_connections SET last_test_at = ?, last_test_outcome = ?, last_test_detail = ?, "
            "last_test_connection_revision = ?, last_test_credential_revision = ? WHERE company_id = ?",
            (_now(), outcome, detail, row["revision"], row["credential_revision"], company_id))
        _audit(conn, company_id, "connection_tested", actor, f"{outcome}: {detail}")
    return {"outcome": outcome, "detail": detail, "billable": True, "connection": view(company_id)}
