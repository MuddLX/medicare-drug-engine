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


def data_year():
    """The CMS data year of the loaded database (its meta table), else DATA_YEAR."""
    from app import data_meta
    return data_meta.data_year() if data_meta.has_meta() else DATA_YEAR


def oop_cap(year=None):
    """Yearly out-of-pocket cap: the database's own value (Landscape file) when it has one,
    else this table for the data year."""
    if year is None:
        from app import data_meta
        override = data_meta.oop_cap_override()
        if override:
            return override
        year = data_year()
    return PART_D_OOP_CAP[year]


def _after_deductible(t, price):
    """What the client pays for `price` worth of drug once the deductible is met (no fee).
    A copay never exceeds the drug's price (2026-10-17: Medicare.gov charges the lower of the two -
    a $1.40 generic on a $10-copay tier costs $1.40)."""
    ct, amt = t.get("cost_type"), float(t.get("cost_amt") or 0)
    if ct == 0:
        return 0.0
    if ct == 2:
        return (price or 0) * amt
    if price:                        # 1 = flat copay, capped at the price when the price is known
        return min(amt, price)
    return amt


def _nominal_after(t, price):
    """The plan's own cost-share for a fill, BEFORE capping a copay at the price. Medicare.gov counts
    this toward the yearly cap on plans with deductible-free tiers (see simulate_year_detail)."""
    ct, amt = t.get("cost_type"), float(t.get("cost_amt") or 0)
    if ct == 0:
        return 0.0
    if ct == 2:
        return (price or 0) * amt
    return amt


def fill_cost(t, deductible_remaining):
    """One month of one drug. Returns (client cost, amount of deductible used)."""
    if t is None:
        return 0.0, 0.0
    if "flat" in t:                  # insulin: the plan's insulin cost, no deductible
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


def simulate_year(terms_list, n_months, deductible, cap=None, gross_rule=False):
    """terms_list: one entry per drug (None = no cost for that drug here).
    Returns one list of monthly costs per drug (None stays None)."""
    return simulate_year_detail(terms_list, n_months, deductible, cap, gross_rule)[0]


def simulate_year_detail(terms_list, n_months, deductible, cap=None, gross_rule=False):
    """Like simulate_year, plus WHEN things happen (Lacey, 2026-10-03):
      info["ded_met"]:     month index the deductible is fully paid; "none" = the plan has no
                           drug deductible; "n/a" = it doesn't apply to any of these drugs;
                           None = not met within the months shown.
      info["cap_reached"]: month index the yearly out-of-pocket cap is reached, or None.

    Medicare.gov's method (reverse-engineered 2026-10-17 from its month-by-month view; matches it to
    the penny on H2001-116 and H3219-001, see Plan jsons/_tools/verify_2026-10-17):
      * every month the drugs are filled cheapest first;
      * plans whose deductible applies to EVERY tier (gross_rule=False): only what the client pays
        counts toward the deductible and the cap (the classic rule);
      * plans with deductible-free tiers (gross_rule=True): every covered drug's full price counts
        toward the deductible, and toward the cap each fill counts the larger of the plan's own
        copay/coinsurance (before capping a copay at the price) and 25% of the drug's price;
        the deductible itself counts in full.
    """
    cap = oop_cap() if cap is None else cap
    ded = float(deductible or 0)
    start_ded = ded
    spent = 0.0                      # what counts toward the cap
    ded_met = "none" if start_ded <= 0 else None
    any_subject = False
    cap_reached = None
    out = [None if t is None else [] for t in terms_list]
    order = sorted((i for i, t in enumerate(terms_list) if t is not None),
                   key=lambda i: (terms_list[i].get("price") is None, terms_list[i].get("price") or 0))
    for m in range(n_months):
        month_cost = {}
        for i in order:
            t = terms_list[i]
            price = t.get("price")
            r = float(price) if price else 0.0
            flat = "flat" in t
            subj = bool(t.get("ded_applies")) and bool(price) and not flat
            if subj:
                any_subject = True
            in_ded = ded > 0.005
            if spent >= cap - 0.005:
                month_cost[i] = 0.0
                continue
            cost, used = fill_cost(t, ded)
            if gross_rule and flat:
                # Insulin (2026-10-18 blind test, matched Medicare.gov to the cent on HealthPartners and
                # Medica): only what the client pays counts - toward the deductible and toward the cap,
                # with no 25% floor and not its full price.
                ded = max(0.0, ded - cost) if in_ded else ded
                add = cost
            elif gross_rule:
                portion = min(r, ded) if in_ded else 0.0        # full price counts toward the deductible
                ded = max(0.0, ded - r) if in_ded else ded
                if in_ded and subj:
                    base = cost
                else:
                    base = _nominal_after(t, r) if price else cost
                    base = base + (float(t.get("fee") or 0) if base > 0 else 0)
                add = portion + max(base - min(base, portion), 0.25 * (r - portion))
            else:
                ded = max(0.0, ded - used)
                add = cost
            if in_ded and ded <= 0.005 and ded_met is None and start_ded > 0:
                ded_met = m
            room = cap - spent
            if add > room:
                cost = max(0.0, cost - (add - room))
                add = room
            spent += add
            if cap_reached is None and spent >= cap - 0.005:
                cap_reached = m
            month_cost[i] = round(cost, 2)
        for i in order:
            out[i].append(month_cost[i])
    if ded_met is None and not any_subject and not gross_rule:
        ded_met = "n/a"
    return out, {"ded_met": ded_met, "cap_reached": cap_reached, "deductible": start_ded,
                 "cap": cap, "spent": round(spent, 2)}


def _month_label(v, month_names):
    if isinstance(v, int):
        return month_names[v]
    return {"none": "No deductible", "n/a": "Doesn't apply"}.get(v)   # None stays None


def _redo(entries, deductible, month_names, gross_rule=False):
    entries = [e for e in entries if e and e.get("_terms", None) is not None]
    sims, info = simulate_year_detail([e["_terms"] for e in entries], len(month_names), deductible,
                                      gross_rule=gross_rule)
    ded_met = _month_label(info["ded_met"], month_names)
    cap_reached = _month_label(info["cap_reached"], month_names)
    for e, costs in zip(entries, sims):
        e["monthly_costs"] = [{"month": m, "cost": c} for m, c in zip(month_names, costs)]
        e["annual_total"] = round(sum(costs), 2)
        e["ded_met"] = ded_met              # month name, "No deductible", "Doesn't apply" or None
        e["cap_reached"] = cap_reached      # month name or None (not reached)


def apply_shared_year(results, plan_details, month_names):
    """Recompute every monthly cost in compute_drug_costs' results with one shared deductible
    and the out-of-pocket cap, separately for: plan level, each pharmacy, and mail order."""
    for carrier, plan in plan_details.items():
        ded = plan.get("deductible", 0)
        gr = bool(plan.get("gross_rule"))           # plan has deductible-free tiers (Medicare.gov method)
        cells = [dr.get("plans", {}).get(carrier) or {} for dr in results]
        _redo(cells, ded, month_names, gr)                               # plan level
        groups = {}
        for c in cells:
            for pc in c.get("pharmacy_costs", []) or []:
                groups.setdefault((pc.get("name"), pc.get("address")), []).append(pc)
        for entries in groups.values():                                  # each pharmacy
            _redo(entries, ded, month_names, gr)
        _redo([c.get("mail_order_costs") for c in cells], ded, month_names, gr)  # mail order
    for dr in results:                                                   # internal keys out
        for c in dr.get("plans", {}).values():
            c.pop("_terms", None)
            (c.get("mail_order_costs") or {}).pop("_terms", None)
            for pc in c.get("pharmacy_costs", []) or []:
                pc.pop("_terms", None)
