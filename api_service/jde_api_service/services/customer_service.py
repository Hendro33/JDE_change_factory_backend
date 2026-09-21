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


def _row_to_customer(row) -> Customer:
    return Customer(
        id=row["id"], name=row["name"], short_name=row["short_name"],
        tools_release=row["tools_release"], environment=row["environment"],
    )


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
                "INSERT INTO companies (id, name, short_name, tools_release, environment, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (c["id"], c["name"], c["short_name"], c["tools_release"], c["environment"], now),
            )
            created.append(c["id"])
    return created
