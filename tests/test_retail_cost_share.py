"""CMS cost_type 0 means "this plan doesn't use this column", not "$0" (2026-10-07, Margaret Ellison test).
Reading the unused preferred-pharmacy column priced brand drugs at $0 after the deductible."""
import os
import sqlite3

import pytest

from app import main
from app.drug_year import simulate_year

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "medicare_mn.db")


def test_preferred_column_used_when_plan_has_it():
    assert main.retail_cost_share(1, 10.0, 1, 20.0, preferred=True) == (1, 10.0)
    assert main.retail_cost_share(1, 10.0, 1, 20.0, preferred=False) == (1, 20.0)


def test_not_applicable_column_falls_back_to_the_other():
    assert main.retail_cost_share(0, 0.0, 2, 0.15, preferred=True) == (2, 0.15)
    assert main.retail_cost_share(2, 0.22, 0, 0.0, preferred=False) == (2, 0.22)


def test_real_zero_copay_is_kept():
    assert main.retail_cost_share(1, 0.0, 1, 5.0, preferred=True) == (1, 0.0)


def test_neither_column_used():
    assert main.retail_cost_share(0, 0.0, 0, 0.0) == (0, 0.0)


@pytest.mark.skipif(not os.path.exists(DB), reason="live plan database not present")
def test_brand_drug_costs_something_after_deductible_on_plan_without_preferred_pharmacies():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""SELECT cost_type_pref, cost_type_nonpref FROM beneficiary_cost
        WHERE contract_id='H2001' AND plan_id='116' AND tier=3 AND days_supply=1 AND coverage_level='1'""").fetchone()
    if row is None or row["cost_type_pref"] != 0 or row["cost_type_nonpref"] != 2:
        pytest.skip("database no longer has the 'preferred not applicable' shape for this plan")
    fid = conn.execute("SELECT formulary_id FROM plans WHERE contract_id='H2001' AND plan_id='116'").fetchone()[0]
    res = main.get_drug_cost_for_plan(conn, fid, "H2001", "116", ["1364447", "1364445"], 600.0,
                                      list(range(1, 13)), drug_name="Eliquis")
    conn.close()
    assert res["tier"] == 3
    months = simulate_year([res["_terms"]], 12, 600.0)[0]
    assert months[-1] > 0, "after the deductible a 15% coinsurance drug must not be $0"
    assert sum(months) > 600.0


@pytest.mark.skipif(not os.path.exists(DB), reason="live plan database not present")
def test_pharmacy_terms_fall_back_too():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    terms = main.pharmacy_fill_terms(conn, "H2001", "116", 3, 500.0, {"preferred": True}, drug_name="Somebrand")
    conn.close()
    assert terms["cost_type"] in (1, 2) and terms["cost_amt"] > 0
