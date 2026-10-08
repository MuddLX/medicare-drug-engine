"""What the engine may show as FACT right now (2026-10-08, Jordon: "only show data backed by official
numbers; anything we don't have says it's not available yet").

Every report and the Plans page ask these questions, so all three always agree, and each section
switches itself back on when the official data is loaded - no code change:

    prices_official()    2027 drug prices loaded (CMS quarterly file, ~Jan 20 2027). Until then the
                         database only has 2026 prices carried forward as ESTIMATES -> no drug-cost
                         dollar figure anywhere (yearly/monthly costs, totals, deductible-met and cap
                         months, pharmacy and mail-order costs).
    networks_official()  2027 pharmacy networks loaded (CMS monthly file, Oct 15). Until then the
                         networks are 2026's -> no "nearby pharmacy" names.
    list_official(fid)   the plan's drug list is CMS's own 2027 list (source 'cms'), not our reading of the
                         carrier's PDF. Until then a drug missing from it is "not on the carrier's list -
                         verify", never "not covered".
    stars_official()     2027 star ratings loaded (pbp_benefits meta stars_year == data year).

What IS official now and always shown: plans, premiums, drug deductibles (CMS 2027 Landscape); each
plan's tier cost-sharing - copay or coinsurance %, preferred/standard pharmacy, deductible applies or not
(CMS 2027 plan benefit files); the $35/month insulin cap (federal law); benefits (CMS 2027 PBP).
"""
import os
import sqlite3

from app import data_meta

PRICES_NOTE = "2027 drug prices available January"
PRICES_NOTE_LONG = ("2027 drug prices aren't published yet (CMS releases them in January). Costs per fill "
                    "below are the plans' official 2027 copays and coinsurance.")
NETWORK_NOTE = "2027 pharmacy networks load Oct 15"
STARS_NOTE = "2027 ratings not out yet"
NOT_ON_LIST = "Not on carrier's list"
NOT_ON_LIST_DETAIL = "verify — official list Oct 15"

PBP_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pbp_benefits.db")


def prices_official(path=None):
    return not data_meta.prices_estimated(path)


def networks_official(path=None):
    return str(data_meta.meta(path).get("network_estimated", "0")) != "1"


def list_official(conn, formulary_id):
    """True when this drug list came from CMS (or the database predates carrier lists)."""
    if not formulary_id:
        return False
    cols = {r[1] for r in conn.execute("PRAGMA table_info(formulary)")}
    if "source" not in cols:                      # an older database: every list in it came from CMS
        return True
    row = conn.execute("SELECT source FROM formulary WHERE formulary_id = ? LIMIT 1", (formulary_id,)).fetchone()
    return bool(row) and (row[0] or "cms") != "carrier"


def stars_year(db_path=None):
    db_path = db_path or PBP_DB
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
            return None
        row = conn.execute("SELECT value FROM meta WHERE key = 'stars_year'").fetchone()
        return int(row[0]) if row and str(row[0]).isdigit() else None
    finally:
        conn.close()


def stars_official(db_path=None, path=None):
    return stars_year(db_path) == data_meta.data_year(path)


def _money(v):
    return f"${v:,.2f}".replace(".00", "")


def _share(ct, amt):
    if ct == 1:
        return _money(float(amt or 0)) + " copay"
    if ct == 2:
        pct = float(amt or 0) * 100
        return ("$0" if pct == 0 else f"{pct:g}% coinsurance")
    return None


def cost_share(conn, contract_id, plan_id, tier, insulin=False):
    """The plan's OFFICIAL 2027 cost-sharing for a tier, from the CMS plan benefit files - no prices needed.
    {"retail": "$47 copay", "retail_standard": "$52 copay" or None (only when it differs),
     "mail_90": "$94 copay" or None, "deductible_applies": bool, "insulin_cap": bool, "text": one line}."""
    if not tier:
        return None
    rows = {}
    for ds, *r in conn.execute(                    # initial coverage ('1') only - '3' is the $0 catastrophic phase
        """SELECT days_supply, cost_type_pref, cost_amt_pref, cost_type_nonpref, cost_amt_nonpref,
                  cost_type_mail_pref, cost_amt_mail_pref, ded_applies
           FROM beneficiary_cost WHERE contract_id = ? AND plan_id = ? AND tier = ? AND coverage_level = '1'
           ORDER BY segment_id""", (contract_id, str(plan_id).zfill(3), int(tier))):
        rows.setdefault(ds, r)
    one = rows.get(1)
    if not one:
        return None
    ctp, amp, cts, ams, _ctm, _amm, ded = one
    pref = _share(ctp, amp) or _share(cts, ams)
    std = _share(cts, ams) or pref
    three = rows.get(2)
    mail = _share(three[4], three[5]) if three else None
    out = {"retail": pref, "retail_standard": std if std != pref else None, "mail_90": mail,
           "deductible_applies": ded != "N", "insulin_cap": bool(insulin)}
    if insulin:
        out["text"] = "$35/month max (insulin)"
    else:
        bits = [pref + (" (preferred pharmacy)" if out["retail_standard"] else "")]
        if out["retail_standard"]:
            bits.append(f"{std} standard")
        bits.append("deductible applies" if out["deductible_applies"] else "no deductible")
        out["text"] = " · ".join(b for b in bits if b)
    return out
