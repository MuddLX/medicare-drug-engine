"""The yearly drug cost must follow Medicare.gov's own method (2026-10-17/18).

Before this, the engine's yearly totals ran $300-900 above Medicare.gov on most Medicare Advantage plans
with $0/$8 generic tiers. Medicare.gov's month-by-month view (tests/data/mgov_monthly_2027.json, captured
from Plan Finder) showed the method: drugs filled cheapest first; on plans with deductible-free tiers every
covered drug's full price counts toward the deductible, and each fill counts toward the $2,400 cap as the
larger of the plan's own copay/coinsurance and 25% of the price."""
import json
import os
import sqlite3
import pytest
from app import drug_year as DY
import app.main as M

DATA = json.load(open(os.path.join(os.path.dirname(__file__), "data", "mgov_monthly_2027.json")))["cases"]


@pytest.mark.parametrize("case", DATA, ids=[f"{c['plan']}-{c['pharmacy'][:12]}" for c in DATA])
def test_year_matches_medicare_gov_month_by_month(case):
    sims, info = DY.simulate_year_detail([d["terms"] for d in case["drugs"]], 12, case["deductible"],
                                         cap=2400.0, gross_rule=True)
    months = [round(sum(s[m] for s in sims), 2) for m in range(12)]
    mg = case["medicare_gov_months"]
    assert abs(sum(months) - sum(mg)) <= 1.50, (months, mg)     # whole year within $1.50
    for a, b in zip(months, mg):
        assert abs(a - b) <= 1.50, (months, mg)                  # and every month


def test_old_rule_was_far_off_on_these_plans():
    """Guard: the classic rule (only what the client pays counts) misses Medicare.gov by hundreds here."""
    case = next(c for c in DATA if c["plan"] == "H2001-116-0" and c["pharmacy"].startswith("CVS"))
    sims, _ = DY.simulate_year_detail([d["terms"] for d in case["drugs"]], 12, case["deductible"], cap=2400.0)
    assert sum(sum(s) for s in sims) - sum(case["medicare_gov_months"]) > 300


def test_copay_never_more_than_the_drug_price():
    t = {"ded_applies": False, "price": 1.40, "fee": 0, "cost_type": 1, "cost_amt": 10.0}
    assert DY.simulate_year([t], 3, 0.0, cap=2400.0)[0] == [1.40, 1.40, 1.40]


def test_copay_without_a_known_price_is_the_copay():
    t = {"ded_applies": False, "price": None, "fee": 0, "cost_type": 1, "cost_amt": 10.0}
    assert DY.simulate_year([t], 2, 0.0, cap=2400.0)[0] == [10.0, 10.0]


def test_cheapest_drug_fills_first_but_output_order_is_kept():
    a = {"ded_applies": True, "price": 300.0, "fee": 0, "cost_type": 2, "cost_amt": 0.25}
    b = {"ded_applies": True, "price": 50.0, "fee": 0, "cost_type": 2, "cost_amt": 0.25}
    out = DY.simulate_year([a, b], 1, 100.0, cap=2400.0)
    # b ($50) fills first and uses $50 of the deductible; a then pays $50 + 25% of $250
    assert out[1] == [50.0] and out[0] == [round(50 + 0.25 * 250, 2)]


def test_chain_of():
    assert M.chain_of("CVS Pharmacy #07110") == "cvs"
    assert M.chain_of("WALGREENS #11832") == "walgreens"
    assert M.chain_of("Hyvee Pharmacy #1") == "hy-vee"
    assert M.chain_of("Mail Order") == "mail order"


def _insulin_db(tmp_path, rows):
    db = tmp_path / "ins.db"
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE insulin_cost (contract_id TEXT, plan_id TEXT, segment_id TEXT, tier TEXT, days_supply INTEGER,
        copay_pref REAL, copay_nonpref REAL, copay_mail_pref REAL, copay_mail_nonpref REAL,
        coins_pref REAL, coins_nonpref REAL, coins_mail_pref REAL, coins_mail_nonpref REAL)""")
    c.executemany("INSERT INTO insulin_cost VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    return c


def test_insulin_uses_the_plans_own_rule(tmp_path):
    # H2001-118 style: all tiers ('.'), $35 copay OR 25% coinsurance -> the lower
    c = _insulin_db(tmp_path, [("H2001", "118", "000", ".", 1, None, 35.0, None, 35.0, None, 0.25, None, 0.25)])
    assert M.insulin_month_cost(c, "H2001", "118", 3, 94.69) == 23.67      # Medicare.gov showed $23.67
    assert M.insulin_month_cost(c, "H2001", "118", 3, 200.0) == 35.0
    assert M.insulin_month_cost(c, "H2001", "118", 3, None) == 35.0


def test_insulin_without_plan_row_is_35(tmp_path):
    c = _insulin_db(tmp_path, [])
    assert M.insulin_month_cost(c, "X", "1", 3, 500.0) == 35.0
    assert M.insulin_month_cost(c, "X", "1", 3, 20.0) == 20.0               # never more than the price
