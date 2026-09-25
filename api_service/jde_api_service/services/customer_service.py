"""
Company ("customer") data access -- SQLite-backed via persistence/db.py.

Kept under this filename (not renamed to company_service.py) because
"customer" is this codebase's own long-established domain term for what
the new work calls a "company" -- every other model in this service
(ChangeRequest.customer_id, BusinessDomain.customer_id, JiraIntegrationConfig
.customer_id, and dozens of call sites across routers/ and services/)
already uses customer_id throughout. Renaming that field and column
everywhere would be a large, purely cosmetic change with no functional
benefit, so this module treats "customer" and "company" as the same
thing and doesn't rename it -- the new `companies` table (see
persistence/migrations.py) is exactly that, named `companies` to match
what was asked for, while every foreign key into it stays `customer_id`
or `company_id` depending on which module already used which name.

WHO belongs to a company and WHAT they can do there is membership_service.py's
job, not this module's -- this module only ever answers "does this
company exist / what is it called."
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..persistence.db import connection


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    short_name: str
    tools_release: str
    environment: str
    is_demo: bool = False


def _row_to_customer(row) -> Customer:
    return Customer(
        id=row["id"], name=row["name"], short_name=row["short_name"],
        tools_release=row["tools_release"], environment=row["environment"],
        is_demo=bool(row["is_demo"]) if "is_demo" in row.keys() else False,
    )


def is_demo_company(company_id: str) -> bool:
    """Demo customers are the only place simulated JDE exists."""
    c = get_registry().get_customer(company_id)
    return bool(c and c.is_demo)


def _clean(value: str, field: str, max_len: int, *, required: bool = False) -> str:
    v = (value or "").strip()
    if required and not v:
        raise ValueError(f"{field} is required")
    if len(v) > max_len:
        raise ValueError(f"{field} is longer than {max_len} characters")
    return v


def update_customer(company_id: str, *, name: str, short_name: str, tools_release: str, environment: str,
                    actor_user_id: str) -> Customer:
    """Edit a customer's own information. The demo flag is not editable."""
    from .membership_service import _require_active_admin, log_access_change

    name = _clean(name, "Name", 120, required=True)
    short_name = _clean(short_name, "Short name", 40) or name[:40]
    tools_release = _clean(tools_release, "Tools release", 40)
    environment = _clean(environment, "Environment", 40)
    now = datetime.now(timezone.utc).isoformat()
    with connection(immediate=True) as conn:
        _require_active_admin(conn, actor_user_id, company_id)
        cur = conn.execute(
            "UPDATE companies SET name = ?, short_name = ?, tools_release = ?, environment = ?, updated_at = ?, "
            "updated_by = ? WHERE id = ?",
            (name, short_name, tools_release, environment, now, actor_user_id, company_id),
        )
        if cur.rowcount == 0:
            raise LookupError(f"no such customer: {company_id}")
        log_access_change(conn, company_id=company_id, actor_user_id=actor_user_id, action="customer_updated",
                          detail=f"name={name}; short_name={short_name}; tools_release={tools_release}; "
                                 f"environment={environment}")
    return get_registry().get_customer(company_id)  # type: ignore[return-value]


def create_customer(*, name: str, short_name: str, tools_release: str, environment: str,
                    creator_user_id: str, creator_company_id: str) -> Customer:
    """A new, real (non-demo) customer. Only an active Admin of a customer
    they already belong to may create one; they become its first Admin."""
    from .membership_service import _require_active_admin, create_membership, log_access_change

    name = _clean(name, "Name", 120, required=True)
    short_name = _clean(short_name, "Short name", 40) or name[:40]
    tools_release = _clean(tools_release, "Tools release", 40)
    environment = _clean(environment, "Environment", 40)
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "customer"
    company_id = f"{base}-{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()
    with connection(immediate=True) as conn:
        _require_active_admin(conn, creator_user_id, creator_company_id)
        conn.execute(
            "INSERT INTO companies (id, name, short_name, tools_release, environment, created_at, is_demo, "
            "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (company_id, name, short_name, tools_release, environment, now, now, creator_user_id),
        )
        log_access_change(conn, company_id=company_id, actor_user_id=creator_user_id, action="customer_created",
                          detail=f"name={name}")
    create_membership(creator_user_id, company_id, ["admin", "product_manager", "dashboard_viewer"],
                      created_by=creator_user_id)
    return get_registry().get_customer(company_id)  # type: ignore[return-value]


class CustomerRegistry:
    def get_customer(self, customer_id: str) -> Optional[Customer]:
        with connection() as conn:
            row = conn.execute("SELECT * FROM companies WHERE id = ?", (customer_id,)).fetchone()
        return _row_to_customer(row) if row else None

    def list_companies(self) -> list[Customer]:
        with connection() as conn:
            rows = conn.execute("SELECT * FROM companies ORDER BY name").fetchall()
        return [_row_to_customer(r) for r in rows]


_registry = CustomerRegistry()


def get_registry() -> CustomerRegistry:
    return _registry


# ---------------------------------------------------------------------
# Seeding -- idempotent, same convention as seed_service.py's dataset
# seeding. Carries over the exact pre-existing demo companies (same
# ids, same names) so nothing already built against "vdb"/"nhd"/"mrv"/
# "bwm" (BicycleWorks -- see seed_service.py's own pilot dataset,
# already keyed to "bwm") needs to change, and so BicycleWorks is never
# accidentally created a second time under a different id.
# ---------------------------------------------------------------------
_SEED_COMPANIES = [
    {"id": "vdb", "name": "Van den Berg Logistiek", "short_name": "Van den Berg", "tools_release": "9.2.7", "environment": "DEV"},
    {"id": "nhd", "name": "Noord-Holland Dairy", "short_name": "NH Dairy", "tools_release": "9.2.8", "environment": "DEV"},
    {"id": "mrv", "name": "Maasrivier Industrials", "short_name": "Maasrivier", "tools_release": "9.2.5", "environment": "DEV"},
    {"id": "bwm", "name": "BicycleWorks Manufacturing BV", "short_name": "BicycleWorks", "tools_release": "9.2.7", "environment": "DEV"},
]


def ensure_seed_companies() -> list[str]:
    """Returns the ids of any companies actually created (empty if all
    were already present)."""
    created: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    with connection() as conn:
        for c in _SEED_COMPANIES:
            existing = conn.execute("SELECT id FROM companies WHERE id = ?", (c["id"],)).fetchone()
            if existing is not None:
                continue
            conn.execute(
                "INSERT INTO companies (id, name, short_name, tools_release, environment, created_at, is_demo) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (c["id"], c["name"], c["short_name"], c["tools_release"], c["environment"], now),
            )
            created.append(c["id"])
    return created
