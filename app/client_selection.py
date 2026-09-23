"""
client_selection.py — deterministic plan-selection brain for the client-facing
Medicare comparison (Phase 3, Part 5). Pure functions: FILTER -> RANK -> CAP.

Compliance properties (deliberate, not incidental):
- Commission is used ONLY as an eligibility gate ("appointed carrier -> eligible"),
  NEVER as a ranking tiebreaker. Ranking is purely client-benefit.
- Selection is fully deterministic: identical inputs -> identical plans, and there
  is always a one-sentence answer to "why these plans?".

Network fit is a PLUGGABLE component — the seam for Jill's provider-systems chart,
which does not exist yet. Today we use NeutralNetworkFit (network unknown -> ranking
falls back to pure cost). When Jill's chart lands, swap in ChartNetworkFit; nothing
else changes.
"""

from dataclasses import dataclass, field

# ----- network-fit verdict levels, ranked best -> worst -----
FIT_ALL, FIT_SOME, FIT_NONE, FIT_UNKNOWN = "ALL", "SOME", "NONE", "UNKNOWN"
_GROUP_RANK = {FIT_ALL: 0, FIT_UNKNOWN: 0, FIT_SOME: 1, FIT_NONE: 2}


@dataclass
class Verdict:
    level: str
    kept: list = field(default_factory=list)
    missing: list = field(default_factory=list)


# ===== pluggable network-fit components (the seam for Jill's chart) =====
class NeutralNetworkFit:
    """TODAY: no provider-systems chart exists, so network fit is unknown for
    every plan. Ranking therefore falls back to pure estimated yearly cost."""

    label = "no provider chart yet — network unknown"

    def evaluate(self, plan, client):
        return Verdict(FIT_UNKNOWN)


class ChartNetworkFit:
    """FUTURE: backed by Jill's provider-systems chart (health system -> carriers).
    This reference version reads plan['health_systems'] against the client's
    systems. When the real chart arrives it becomes the lookup source; the
    interface below stays identical."""

    label = "provider chart active — doctors-first"

    def evaluate(self, plan, client):
        client_systems = set(client.get("systems", []))
        if not client_systems:
            return Verdict(FIT_UNKNOWN)
        plan_systems = set(plan.get("health_systems", []))
        kept = sorted(client_systems & plan_systems)
        missing = sorted(client_systems - plan_systems)
        if not missing:
            return Verdict(FIT_ALL, kept, [])
        if kept:
            return Verdict(FIT_SOME, kept, missing)
        return Verdict(FIT_NONE, [], missing)


# ===== estimated total annual cost (Part 5 formula) =====
def estimated_annual_cost(plan):
    prem     = plan.get("monthly_premium", 0) * 12
    pdp      = plan.get("part_d_premium_separate", 0) * 12   # 0 when Rx is included
    drugs    = plan.get("est_annual_drug_cost", 0)           # engine already computes this
    giveback = plan.get("part_b_giveback_monthly", 0) * 12
    return prem + pdp + drugs - giveback


# ===== stage 1: FILTER (eligibility) =====
def filter_eligible(candidates, appointed_carriers, county):
    appointed = {c.lower() for c in appointed_carriers}
    eligible, dropped = [], []
    for p in candidates:
        if p["carrier"].lower() not in appointed:
            dropped.append((p, "carrier not appointed"))
            continue
        counties = p.get("counties")
        if counties is not None and county not in counties and "All Counties" not in counties:
            dropped.append((p, f"not offered in {county} County"))
            continue
        eligible.append(p)
    return eligible, dropped


# ===== stage 2: RANK (client-benefit) =====
def rank_plans(eligible, client, network_fit, mode="doctors_first"):
    scored = [(p, network_fit.evaluate(p, client), estimated_annual_cost(p)) for p in eligible]
    if mode == "cost_first":                 # the "dial": cost first, doctors as tiebreak
        key = lambda t: (t[2], _GROUP_RANK[t[1].level])
    else:                                    # default: doctors first, then cost
        key = lambda t: (_GROUP_RANK[t[1].level], t[2])
    return sorted(scored, key=key)


# ===== stage 3: CAP =====
def cap_plans(ranked, max_plans=3):
    # Part 6 also adds exactly ONE Medicare Supplement here — reserved slot,
    # deferred until MN Medigap premium data is loaded (~mid-October).
    return ranked[:max_plans]


# ===== orchestrator =====
def select_plans(candidates, config, client, network_fit=None):
    network_fit = network_fit or NeutralNetworkFit()
    mode = config.get("rank_mode", "doctors_first")
    eligible, dropped = filter_eligible(candidates, config["appointed_carriers"], client["county"])
    ranked = rank_plans(eligible, client, network_fit, mode)
    selected = cap_plans(ranked, config.get("max_plans", 3))
    return {"selected": selected, "ranked": ranked, "dropped": dropped, "mode": mode}
