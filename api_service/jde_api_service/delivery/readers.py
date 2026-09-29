"""
The target-state readers an exact-change approval is bound to
(jde_mcp_server.binding), registered at start-up.

  functional -- the processing-option value, read LIVE through the
                customer's JD Edwards connection. Unknown when the
                connection cannot read it; never guessed.
  technical  -- for each object the package changes, the checksum of the
                latest source export uploaded for the customer (the customer
                attests which export matches the DEV runtime). A newer
                export after approval is a changed target.
"""

from __future__ import annotations

from jde_mcp_server import binding


def read_item(record: dict, item: dict, actor_user_id=None) -> dict:
    """One configuration item's current state in DEV: {known, value, source}
    or {known: False, reason}. Never guessed."""
    from jde_mcp_server import config_items

    from . import live

    try:
        if item["kind"] == "processing_option":
            v = live.read_processing_option(record["company_id"], record["story_id"], actor_user_id, item["application"],
                                            item["version"], item["option"])
            if not v.found:
                return {"known": False, "reason": v.detail}
            return {"known": True, "value": v.value, "source": f"live AIS read ({v.observation_id})"}
        if item["kind"] in config_items.ROW_KINDS:
            row = live.read_row(record["company_id"], record["story_id"], actor_user_id, item["table"], item["key"],
                                config_items.read_fields(item))
            return {"known": True, "value": {"exists": row.exists, "values": row.values if row.exists else {}},
                    "source": f"live AIS read ({row.observation_id})"}
        return {"known": False, "reason": "AIS does not expose a batch version's saved data selection or sequencing; "
                                          "the person applying it states it, with evidence"}
    except live.LiveUnavailable as exc:
        return {"known": False, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- recorded as unknown, never guessed
        return {"known": False, "reason": f"the live read failed: {exc}"}


def read_change_set(record: dict) -> dict:
    from jde_mcp_server import config_items

    items = {}
    for item in config_items.items_of(record):
        items[item["id"]] = read_item(record, item)
    known = all(i["known"] for i in items.values())
    return {"known": known, "value": {k: i["value"] for k, i in items.items() if i["known"]}, "items": items,
            "target": ", ".join(config_items.target(i) for i in config_items.items_of(record)),
            "source": "live AIS reads per item" if known else "partly unknown: "
            + "; ".join(f"{k}: {i['reason']}" for k, i in items.items() if not i["known"])[:900],
            **({} if known else {"reason": "; ".join(f"{k}: {i['reason']}" for k, i in items.items()
                                                      if not i["known"])[:900]})}


def read_functional(record: dict) -> dict:
    from jde_mcp_server import config_items

    from . import live

    if config_items.is_change_set(record):
        return read_change_set(record)
    op = record.get("operation") or {}
    target = f"{op.get('application')}/{op.get('version')}/{op.get('option')}"
    try:
        value = live.read_processing_option(record["company_id"], record["story_id"], None, op.get("application", ""),
                                            op.get("version", ""), op.get("option", ""))
    except live.LiveUnavailable as exc:
        return {"known": False, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- recorded as unknown, never guessed
        return {"known": False, "reason": f"the live read failed: {exc}"}
    if not value.found:
        return {"known": False, "reason": value.detail}
    return {"known": True, "value": value.value, "target": target,
            "source": f"live AIS read ({value.observation_id})"}


def read_technical(record: dict) -> dict:
    from ..discovery import artifacts as artifact_store
    from ..technical import store

    op = record.get("operation") or {}
    package = store.get_package(record["company_id"], record["story_id"], op.get("package_revision"))
    if package is None or package.get("package_id") != op.get("package_id"):
        return {"known": False, "reason": "the package this change names is not stored"}
    content = package["content"]
    sources = {s.get("evidence_id"): s for s in content.get("sources") or []}
    value = {}
    for cand in content.get("candidates") or []:
        src = sources.get(cand.get("source_ref")) or {}
        artifact_id = src.get("artifact_id")
        latest = artifact_store.get(record["company_id"], artifact_id) if artifact_id else None
        if latest is None:
            return {"known": False, "reason": f"no uploaded source export for {cand['object_key']}"}
        value[cand["object_key"]] = latest["sha256"]
    return {"known": True, "value": value, "target": ", ".join(sorted(value)),
            "source": "latest uploaded source export per object (customer-attested DEV runtime)"}


def register() -> None:
    binding.register_before_reader("functional", read_functional)
    binding.register_before_reader("technical", read_technical)
