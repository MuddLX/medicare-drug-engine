"""
client_benefits.py — reads pbp_benefits.db (CMS PBP benefit data) by plan and
formats each benefit into the client sheet's display fields.

Keyed by (contract_id, plan_id). lookup_benefits() returns None if the plan has no
benefit row (e.g. a standalone Part D plan). format_benefits() turns a raw row into
the renderer's display fields; the endpoint merges those into each selected plan.
"""
import os
import sqlite3

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pbp_benefits.db")

_FIELDS = ["oop_max_inn", "oop_max_comb", "medical_deductible", "part_b_giveback",
           "premium_pbp", "pcp_copay", "pcp_coins", "spec_copay", "spec_coins",
           "er_copay", "er_coins", "hosp_copay", "hosp_coins", "hosp_day_begin",
           "hosp_day_end", "dental_allowance", "vision_allowance", "hearing_allowance",
           "otc_amount", "fitness_included", "star_rating"]


def lookup_benefits(contract_id, plan_id, db_path=None):
    """Raw benefit row (dict) for a plan, or None if not present."""
    db_path = db_path or _DEFAULT_DB
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM plan_benefits WHERE contract_id=? AND plan_id=?",
        (str(contract_id).strip(), str(plan_id).strip().zfill(3))).fetchone()
    conn.close()
    return dict(row) if row else None


def _dollars(v):
    return f"${v:,.0f}" if v is not None else None


def _visit(copay, coins):
    """A covered visit: copay ($), coinsurance (%), or unknown (—)."""
    if copay is not None:
        return f"${copay:,.0f}"
    if coins is not None:
        return f"{coins:.0f}%"
    return "\u2014"


def _allowance(v):
    # Dollar allowance if present. If None, the plan may still cover the benefit via a
    # copay (common for hearing) - honest neutral until the "offered" flag refinement.
    return _dollars(v) if v is not None else "See plan"


def format_benefits(raw):
    """Raw benefit row -> the renderer's display fields (merged into each plan)."""
    if not raw:
        return {}

    moop = raw.get("oop_max_inn") or raw.get("oop_max_comb")   # in-network first

    hc, hco = raw.get("hosp_copay"), raw.get("hosp_coins")
    hb, he = raw.get("hosp_day_begin"), raw.get("hosp_day_end")
    if hc is not None and hb is not None and he is not None:
        hosp = {"amount": f"${hc:,.0f}", "note": f"days {hb:.0f}\u2013{he:.0f}"}
    elif hc is not None:
        hosp = {"amount": f"${hc:,.0f}", "note": "per stay"}
    elif hco is not None:
        hosp = {"amount": f"{hco:.0f}%", "note": None}
    else:
        hosp = {"amount": "\u2014", "note": None}

    gb = raw.get("part_b_giveback")
    med_ded = raw.get("medical_deductible")
    return {
        "oop_max": _dollars(moop) or "\u2014",
        "deductible": _dollars(med_ded) if med_ded is not None else "\u2014",
        "part_b_giveback": (f"+${gb:,.0f} / mo" if gb else None),   # None -> renderer shows "—"
        "pcp_visit": _visit(raw.get("pcp_copay"), raw.get("pcp_coins")),
        "specialist_visit": _visit(raw.get("spec_copay"), raw.get("spec_coins")),
        "hospital_per_day": hosp,
        "emergency_room": _visit(raw.get("er_copay"), raw.get("er_coins")),
        "dental": _allowance(raw.get("dental_allowance")),
        "vision": _allowance(raw.get("vision_allowance")),
        "hearing": _allowance(raw.get("hearing_allowance")),
        "otc": _allowance(raw.get("otc_amount")),
        "fitness": "Included" if raw.get("fitness_included") == 1 else "\u2014",
        "star_rating": raw.get("star_rating"),
    }
