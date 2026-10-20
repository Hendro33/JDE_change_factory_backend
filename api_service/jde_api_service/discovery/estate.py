"""
"Read the JDE estate": a fixed, read-only set of reads through the
customer's JD Edwards read user that shows what the environment holds --
companies, business units and branch plants, items, customers and
suppliers, and a few code lists -- saved as a reference document for the
customer, so people and agents can build stories on what is really there.

Every read goes through discovery.service.execute_read (read-only,
at most 10 rows, recorded in the discovery activity). A read that JDE
refuses or that fails is reported in the document and does not stop the
others.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Optional

from . import artifacts, profile_service, service
from .models import ArtifactUpload

# (title, why, capability, target, fields, filters)
READS: tuple[tuple[str, str, str, str, list[str], list[dict]], ...] = (
    ("Companies", "Legal entities and their base currency", "table_browse", "F0010", ["CO", "NAME", "CRCD"], []),
    ("Branch plants", "Business units of type BP: where stock is kept", "table_browse", "F0006",
     ["MCU", "DL01", "STYL", "CO"], [{"field": "STYL", "op": "=", "value": "BP"}]),
    ("Business units", "A sample of all business units", "table_browse", "F0006", ["MCU", "DL01", "STYL", "CO"], []),
    ("Items", "Item master: number, description, stocking type, unit of measure", "table_browse", "F4101",
     ["ITM", "LITM", "DSC1", "STKT", "UOM1", "GLPT"], []),
    ("Item branch records", "Which items are set up in which branch plant", "table_browse", "F4102",
     ["MCU", "ITM", "LITM", "STKT"], []),
    ("Stock on hand", "Item location quantities", "table_browse", "F41021", ["MCU", "ITM", "LOCN", "LOTN", "PQOH"], []),
    ("Customers", "Address book entries of search type C", "table_browse", "F0101", ["AN8", "ALPH", "AT1"],
     [{"field": "AT1", "op": "=", "value": "C"}]),
    ("Suppliers", "Address book entries of search type V", "table_browse", "F0101", ["AN8", "ALPH", "AT1"],
     [{"field": "AT1", "op": "=", "value": "V"}]),
    ("Line types", "Order line types and their interfaces (GL, inventory)", "table_browse", "F40205",
     ["LNTY", "GLI", "IVI"], []),
    ("Document types", "UDC 00/DT", "udc_values", "00/DT", [], []),
    ("Units of measure", "UDC 00/UM", "udc_values", "00/UM", [], []),
    ("Lot status codes", "UDC 41/L", "udc_values", "41/L", [], []),
)


def _cell(v: Any) -> str:
    return str(v if v is not None else "").strip().replace("|", "/").replace("\n", " ")


def _table(rows: list[dict], fields: list[str]) -> str:
    cols = fields or (list(rows[0]) if rows else [])
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(_cell(r.get(c)) for c in cols) + " |" for r in rows]
    return "\n".join(out)


def read_estate(company_id: str, *, actor_user_id: str, actor_name: str) -> dict[str, Any]:
    profile = profile_service.load(company_id)
    if profile is None:
        raise service.DiscoveryBlocked("set up the JD Edwards connection first")
    config = profile["config"]
    grant = service.admin_grant(company_id, actor_user_id, "estate")
    now = datetime.now(timezone.utc)
    sections: list[str] = []
    summary: list[dict[str, Any]] = []
    for title, why, cap, target, fields, filters in READS:
        rows: list[dict] = []
        outcome: Optional[str] = None
        try:
            service.execute_read(grant, cap, target, fields, filters, 10, raw_out=rows)
        except (service.DiscoveryBlocked, service.DiscoveryFailed) as exc:
            outcome = str(exc)
        label = f"{target}" + (f" where {filters[0]['field']} = {filters[0]['value']}" if filters else "")
        summary.append({"title": title, "target": label, "rows": len(rows), "error": outcome})
        body = f"_Could not read: {outcome}_" if outcome else (
            _table(rows, fields or ["DRKY", "DRDL01", "DRSPHD"]) if rows else "_No rows._")
        sections.append(f"## {title}\n\n{why}. Source: `{label}`, first {len(rows)} rows (at most 10).\n\n{body}")
    env = getattr(config, "environment", "")
    markdown = (
        f"# JD Edwards estate — {env}\n\n"
        f"Read by Jade on {now.strftime('%d %b %Y %H:%M')} UTC through the read user, read-only. Each list shows at "
        "most the first 10 rows JD Edwards returned; it is a sample of what exists, not a complete list.\n\n"
        + "\n\n".join(sections) + "\n"
    )
    upload = ArtifactUpload(
        kind="reference_document", object_name="JDE estate", object_type="Estate snapshot",
        export_format="markdown", customer_environment=env, exported_at=now.isoformat(),
        runtime_correspondence="matches_dev_runtime", runtime_statement="Read live from the connected environment",
        runtime_stated_by=actor_name, doc_title=f"JD Edwards estate {env} ({now.strftime('%d %b %Y')})",
        doc_revision=now.strftime("%Y%m%d%H%M"), file_name=f"jde-estate-{env.lower() or 'jde'}.md",
        content_base64=base64.b64encode(markdown.encode("utf-8")).decode("ascii"),
    )
    created = artifacts.upload(company_id, upload, actor=actor_name)
    if created["revision"] > 1:
        from . import baseline

        baseline.on_artifact_uploaded(company_id, created)
    return {"artifact_id": created["artifact_id"], "revision": created["revision"], "markdown": markdown,
            "reads": summary}
