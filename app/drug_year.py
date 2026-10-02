"""A client's drug costs over the year, all drugs together (added 2026-10-02 evening).

Why this exists: costs used to be worked out one drug at a time, so every drug paid its own
full deductible and nothing ever stopped at Medicare's yearly out-of-pocket cap. Harold
Lindgren's test came out at $2,928.25 for a year when the legal maximum was $2,100.

How it works now: each drug (at one pharmacy, or by mail, or at plan level) is described by its
"terms" (price, copay or coinsurance, whether the deductible applies, dispensing fee). Then the
year is walked month by month, all drugs together:
  * ONE deductible is shared by every drug on the plan;
  * once what the client has paid this year reaches the cap, everything after costs $0.

DATA_YEAR is the year of the CMS data loaded in medicare_mn.db, NOT the plan year printed on
the report. Change it to 2027 when the 2027 CMS files are loaded; the cap follows.
"""

PART_D_OOP_CAP = {2025: 2000.0, 2026: 2100.0, 2027: 2400.0}
DATA_YEAR = 2026


def oop_cap(year=None):
    return PART_D_OOP_CAP[year or DATA_YEAR]


def _after_deductible(t, price):
    """What the client pays for `price` worth of drug once the deductible is met (no fee)."""
    ct, amt = t.get("cost_type"), float(t.get("cost_amt") or 0)
    if ct == 0:
        return 0.0
    if ct == 2:
        return (price or 0) * amt
    return amt                       # 1 = flat copay (and any other type, as before)


def fill_cost(t, deductible_remaining):
    """One month of one drug. Returns (client cost, amount of deductible used)."""
    if t is None:
        return 0.0, 0.0
    if "flat" in t:                  # insulin $35 cap: no deductible
        return float(t["flat"]), 0.0
    price = t.get("price")
    fee = float(t.get("fee") or 0)
    if not t.get("ded_applies") or deductible_remaining <= 0 or not price:
        cost = _after_deductible(t, price)
        return (cost + fee if cost > 0 else 0.0), 0.0
    if price >= deductible_remaining:
        rest = price - deductible_remaining
        return deductible_remaining + fee + _after_deductible(t, rest), deductible_remaining
    return price + fee, price


def simulate_year(terms_list, n_months, deductible, cap=None):
    """terms_list: one entry per drug (None = no cost for that drug here).
    Returns one list of monthly costs per drug (None stays None)."""
    cap = oop_cap() if cap is None else cap
    ded = float(deductible or 0)
    spent = 0.0
    out = [None if t is None else [] for t in terms_list]
    for _ in range(n_months):
        for i, t in enumerate(terms_list):
            if t is None:
                continue
            cost, used = fill_cost(t, ded)
            ded = max(0.0, ded - used)
            cost = max(0.0, min(cost, cap - spent))
            spent += cost
            out[i].append(round(cost, 2))
    return out


def _redo(entries, deductible, month_names):
    entries = [e for e in entries if e and e.get("_terms", None) is not None]
    sims = simulate_year([e["_terms"] for e in entries], len(month_names), deductible)
    for e, costs in zip(entries, sims):
        e["monthly_costs"] = [{"month": m, "cost": c} for m, c in zip(month_names, costs)]
        e["annual_total"] = round(sum(costs), 2)


def apply_shared_year(results, plan_details, month_names):
    """Recompute every monthly cost in compute_drug_costs' results with one shared deductible
    and the out-of-pocket cap, separately for: plan level, each pharmacy, and mail order."""
    for carrier, plan in plan_details.items():
        ded = plan.get("deductible", 0)
        cells = [dr.get("plans", {}).get(carrier) or {} for dr in results]
        _redo(cells, ded, month_names)                                   # plan level
        groups = {}
        for c in cells:
            for pc in c.get("pharmacy_costs", []) or []:
                groups.setdefault((pc.get("name"), pc.get("address")), []).append(pc)
        for entries in groups.values():                                  # each pharmacy
            _redo(entries, ded, month_names)
        _redo([c.get("mail_order_costs") for c in cells], ded, month_names)  # mail order
    for dr in results:                                                   # internal keys out
        for c in dr.get("plans", {}).values():
            c.pop("_terms", None)
            (c.get("mail_order_costs") or {}).pop("_terms", None)
            for pc in c.get("pharmacy_costs", []) or []:
                pc.pop("_terms", None)
