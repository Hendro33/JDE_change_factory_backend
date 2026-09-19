"""
Customer registry and identity->entitlement resolution.

This is the ONLY place entitlements are decided. Every other module
that needs to know "which customers can this caller see" goes through
resolve_identity() / customer_ids_for() here -- never through a header
value taken at face value.

Identity is a documented stand-in for real authentication (design doc
Section 15.10 -- identity/authorisation is a target-architecture NFR,
not built yet). The two demo personas below are the same ones
src/services/session.ts already uses client-side; the difference that
actually matters is that the entitlement LIST now lives here, resolved
server-side on every request, rather than in the browser's
localStorage where a client could edit it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SEED_DIR = os.path.normpath(os.path.join(_HERE, "..", "persistence"))


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    short_name: str
    tools_release: str
    environment: str


@dataclass(frozen=True)
class Identity:
    id: str
    display_name: str
    role: str
    customer_ids: tuple[str, ...]


class UnknownIdentity(RuntimeError):
    pass


class CustomerRegistry:
    def __init__(self, customers_path: str, identities_path: str) -> None:
        with open(customers_path, "r", encoding="utf-8") as f:
            raw_customers = json.load(f)
        with open(identities_path, "r", encoding="utf-8") as f:
            raw_identities = json.load(f)

        self._customers: dict[str, Customer] = {
            c["id"]: Customer(
                id=c["id"], name=c["name"], short_name=c["short_name"],
                tools_release=c["tools_release"], environment=c["environment"],
            )
            for c in raw_customers
        }
        self._identities: dict[str, Identity] = {
            i["id"]: Identity(
                id=i["id"], display_name=i["display_name"], role=i["role"],
                customer_ids=tuple(i["customer_ids"]),
            )
            for i in raw_identities
        }

    def resolve_identity(self, identity_id: str) -> Identity:
        identity = self._identities.get(identity_id)
        if identity is None:
            raise UnknownIdentity(f"no such identity: {identity_id}")
        return identity

    def customers_for(self, identity: Identity) -> list[Customer]:
        return [self._customers[cid] for cid in identity.customer_ids if cid in self._customers]

    def get_customer(self, customer_id: str) -> Customer | None:
        return self._customers.get(customer_id)

    def is_entitled(self, identity: Identity, customer_id: str) -> bool:
        return customer_id in identity.customer_ids


_registry: CustomerRegistry | None = None


def get_registry() -> CustomerRegistry:
    global _registry
    if _registry is None:
        _registry = CustomerRegistry(
            os.path.join(_SEED_DIR, "seed_customers.json"),
            os.path.join(_SEED_DIR, "seed_identities.json"),
        )
    return _registry
