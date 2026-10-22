"""
Two Administrator tools for setting up test customers.

clear_work(company)   -- removes everything a customer's work produced
  (requests, stories, reviews, designs, changes and their approvals,
  delivery records, evidence, process mappings and maps, findings, as-built
  records, agent runs, discovery activity, uploaded reference documents) and
  keeps the customer's settings: the JD Edwards connection and users, the
  engagement scope, agent switches, the AI and Jira connections, the process
  framework and business domains, people and roles. Optionally also removes
  what Jade's sample data set added: its fictional people (e-mail addresses on
  the reserved .example domain) and its business domains.

copy_setup(source, target) -- gives a new customer a working, integrated set-up
  by copying another customer's settings: the JD Edwards connection (read,
  change and web client users, certificate), the engagement scope, company
  settings (agent switches and the like) and the AI connection. The Jira
  connection is MOVED, not copied: one Jira project feeding two customers would
  import every ticket twice. The copied JD Edwards connection must be tested
  once in its new customer before agents use it.

Both refuse unless the caller is an active Admin of every customer involved.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from typing import Any

from jde_mcp_server import docstore

from ..persistence.db import connection
from . import membership_service

# Document kinds that are settings, not work: never removed by clear_work.
SETTINGS_KINDS = {"engagement_scope", "business_domains", "validation_environments", "validation_policy",
                  "validation_workers"}

# Tables holding work, all keyed by company_id.
WORK_TABLES = (
    "discovery_activity", "discovery_observations", "technical_artifacts", "technical_artifacts_v2",
    "design_baselines", "design_approvals", "technical_runs", "technical_packages", "process_analysis_runs",
    "story_process_mappings", "process_map_versions", "process_maps", "as_built_records", "story_findings",
    "story_revisions", "ai_runs", "ai_context_packages", "request_attachments",
)

# Copied by copy_setup (company_id is the key). Health is reset so the copy is tested again.
CONNECTION_TABLES = ("jde_profiles", "jde_profile_revisions", "jde_ca_certificates", "jde_execution_settings",
                     "ai_connections", "company_settings")
JIRA_TABLES = ("jira_integrations", "jira_credentials")


class NotAllowed(PermissionError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_admin(user_id: str, *companies: str) -> None:
    for c in companies:
        if "admin" not in membership_service.roles_for(user_id, c):
            raise NotAllowed(f"you need to be an Administrator of {c!r} for this")


def _table_exists(conn, table: str) -> bool:
    # A savepoint, so a missing table does not abort the transaction on PostgreSQL.
    conn.execute("SAVEPOINT jade_table_check")
    try:
        conn.execute(f"SELECT 1 FROM {table} WHERE 1 = 0")  # noqa: S608 -- fixed table names only
        conn.execute("RELEASE SAVEPOINT jade_table_check")
        return True
    except Exception:  # noqa: BLE001 -- a table an older schema doesn't have
        conn.execute("ROLLBACK TO SAVEPOINT jade_table_check")
        conn.execute("RELEASE SAVEPOINT jade_table_check")
        return False


def _company_story_ids(conn, company_id: str) -> set[str]:
    rows = conn.execute("SELECT doc_id FROM documents WHERE kind IN ('customer_links', 'change_requests') "
                        "AND company_id = ?", (company_id,)).fetchall()
    return {r["doc_id"] for r in rows}


def _belongs_to_story(doc_id: str, story_ids: set[str]) -> bool:
    if doc_id in story_ids:
        return True
    head = doc_id
    while "-" in head:  # CR-HW-013-CH123 -> CR-HW-013-CH123, CR-HW-013, CR-HW, CR
        head = head.rsplit("-", 1)[0]
        if head in story_ids:
            return True
    return False


def clear_work(company_id: str, *, actor_user_id: str, remove_sample_data: bool = False) -> dict[str, Any]:
    _require_admin(actor_user_id, company_id)
    removed: dict[str, int] = {}
    with connection(immediate=True) as conn:
        story_ids = _company_story_ids(conn, company_id)
        docs = conn.execute("SELECT kind, doc_id, company_id FROM documents").fetchall()
        for d in docs:
            if d["kind"] in SETTINGS_KINDS:
                continue
            if d["company_id"] == company_id or _belongs_to_story(d["doc_id"], story_ids):
                conn.execute("DELETE FROM documents WHERE kind = ? AND doc_id = ?", (d["kind"], d["doc_id"]))
                removed[d["kind"]] = removed.get(d["kind"], 0) + 1
        for table in WORK_TABLES:
            if not _table_exists(conn, table):
                continue
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE company_id = ?",  # noqa: S608
                             (company_id,)).fetchone()["n"]
            if n:
                conn.execute(f"DELETE FROM {table} WHERE company_id = ?", (company_id,))  # noqa: S608
                removed[table] = n
        sample = {"people": [], "domains": []}
        if remove_sample_data:
            from ..sample_data import hoogwegt

            sample_domains = {v[0] for v in hoogwegt.DOMAINS.values()}
            for dom in sample_domains:
                hit = conn.execute("DELETE FROM documents WHERE kind = 'business_domains' AND doc_id = ? "
                                   "AND company_id = ?", (dom, company_id))
                if getattr(hit, "rowcount", 0):
                    sample["domains"].append(dom)
            rows = conn.execute(
                "SELECT m.id, u.email FROM company_memberships m JOIN users u ON u.id = m.user_id "
                "WHERE m.company_id = ? AND m.status = 'active'", (company_id,)).fetchall()
            for r in rows:
                if (r["email"] or "").lower().endswith(".example"):
                    conn.execute("UPDATE company_memberships SET status = 'inactive', updated_at = ?, "
                                 "revision = revision + 1 WHERE id = ?", (_now(), r["id"]))
                    conn.execute("DELETE FROM domain_assignments WHERE membership_id = ?", (r["id"],))
                    sample["people"].append(r["email"])
            if sample_domains:
                marks = ",".join("?" * len(sample_domains))
                conn.execute(f"DELETE FROM domain_assignments WHERE business_domain_id IN ({marks})",  # noqa: S608
                             tuple(sample_domains))
        membership_service.log_access_change(conn, company_id=company_id, actor_user_id=actor_user_id,
                                             action="customer_work_cleared",
                                             detail=f"{len(story_ids)} stories removed")
    from ..config import settings

    files = 0
    base = os.path.join(settings.data_dir, "executions", company_id)
    if os.path.isdir(base):
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if os.path.isdir(path) and _belongs_to_story(name, story_ids):
                shutil.rmtree(path, ignore_errors=True)
                files += 1
    return {"stories": sorted(story_ids), "removed": removed, "execution_folders": files,
            "sample_people_deactivated": sample["people"], "sample_domains_removed": sample["domains"]}


def _copy_rows(conn, table: str, source: str, target: str, reset: dict[str, Any]) -> int:
    if not _table_exists(conn, table):
        return 0
    rows = conn.execute(f"SELECT * FROM {table} WHERE company_id = ?", (source,)).fetchall()  # noqa: S608
    conn.execute(f"DELETE FROM {table} WHERE company_id = ?", (target,))  # noqa: S608
    for row in rows:
        values = dict(row)
        values["company_id"] = target
        for k, v in reset.items():
            if k in values:
                values[k] = v
        cols = list(values)
        conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",  # noqa: S608
                     tuple(values[c] for c in cols))
    return len(rows)


def copy_setup(source_id: str, target_id: str, *, actor_user_id: str, actor_name: str,
               move_jira: bool = True) -> dict[str, Any]:
    if source_id == target_id:
        raise ValueError("choose another customer to copy from")
    _require_admin(actor_user_id, source_id, target_id)
    now = _now()
    copied: dict[str, int] = {}
    with connection(immediate=True) as conn:
        for table in CONNECTION_TABLES:
            reset: dict[str, Any] = {}
            if table in ("jde_profiles", "jde_execution_settings"):
                reset = {"health": "{}", "updated_at": now, "updated_by": actor_name}
            if table == "jde_profiles":
                reset.update({"capability_checks": "{}", "discovery_enabled": 0, "enabled_material_hash": None,
                              "enabled_by": None, "enabled_at": None})
            copied[table] = _copy_rows(conn, table, source_id, target_id, reset)
        scope = conn.execute("SELECT body FROM documents WHERE kind = 'engagement_scope' AND doc_id = ?",
                             (source_id,)).fetchone()
        if scope is not None:
            body = json.loads(scope["body"]) if isinstance(scope["body"], str) else dict(scope["body"])
            body.update({"customer_id": target_id, "revision": 1, "updated_at": now, "updated_by": actor_name})
            docstore.delete("engagement_scope", target_id)
            docstore.put("engagement_scope", target_id, body, company_id=target_id)
            copied["engagement_scope"] = 1
        if move_jira:
            for table in JIRA_TABLES:
                copied[table] = _copy_rows(conn, table, source_id, target_id, {})
                if copied[table]:
                    conn.execute(f"DELETE FROM {table} WHERE company_id = ?", (source_id,))  # noqa: S608
        for company in (source_id, target_id):
            membership_service.log_access_change(conn, company_id=company, actor_user_id=actor_user_id,
                                                 action="customer_setup_copied",
                                                 detail=f"from {source_id} to {target_id}")
    return {"copied": copied, "jira_moved": move_jira and bool(copied.get("jira_integrations")),
            "next": "Open Administration › JD Edwards and click Test once: the copied connection is checked "
                    "again before agents use it."}
