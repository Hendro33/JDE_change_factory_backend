"""Hoogwegt Arnhem -- a sample customer for testing Jade's screens and dashboards.

A fictional data set for a dairy trading company that runs JD Edwards: its
business domains, the people who decide (Domain Owners, an Application
Manager, a Test Manager), and twenty requests spread across the whole
lifecycle -- new requests, stories in review, solutions waiting for a
decision, changes being delivered and validated, and delivered stories with
finalised as-built records. Timestamps are spread over the past months so
Insights shows trends.

Every record is written through the same services the agents and screens use,
so the lifecycle, My Work, dashboards and as-built records read them exactly
as they would read real work. Two things are deliberately NOT set up, because
an administrator does that: the JD Edwards connection, Jira, the AI
connection and governance (scope and approval policy). The sample's approved
changes and recorded delivery steps are therefore written as recorded
history; they do not depend on, and do not configure, any of those.

All people are fictional (@hoogwegt.example addresses, which cannot receive
e-mail). They get accounts so their names appear as decision makers, but no
known password: an administrator can invite or reset them if wanted.

Run once per installation, by hand:

    python -m jde_api_service.sample_data.hoogwegt --admin-email you@example.com

`--admin-email` is an existing user who is given every role on the new
customer so they can see it. The run refuses if the customer already exists.
"""

from __future__ import annotations

import argparse
import contextlib
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

COMPANY_ID = "hoogwegt"
COMPANY = {"name": "Hoogwegt Arnhem", "short_name": "Hoogwegt", "tools_release": "9.2.8", "environment": "DEV"}
DOMAIN_EMAIL = "hoogwegt.example"

# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------
PEOPLE = {
    "sales": ("u-hw-marieke", "Marieke Jansen", ["domain_owner", "dashboard_viewer"]),
    "supply": ("u-hw-pieter", "Pieter van Dijk", ["domain_owner", "dashboard_viewer"]),
    "finance": ("u-hw-ellen", "Ellen Bos", ["domain_owner", "dashboard_viewer"]),
    "quality": ("u-hw-joost", "Joost Mulder", ["domain_owner", "dashboard_viewer"]),
    "am": ("u-hw-sanne", "Sanne de Wit", ["product_manager", "dashboard_viewer"]),
    "test": ("u-hw-ruud", "Ruud Peters", ["test_manager", "dashboard_viewer"]),
    "cnc": ("u-hw-kees", "Kees Visser", ["cnc_operator"]),
}

# ---------------------------------------------------------------------------
# Business domains (APQC-aligned)
# ---------------------------------------------------------------------------
DOMAINS = {
    "sales": ("DOM-HW-SELL", "3.0", "Sell dairy products", "1",
              "Contracts, spot and forward sales of milk powders, butter, cheese and whey to customers worldwide; "
              "pricing, order entry and customer credit."),
    "supply": ("DOM-HW-SUPPLY", "4.0", "Source, store and ship dairy products", "1",
               "Buying from dairy producers and cooperatives, third-party cold stores, lot and shelf-life control, "
               "container shipments and export documentation."),
    "finance": ("DOM-HW-FIN", "9.0", "Manage financial resources", "1",
                "Accounts receivable and payable, currency exposure and hedging, accruals, month-end close."),
    "quality": ("DOM-HW-QUAL", "11.0", "Assure quality and food safety", "1",
                "Product specifications, certificates of analysis, health certificates, recalls and audits."),
}


# ---------------------------------------------------------------------------
# The requests
# ---------------------------------------------------------------------------
def _story(statement, context, criteria, tests, rules=(), assumptions=(), questions=(), impact="Medium",
           benefit="Medium"):
    return {
        "statement": statement, "business_context": context, "business_impact_rating": impact,
        "business_benefit_rating": benefit,
        "acceptance_criteria": [{"id": f"AC{i}", "text": t, "verified_by": f"T{i}"} for i, t in enumerate(criteria, 1)],
        "test_script": [{"id": f"T{i}", "action": a, "expected": e} for i, (a, e) in enumerate(tests, 1)],
        "business_rules": list(rules), "assumptions": list(assumptions), "open_questions": list(questions),
    }


def _po(app, version, option, value, before, summary, test=""):
    """A processing-option change the Functional Agent would propose."""
    return {"tool": "set_processing_option", "application": app, "version": version, "option": option,
            "value": value, "summary": summary, **({"test_orchestration": test} if test else {}), "_before": before}


# stage: request | needs_input | unassigned | do_review | am_decision | rejected | solutioning | clarify |
#        resolve | tech_design | exact_pending | approved | applied | tested | done
REQUESTS = [
    dict(id="CR-HW-001", days_ago=160, stage="done", domain="supply", source="Business", ref="Mail S. Kok",
         requester="Logistics planning", title="Temperature requirement on export sales order lines",
         raw="Reefer containers are booked by the shipping desk from the sales order, but the required transport "
             "temperature is only in an e-mail from sales. Twice this year butter was shipped at the wrong setting. "
             "We want the temperature requirement on the sales order line, defaulted from the item, so the "
             "shipping desk sees it when booking.",
         story=_story("As a shipping planner I want the required transport temperature on each export sales order "
                      "line, defaulted from the item, so that reefer containers are always booked at the right "
                      "setting.",
                      "Chilled and frozen dairy is shipped in reefer containers. The setting is currently passed on "
                      "by e-mail, which caused two incorrect shipments this year.",
                      ["The transport temperature defaults from the item on every new export order line.",
                       "The temperature is shown on the shipping desk's booking view.",
                       "Changing the temperature on the line is possible and kept on the order."],
                      [("Enter an export order for frozen butter (item 30120).", "Line shows -18 C."),
                       ("Open the booking view for the order.", "Booking view shows -18 C."),
                       ("Change the line to -20 C and save.", "Order keeps -20 C.")],
                      rules=["Applies to export orders only (order types SX and SF)."], impact="High", benefit="High"),
         route="Functional Agent", cycle=21,
         change=_po("P4210", "HW0010", "PTEMPDEF", "1", "0",
                    "Default the transport temperature from the item branch record on export order lines")),
    dict(id="CR-HW-002", days_ago=75, stage="done", domain="supply", source="Support / Topdesk", ref="TD-24117",
         requester="Shipping desk", title="Default carrier for Rotterdam export shipments",
         raw="For almost all containers leaving via Rotterdam we use the same carrier, but the order always comes "
             "in without a carrier and the desk has to fill it in by hand. Can the carrier default for branch "
             "ARN-RTM?",
         story=_story("As a shipping clerk I want export orders shipped from branch ARN-RTM to default to our "
                      "contracted Rotterdam carrier, so that I only change the carrier for the exceptions.",
                      "About 90% of Rotterdam containers use carrier 41200. The field is filled in by hand today.",
                      ["New export orders for branch ARN-RTM default to carrier 41200.",
                       "Orders for other branches are not affected."],
                      [("Enter an export order for branch ARN-RTM.", "Carrier is 41200."),
                       ("Enter an export order for branch ARN-HAM.", "Carrier is empty, as before.")],
                      impact="Low", benefit="Medium"),
         route="Functional Agent", cycle=9,
         change=_po("P4210", "HW0011", "PCARRDEF", "41200", "",
                    "Default carrier 41200 on export orders for branch ARN-RTM")),
    dict(id="CR-HW-003", days_ago=48, stage="done", domain="sales", source="Business", ref="Sales meeting 12-08",
         requester="Head of sales", title="Default order type for spot sales of milk powder",
         raw="Spot deals in skimmed milk powder are entered as normal contract orders (SO) and then changed. That "
             "goes wrong regularly and the spot margin report misses them. The spot desk version of sales order "
             "entry should default to order type S5.",
         story=_story("As a spot trader I want orders I enter in the spot desk version of sales order entry to "
                      "default to order type S5, so that spot deals are reported correctly without retyping.",
                      "Spot deals are a separate margin line in management reporting. Mistyped order types hide "
                      "about 4% of spot volume from the report.",
                      ["Orders entered through version HW0001 default to order type S5.",
                       "Other versions of sales order entry keep their current default (SO)."],
                      [("Enter an order through P4210 version HW0001.", "Order type is S5."),
                       ("Enter an order through P4210 version ZJDE0001.", "Order type is SO.")],
                      rules=["Only the spot desk version HW0001 changes."], impact="Medium", benefit="High"),
         route="Functional Agent", cycle=12,
         change=_po("P4210", "HW0001", "PDOCTYPE", "S5", "SO", "Spot desk orders default to order type S5")),
    dict(id="CR-HW-004", days_ago=26, stage="done", domain="sales", source="Business", ref="Mail D. Smit",
         requester="Export sales", title="Pricing unit of measure MT on export orders",
         raw="Export customers buy per metric tonne but our export order entry prices per kilo by default. Every "
             "price is converted by hand. Please make MT the default pricing unit for export orders.",
         story=_story("As an export sales assistant I want export order entry to price in metric tonnes by "
                      "default, so that I enter the customer's contract price without converting it.",
                      "Export contracts are priced per MT; prices are converted manually today, with rounding "
                      "differences on invoices.",
                      ["Export order entry (version HW0002) prices in MT by default.",
                       "The invoice shows the price per MT."],
                      [("Enter an export order in P4210 version HW0002.", "Pricing UoM is MT."),
                       ("Print the invoice for the order.", "Price is shown per MT.")],
                      impact="Medium", benefit="Medium"),
         route="Functional Agent", cycle=8,
         change=_po("P4210", "HW0002", "PPRCUOM", "MT", "KG", "Export orders price per metric tonne")),
    dict(id="CR-HW-005", days_ago=19, stage="tested", domain="quality", source="Business", ref="Audit finding QA-07",
         requester="Quality manager", title="Block shipment of lots past their expiry date",
         raw="The FSSC audit found that the shipment confirmation does not stop us from shipping a lot whose "
             "expiry date has passed. It relies on the warehouse noticing. We need the system to refuse it.",
         story=_story("As a quality manager I want shipment confirmation to refuse lots that are past their expiry "
                      "date, so that we never ship expired product.",
                      "Audit finding QA-07 (FSSC 22000). Today only a visual check prevents shipping expired lots.",
                      ["Confirming a shipment with an expired lot is refused with a clear message.",
                       "Lots that expire later than today ship as before."],
                      [("Confirm shipment of an order allocated to expired lot L2409-118.", "Refused: lot expired."),
                       ("Confirm shipment of an order with a valid lot.", "Shipment confirmed.")],
                      rules=["The check uses the lot expiry date, not the best-before date."], impact="High",
                      benefit="High"),
         route="Functional Agent",
         change=_po("P4205", "HW0001", "PLOTEXP", "1", "0", "Refuse expired lots at shipment confirmation")),
    dict(id="CR-HW-006", days_ago=14, stage="applied", domain="supply", source="Business", ref="Mail F. Vos",
         requester="Purchasing", title="Landed cost rule for container freight on purchase receipts",
         raw="Freight for incoming containers is booked as a separate invoice and never reaches the item cost. "
             "Purchase receipts from overseas suppliers should apply landed cost rule FRT automatically.",
         story=_story("As a purchaser I want receipts on overseas purchase orders to apply landed cost rule FRT "
                      "automatically, so that freight is part of the item cost.",
                      "Item costs for imported products are understated by the freight, which distorts margins.",
                      ["Receipts through P4312 version HW0001 apply landed cost rule FRT.",
                       "Receipts for domestic suppliers are unchanged."],
                      [("Receive overseas PO 4410023 in P4312 version HW0001.", "Landed cost FRT applied."),
                       ("Receive domestic PO 4410024.", "No landed cost applied.")],
                      impact="Medium", benefit="Medium"),
         route="Functional Agent",
         change=_po("P4312", "HW0001", "PLNDCOST", "FRT", "", "Apply landed cost rule FRT on overseas receipts")),
    dict(id="CR-HW-007", days_ago=10, stage="approved", domain="finance", source="Business", ref="Treasury",
         requester="Treasury", title="Hold export orders without FX hedge cover",
         raw="Large USD orders are sometimes confirmed before treasury has hedged them. We want such orders to go "
             "on hold so treasury can release them once the hedge is in place.",
         story=_story("As a treasurer I want export orders in a foreign currency that are entered through the "
                      "contract desk to be placed on hold code FX, so that no order ships before its currency is "
                      "hedged.",
                      "Unhedged USD exposure caused a loss on two large orders in Q2.",
                      ["Foreign-currency orders from version HW0003 are placed on hold FX.",
                       "Treasury can release the hold in the standard hold release screen."],
                      [("Enter a USD order in P4210 version HW0003.", "Order is on hold FX."),
                       ("Release the hold as treasury.", "Order continues normally.")],
                      impact="High", benefit="High"),
         route="Functional Agent",
         change=_po("P4210", "HW0003", "PHOLDCD", "FX", "", "Place foreign-currency contract orders on hold FX")),
    dict(id="CR-HW-008", days_ago=8, stage="exact_pending", domain="finance", source="Business", ref="AP team",
         requester="Accounts payable", title="Default payment terms for EU dairy cooperatives",
         raw="Suppliers that are EU dairy cooperatives all have 30-day terms, but new supplier records come in "
             "with immediate payment. Please default the payment terms to N30 for that supplier category.",
         story=_story("As an accounts payable clerk I want new suppliers entered in the cooperative supplier "
                      "version to default to payment terms N30, so that cooperatives are not paid early.",
                      "Early payments cost interest; three cooperatives were paid immediately last month.",
                      ["New suppliers entered through P04012 version HW0001 default to N30.",
                       "Existing suppliers are not changed."],
                      [("Add a supplier in P04012 version HW0001.", "Payment terms N30."),
                       ("Open an existing cooperative supplier.", "Terms unchanged.")],
                      impact="Low", benefit="Medium"),
         route="Functional Agent",
         change=_po("P04012", "HW0001", "PPTC", "N30", "", "Default payment terms N30 for cooperative suppliers")),
    dict(id="CR-HW-009", days_ago=7, stage="tech_design", domain="quality", source="Business", ref="QA-11",
         requester="Quality manager", title="Health certificate print per shipment",
         raw="For every export shipment the quality team prepares a health certificate in Word from the order "
             "and lot data. We want JDE to print it per shipment with the lots and their analysis results.",
         story=_story("As a quality officer I want to print the health certificate for a shipment from JDE, with "
                      "its lots and analysis results, so that I no longer prepare it by hand.",
                      "About 60 certificates per week are prepared manually from order and lot data.",
                      ["A health certificate prints per shipment with all its lots.",
                       "Each lot shows its analysis results from the quality module."],
                      [("Print the certificate for shipment 88412.", "All three lots with results are printed.")],
                      assumptions=["The certificate layout follows the NVWA template."], impact="Medium",
                      benefit="High"),
         route="Technical Agent"),
    dict(id="CR-HW-010", days_ago=6, stage="clarify", domain="supply", source="Support / Topdesk", ref="TD-25003",
         requester="Shipping desk", title="Customs tariff code on the commercial invoice",
         raw="Customs asks for the HS code on every commercial invoice line. Can JDE print it?",
         story=_story("As a shipping clerk I want the customs tariff (HS) code on every commercial invoice line, "
                      "so that customs clearance is not delayed.",
                      "Two shipments were held at customs in September for missing HS codes.",
                      ["Every commercial invoice line shows the item's HS code."],
                      [("Print the commercial invoice for order 120455.", "Each line shows its HS code.")],
                      questions=["Is the HS code the same for all destinations, or country specific?"],
                      impact="Medium", benefit="Medium"),
         route="Clarification Required"),
    dict(id="CR-HW-011", days_ago=5, stage="resolve", domain="finance", source="Business", ref="Credit control",
         requester="Credit control", title="Credit limit warning in the customer's currency",
         raw="Credit control wants the credit limit warning to show the amounts in the customer's own currency "
             "instead of EUR.",
         story=_story("As a credit controller I want the credit limit warning to show amounts in the customer's "
                      "currency, so that sales can explain it to the customer.",
                      "Warnings in EUR confuse sales staff dealing with USD customers.",
                      ["The credit warning shows the exposure in the customer's currency."],
                      [("Enter an order over the limit for USD customer 50120.", "Warning shows USD amounts.")],
                      impact="Low", benefit="Small"),
         route="Resolve without Change"),
    dict(id="CR-HW-012", days_ago=4, stage="solutioning", domain="quality", source="Business", ref="Lab",
         requester="Laboratory", title="Fat and protein content on purchase receipt lots",
         raw="When raw materials are received we get fat and protein percentages from the supplier. These should "
             "be stored on the lot so we can select lots for customer specifications.",
         story=_story("As a lab analyst I want to record fat and protein percentages on each received lot, so "
                      "that lots can be selected against customer specifications.",
                      "Specifications are matched by hand from supplier certificates today.",
                      ["Fat and protein percentages can be entered at receipt and are kept on the lot.",
                       "Lots can be searched by fat and protein range."],
                      [("Receive a lot with 26.1% fat and 24.0% protein.", "Values stored on the lot."),
                       ("Search lots with fat between 26 and 27%.", "The lot is found.")],
                      impact="Medium", benefit="High"),
         route=None),
    dict(id="CR-HW-013", days_ago=4, stage="am_decision", domain="finance", source="Business", ref="Controller",
         requester="Financial controller", title="Monthly accrual for unbilled cold-store costs",
         raw="Third-party cold stores invoice weeks late. At month end we need an accrual for the storage costs "
             "not yet invoiced, based on pallets in stock.",
         story=_story("As a financial controller I want a monthly accrual for unbilled cold-store costs based on "
                      "pallets in stock, so that month-end results include storage costs.",
                      "Storage invoices arrive 3-6 weeks late; results swing by up to EUR 80k per month.",
                      ["At month end an accrual is proposed per cold store from pallets in stock and the contract "
                       "rate.", "The accrual reverses on the first day of the next month."],
                      [("Run the month-end accrual for September.", "One accrual line per cold store."),
                       ("Open October's first day journal.", "The accrual is reversed.")],
                      impact="High", benefit="High"),
         route=None),
    dict(id="CR-HW-014", days_ago=3, stage="do_review", domain="supply", source="Business", ref="Warehouse",
         requester="Warehouse manager", title="Remaining shelf life on pick slips",
         raw="Pickers should see the remaining shelf life of each lot on the pick slip, so customers with "
             "minimum shelf-life requirements get the right lots.",
         story=_story("As a warehouse picker I want the remaining shelf life of each lot printed on the pick slip, "
                      "so that I pick lots that meet the customer's minimum shelf life.",
                      "Three claims this year for short shelf life on delivery.",
                      ["The pick slip shows remaining shelf life in days for each lot.",
                       "Lots below the customer's minimum are marked."],
                      [("Print a pick slip for order 120501.", "Each lot shows remaining days.")],
                      assumptions=["The customer minimum shelf life is held on the customer billing instructions."],
                      impact="Medium", benefit="Medium"),
         route=None),
    dict(id="CR-HW-015", days_ago=3, stage="do_review", domain="sales", source="Business", ref="Sales ops",
         requester="Sales operations", title="Forward contract balance on the customer ledger inquiry",
         raw="Sales wants to see the remaining quantity on open forward contracts when looking at a customer.",
         story=_story("As an account manager I want to see the open balance of each forward contract when I look "
                      "up a customer, so that I know what the customer still has to call off.",
                      "Contract balances are kept in a spreadsheet by sales operations.",
                      ["The customer inquiry lists open forward contracts with their remaining quantity."],
                      [("Look up customer 50088.", "Two open contracts with remaining MT are shown.")],
                      impact="Low", benefit="Medium"),
         route=None),
    dict(id="CR-HW-016", days_ago=2, stage="unassigned", domain=None, source="Support / Topdesk", ref="TD-25110",
         requester="Quality assurance", title="Reclassify stock with expiring certificates",
         raw="When a product certificate expires, the stock should automatically get a different status so it "
             "cannot be sold until the certificate is renewed.",
         story=_story("As a quality officer I want stock whose product certificate has expired to be blocked for "
                      "sale automatically, so that we never sell uncertified product.",
                      "Certificates (e.g. halal, organic) are tracked in a spreadsheet today.",
                      ["Stock with an expired certificate gets lot status Q and cannot be allocated."],
                      [("Expire the organic certificate for item 30410.", "Its lots get status Q.")],
                      impact="High", benefit="Medium"),
         route=None),
    dict(id="CR-HW-017", days_ago=30, stage="rejected", domain="sales", source="Business", ref="Sales team",
         requester="Sales team", title="Let sales override the minimum order quantity",
         raw="Sales reps want to override the minimum order quantity on any order without approval.",
         story=_story("As a sales rep I want to override the minimum order quantity on an order, so that I can "
                      "accept small orders from key customers.",
                      "Minimum order quantities protect container utilisation.",
                      ["A sales rep can enter a quantity below the minimum on any order."],
                      [("Enter 5 MT for item 30120 with a 20 MT minimum.", "Order is accepted.")],
                      impact="Medium", benefit="Small"),
         route=None, reject_note="Overriding minimums without approval undermines container planning. Small orders "
                                 "for key customers go through the existing exception process."),
    dict(id="CR-HW-018", days_ago=2, stage="needs_input", domain=None, source="Support / Topdesk", ref="TD-25131",
         requester="Warehouse", title="Weight variance on goods receipt",
         raw="Weights are always different on receipt. Please fix.",
         story=_story("As a warehouse clerk I want weight differences on goods receipt handled automatically.",
                      "", [], [], questions=["Which tolerance applies, and per product group or per supplier?",
                                             "What should happen when the difference exceeds the tolerance?"],
                      impact="Low", benefit="Small"),
         route=None),
    dict(id="CR-HW-019", days_ago=1, stage="request", domain=None, source="Business", ref="Purchasing",
         requester="Purchasing", title="Producer milk price adjustments without re-keying",
         raw="Each month the cooperatives send their milk price adjustments as Excel. We re-key them into the "
             "purchase price records. We would like to load them directly.", story=None, route=None),
    dict(id="CR-HW-020", days_ago=0, stage="request", domain=None, source="Business", ref="Treasury",
         requester="Treasury", title="Daily FX revaluation report for open sales contracts",
         raw="Treasury needs a daily overview of open sales contracts in foreign currency revalued at today's rate.",
         story=None, route=None),
]

_ORDER = ["request", "needs_input", "unassigned", "do_review", "am_decision", "solutioning", "decided",
          "exact_pending", "approved", "applied", "tested", "done"]


def _at_least(stage: str, step: str) -> bool:
    mapped = {"rejected": "do_review", "clarify": "decided", "resolve": "decided", "tech_design": "decided"}
    return _ORDER.index(mapped.get(stage, stage)) >= _ORDER.index(step)


# ---------------------------------------------------------------------------
# A simulated clock: every timestamp a service writes while a step runs is the
# time that step would have happened, so the history reads naturally and
# Insights has trends. Evidence hash chains stay valid because entries are
# written in order with these times.
# ---------------------------------------------------------------------------
_CLOCK = {"t": None}


@contextlib.contextmanager
def simulated_clock() -> Iterator[None]:
    from jde_mcp_server import execution

    from ..discovery import baseline
    from ..process import asbuilt
    from ..services import (architecture_review_service, decision_feedback_service, delivery_queue_service,
                            domain_review_service, enhancement_run_service)

    real_time = time.time

    def fake_time() -> float:
        return _CLOCK["t"] if _CLOCK["t"] is not None else real_time()

    def iso() -> str:
        return datetime.fromtimestamp(fake_time(), tz=timezone.utc).isoformat()

    patched = [(m, "_now", iso) for m in (domain_review_service, architecture_review_service, enhancement_run_service,
                                          decision_feedback_service, delivery_queue_service, asbuilt, baseline)]
    patched.append((execution, "_now", fake_time))
    saved = [(m, name, getattr(m, name)) for m, name, _ in patched]
    time.time = fake_time  # type: ignore[assignment]
    for m, name, fn in patched:
        setattr(m, name, fn)
    try:
        yield
    finally:
        time.time = real_time  # type: ignore[assignment]
        for m, name, fn in saved:
            setattr(m, name, fn)
        _CLOCK["t"] = None


def _set_clock(when: datetime) -> None:
    _CLOCK["t"] = when.timestamp()


# ---------------------------------------------------------------------------
# Setup: customer, people, domains
# ---------------------------------------------------------------------------
def _create_customer(admin_user_id: str) -> None:
    from ..persistence.db import connection
    from ..services import membership_service

    now = datetime.now(timezone.utc).isoformat()
    with connection(immediate=True) as conn:
        conn.execute(
            "INSERT INTO companies (id, name, short_name, tools_release, environment, created_at, is_demo, updated_at, "
            "updated_by) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (COMPANY_ID, COMPANY["name"], COMPANY["short_name"], COMPANY["tools_release"], COMPANY["environment"],
             now, now, admin_user_id))
        membership_service.log_access_change(conn, company_id=COMPANY_ID, actor_user_id=admin_user_id,
                                             action="customer_created", detail="sample data set: Hoogwegt Arnhem")
    from ..models.auth import ALL_ROLES

    membership_service.create_membership(admin_user_id, COMPANY_ID, list(ALL_ROLES), created_by=admin_user_id)


def _create_people(admin_user_id: str) -> dict[str, dict]:
    from ..services import auth_service, membership_service

    out = {}
    for key, (user_id, name, roles) in PEOPLE.items():
        email = f"{name.split()[0].lower()}.{name.split()[-1].lower()}@{DOMAIN_EMAIL}"
        if auth_service.get_user_by_id(user_id) is None:
            auth_service.create_user(email, secrets.token_urlsafe(24) + "aA1!", name, user_id=user_id)
        membership_service.create_membership(user_id, COMPANY_ID, roles, created_by=admin_user_id)
        out[key] = {"id": user_id, "name": name}
    return out


def _create_domains(people: dict[str, dict]) -> None:
    from ..models.business_domain import BusinessDomainCreate
    from ..persistence.db import connection
    from ..services.registry import get_business_domain_service

    service = get_business_domain_service()
    for key, (domain_id, code, name, level, description) in DOMAINS.items():
        service.create(BusinessDomainCreate(apqc_code=code, name=name, level=level, description=description,
                                            domain_owner=people[key]["name"]),
                       customer_id=COMPANY_ID, domain_id=domain_id, actor="sample data")
        with connection(immediate=True) as conn:
            row = conn.execute("SELECT id FROM company_memberships WHERE user_id = ? AND company_id = ?",
                               (people[key]["id"], COMPANY_ID)).fetchone()
            conn.execute("INSERT INTO domain_assignments (membership_id, business_domain_id) VALUES (?, ?) "
                         "ON CONFLICT DO NOTHING", (row["id"], domain_id))


# ---------------------------------------------------------------------------
# One request through its stages
# ---------------------------------------------------------------------------
def _user_story(raw: dict):
    from ..services.orchestration_driver import _user_story_from_summary

    return _user_story_from_summary({"user_story": {**raw, "quality_status": "passed", "revision_count": 0}})


def _impact(spec: dict) -> dict:
    level = (spec["story"] or {}).get("business_impact_rating", "Medium")
    return {"financial_impact": f"{level} -- see business context", "operational_reach": spec["requester"],
            "risk_compliance": "Audit relevant" if spec["domain"] == "quality" else "",
            "strategic_alignment": "", "urgency": "High" if level == "High" else "Normal"}


def _decision(spec: dict, when: datetime):
    from ..models.change import ArchitectDecision, ImplementationSpecification

    route = spec["route"]
    change = spec.get("change") or {}
    target = f"{change.get('application')} version {change.get('version')}" if change else ""
    texts = {
        "Functional Agent": (
            f"{target} exists in DEV; processing option {change.get('option')} currently holds "
            f"{change.get('_before') or 'blank'}. The requirement is met by setting it to {change.get('value')}.",
            [f"Set processing option {change.get('option')} of {target} to {change.get('value')} in DEV.",
             "Re-read the option to confirm the approved value.",
             "Run the story's test script in DEV and record the results."],
            ["Promote the version through CNC after the as-built record is finalised."],
            "Story test script in DEV; neighbouring versions checked for unchanged behaviour.",
            f"Restore {change.get('option')} to {change.get('_before') or 'blank'} as its own approved change."),
        "Technical Agent": (
            "No standard report prints certificate data per shipment. Lot analysis results are held in F37311 and "
            "shipment lots in F4211/F4108.",
            ["Create custom report R5537001 reading shipment lots and their analysis results.",
             "Add a print option to the shipment confirmation exit."],
            ["CNC builds and deploys the package to DV920.", "Quality confirms the layout."],
            "Positive, negative and neighbouring tests on three shipments with known lots.",
            "Remove the report and menu entry; no standard objects change."),
        "Clarification Required": (
            "HS codes exist on the item master (category code 22) for 70% of export items, but one item has "
            "different codes per destination country.",
            [], ["Domain Owner answers whether HS codes are destination specific."],
            "", ""),
        "Resolve without Change": (
            "The credit check in JDE already shows the exposure in the customer currency when the credit "
            "message version HW0002 is used; the sales team uses the default version.",
            [], ["Ask sales to use the credit message version HW0002 (menu G42HW, option 4)."],
            "Confirm with credit control on two USD customers.", ""),
    }
    found, sequence, human, validation, rollback = texts[route]
    return (ArchitectDecision(recommended_route=route, confidence=0.85 if route == "Functional Agent" else 0.7,
                              technical_impact_rating="Low" if route != "Technical Agent" else "Medium",
                              existing_functionality_found=found,
                              alternatives_considered=[{"option": "Manual workaround", "reason_rejected":
                                                        "keeps the error-prone manual step"}],
                              objects_affected=[target] if change else (["R5537001"] if route == "Technical Agent" else []),
                              dependencies_and_conflicts=[], rollback_strategy=rollback,
                              decided_at=when.isoformat()),
            ImplementationSpecification(sequence=sequence, required_mcp_operations=["set_processing_option"]
                                        if change else [], human_actions_required=human,
                                        validation_approach=validation))


def _record_design(story_id: str, domain_id: Optional[str], change_id: Optional[str]) -> None:
    from ..discovery.architect_tools import ArchitectDiscoveryTools
    from ..services import architecture_driver
    from ..services.registry import get_architecture_review_service

    tools = ArchitectDiscoveryTools(company_id=COMPANY_ID, story_id=story_id, domain_id=domain_id, grant=None,
                                    no_grant_reason="sample data: no JD Edwards connection configured")
    summary = {"evidence": {"confidence_limitations": [
        "Sample data: no JD Edwards connection was configured, so no live observations were recorded."]}}
    architecture_driver.record_design_baseline(
        story_id=story_id, customer_id=COMPANY_ID, run_service=get_architecture_review_service(), tools=tools,
        summary=summary, agent_run_id=None, initiated_by=None, proposed_change_id=change_id)


def _approve(change_id: str, am: dict) -> None:
    from jde_mcp_server import approval, binding

    record = approval._load(change_id)  # noqa: SLF001 -- recorded history, see the module docstring
    now = time.time()
    record.update({
        "status": "approved", "approved_by": am["name"], "approved_at": now, "expires_at": now + 24 * 3600,
        "approver_authority": {"user_id": am["id"], "roles": ["product_manager"], "policy_version": 1,
                               "scope_revision": "sample data"},
        "decision_note": "Reviewed the proposed value and the test approach.",
        "binding": binding.snapshot(record),
    })
    approval._save(change_id, record)  # noqa: SLF001


def _apply(change_id: str, spec: dict, am: dict) -> None:
    from jde_mcp_server import approval, execution
    from jde_mcp_server.evidence import capture_evidence

    record = approval._load(change_id)  # noqa: SLF001
    op = record["operation"]
    value, before = str(op["value"]), spec["change"]["_before"]
    target = f"{op['application']}/{op['version']}/{op['option']}"
    source = f"stated by {am['name']} (read in JDE); sample data"
    reference = f"Screenshot {spec['id']}-DEV-1"
    execution.record(change_id, execution.WRITE, "applied", before_value=before or None,
                     detail=f"{target} = {value!r} in DEV ({source})",
                     recorded={"by": am["name"], "user_id": am["id"], "observed_value": value, "source": source,
                               "evidence_reference": reference, "note": "Applied in DV920 and re-read.",
                               "at": time.time()})
    capture_evidence(spec["id"], {
        "event": "applied_in_dev", "stage": "delivery", "actor": am["name"], "change_id": change_id,
        "detail": f"{target} set to {value!r} in DEV (before: {before!r}); {source}; evidence: {reference}",
        "operation": op, "before_value": before, "observed_value": value, "source": source,
        "evidence_reference": reference, "note": ""})


def _test(change_id: str, spec: dict, tester: dict) -> None:
    from jde_mcp_server import approval, execution
    from jde_mcp_server.evidence import capture_evidence

    note = "Ran the story's test script in DV920: " + "; ".join(
        f"{t['id']} passed" for t in spec["story"]["test_script"])
    reference = f"Test log {spec['id']}-T1"
    execution.record(change_id, execution.TEST, "completed",
                     detail=f"test passed (recorded by {tester['name']})",
                     recorded={"by": tester["name"], "user_id": tester["id"], "passed": True,
                               "evidence_reference": reference, "note": note})
    outcome = {"passed": True, "source": "recorded by a person", "by": tester["name"], "at": time.time(),
               "evidence_reference": reference, "note": note}
    outcome["evidence_entry_hash"] = capture_evidence(spec["id"], {
        "event": "test_result_recorded", "stage": "validation", "actor": tester["name"], "change_id": change_id,
        "detail": f"test PASSED in DEV, recorded by {tester['name']}: {note} (evidence: {reference})"})["entry_hash"]
    record = approval._load(change_id)  # noqa: SLF001
    record["verification"] = outcome
    approval._save(change_id, record)  # noqa: SLF001


def _confirm_ratings(story_id: str, person: dict, keys: dict[str, str]) -> None:
    """Version-bound rating confirmations, as the ratings endpoint records them."""
    from ..models.ratings import RatingConfirmation
    from ..services import story_ratings
    from ..services.registry import get_change_service, get_domain_review_service

    service = get_domain_review_service()
    with service._store.locked():  # noqa: SLF001 -- the same lock the endpoint takes
        review = service.get(story_id)
        current = story_ratings.view(get_change_service().get_for_customer(story_id, COMPANY_ID), review)
        for key, value in keys.items():
            if value:
                review.rating_confirmations.append(RatingConfirmation(
                    key=key, value=value, source_hash=getattr(current, key).source_hash, actor_id=person["id"],
                    actor=person["name"], at=datetime.fromtimestamp(time.time(), timezone.utc).isoformat()))
        service._save(review)  # noqa: SLF001


def _seed_request(spec: dict, people: dict[str, dict], now: datetime) -> None:
    from jde_mcp_server import approval, backlog

    from ..models.change_request import ChangeRequestSourceType
    from ..process import asbuilt
    from ..services.registry import (get_architecture_review_service, get_change_request_service,
                                     get_change_service, get_customer_link_service, get_decision_feedback_service,
                                     get_delivery_queue_service, get_domain_review_service,
                                     get_enhancement_run_service)

    sid, stage = spec["id"], spec["stage"]
    start = now - timedelta(days=spec["days_ago"], hours=3)
    span = max(spec.get("cycle") or spec["days_ago"] or 1, 1)
    # The steps are spread over the request's age (or, for delivered work,
    # its cycle time), in order; the last step happens no later than now.
    steps = ["intake", "analysed", "assigned", "do_start", "do_decided", "am_decided", "designed", "approved",
             "applied", "tested", "asbuilt", "final"]

    def at(step: str) -> datetime:
        fraction = steps.index(step) / (len(steps) - 1)
        moment = start + timedelta(hours=1) + (timedelta(days=span) - timedelta(hours=2)) * fraction
        _set_clock(min(moment, now - timedelta(minutes=5)))
        return min(moment, now)

    domain_key = spec["domain"]
    domain_id = DOMAINS[domain_key][0] if domain_key else None
    owner = people[domain_key] if domain_key else None
    am, tester = people["am"], people["test"]

    at("intake")
    get_change_request_service()._create(  # noqa: SLF001 -- the intake service, with a stable id
        customer_id=COMPANY_ID, source_type=ChangeRequestSourceType.DIRECT
        if spec["source"] == "Business" else ChangeRequestSourceType.TOPDESK,
        business_source=spec["source"], source_reference=spec["ref"], title=spec["title"], raw_content=spec["raw"],
        requester=spec["requester"], request_id=sid, received_at=datetime.fromtimestamp(time.time(), timezone.utc))
    if not _at_least(stage, "needs_input"):
        return

    # Receive -> Improve -> Check, as recorded by the orchestration driver.
    at("analysed")
    runs = get_enhancement_run_service()
    runs.start(sid)
    story = _user_story(spec["story"])
    proposed = stage != "needs_input"
    if proposed:
        backlog.propose_to_backlog(sid, story.statement, _impact(spec), "Low" if spec["route"] == "Functional Agent"
                                   else "Medium", source=spec["source"])
        get_customer_link_service().link(sid, COMPANY_ID)
    else:
        story.quality_status = "needs_human_input"
    runs.complete(sid, user_story=story, business_impact=_business_impact(spec),
                  rough_complexity_signal="Unknown" if not proposed else "Low",
                  check_outcome="proposed_to_backlog" if proposed else "needs_human_input",
                  failed_criteria=[] if proposed else ["Acceptance criteria cannot be made testable from the request"],
                  backlog_story_id=sid if proposed else None)
    if not proposed:
        return

    reviews = get_domain_review_service()
    reviews.ensure(sid, story)
    if not _at_least(stage, "do_review") or not domain_id:
        return
    at("assigned")
    reviews.assign_domain(sid, business_domain_id=domain_id, uncertain=False, note="")
    at("do_start")
    reviews.set_stage(sid, "domain_owner_reviewing")
    if stage == "do_review":
        return
    feedback = get_decision_feedback_service()
    at("do_decided")
    if stage == "rejected":
        reviews.record_domain_owner_rejection(sid, owner["name"], spec["reject_note"], identity_id=owner["id"])
        feedback.record(change_id=sid, customer_id=COMPANY_ID, kind="domain_owner_rejection", decided_by=owner["name"],
                        identity_id=owner["id"], note=spec["reject_note"])
        return
    _confirm_ratings(sid, owner, {"business_impact": spec["story"]["business_impact_rating"],
                                  "business_benefit": spec["story"]["business_benefit_rating"]})
    note = "Clear and complete; matches what the business needs."
    reviews.record_domain_owner_approval(sid, owner["name"], note, identity_id=owner["id"])
    feedback.record(change_id=sid, customer_id=COMPANY_ID, kind="domain_owner_approval", decided_by=owner["name"],
                    identity_id=owner["id"], note=note)
    if stage == "am_decision":
        return

    # Gate 1: the Application Manager authorises Jade to work on it.
    at("am_decided")
    note = "Authorised for delivery."
    reviews.record_application_manager_approval(sid, am["name"], note, identity_id=am["id"])
    backlog.approve(sid, am["name"], note)
    get_delivery_queue_service().add(sid, COMPANY_ID, am["name"], note)
    feedback.record(change_id=sid, customer_id=COMPANY_ID, kind="application_manager_approval", decided_by=am["name"],
                    identity_id=am["id"], note=note)
    if stage == "solutioning":
        return

    # The Architect's solution (and, for the Functional route, the exact change it proposed).
    when = at("designed")
    arch = get_architecture_review_service()
    arch.start(sid)
    decision, implementation = _decision(spec, when)
    arch.complete(sid, architect_decision=decision, implementation_spec=implementation, note="sample data")
    _confirm_ratings(sid, am, {"technical_impact": decision.technical_impact_rating})
    change_id = None
    if spec["route"] == "Functional Agent":
        op = {k: v for k, v in spec["change"].items() if not k.startswith("_")}
        change_id = approval.propose_change(sid, {**op, "story_id": sid}, "processing_option_update")["change_id"]
    if spec["route"] in ("Functional Agent", "Technical Agent"):
        _record_design(sid, domain_id, change_id)
    if change_id is None or not _at_least(stage, "approved"):
        return

    at("approved")
    _approve(change_id, am)
    if not _at_least(stage, "applied"):
        return
    at("applied")
    _apply(change_id, spec, am)
    if not _at_least(stage, "tested"):
        return
    at("tested")
    _test(change_id, spec, tester)
    if stage != "done":
        return

    at("asbuilt")
    change = get_change_service().get_for_customer(sid, COMPANY_ID)
    record = asbuilt.generate(COMPANY_ID, sid, change, actor=am["name"])
    at("final")
    change = get_change_service().get_for_customer(sid, COMPANY_ID)
    asbuilt.finalise(COMPANY_ID, sid, record["version"], change, actor=am["name"])


def _business_impact(spec: dict):
    from ..models.change import BusinessImpact

    return BusinessImpact(**_impact(spec))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def load(admin_email: str) -> list[tuple[str, str, str]]:
    """Creates the data set; returns (story, phase, health) for every request."""
    from ..persistence.db import connection, ensure_schema
    from ..services import auth_service
    from ..services.registry import get_change_service

    ensure_schema()
    admin = auth_service.get_user_by_email(admin_email)
    if admin is None:
        raise SystemExit(f"No user with e-mail {admin_email}. Sign in to Jade once, then run this again.")
    with connection() as conn:
        if conn.execute("SELECT 1 FROM companies WHERE id = ?", (COMPANY_ID,)).fetchone():
            raise SystemExit(f"Customer {COMPANY['name']} ({COMPANY_ID}) already exists; nothing was changed.")

    _create_customer(admin.id)
    people = _create_people(admin.id)
    _create_domains(people)
    now = datetime.now(timezone.utc)
    with simulated_clock():
        for spec in sorted(REQUESTS, key=lambda s: -s["days_ago"]):
            _seed_request(spec, people, now)

    service = get_change_service()
    out = []
    for spec in REQUESTS:
        change = service.get_for_customer(spec["id"], COMPANY_ID)
        lc = change.lifecycle if change else None
        out.append((spec["id"], lc.phase if lc else "missing", lc.health if lc else "missing"))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Load the Hoogwegt Arnhem sample customer into this installation.")
    parser.add_argument("--admin-email", required=True,
                        help="An existing user who gets every role on the new customer.")
    args = parser.parse_args(argv)
    for story_id, phase, health in load(args.admin_email.strip().lower()):
        print(f"{story_id:10} {phase:16} {health}")
    print(f"Loaded {COMPANY['name']} with {len(REQUESTS)} requests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
