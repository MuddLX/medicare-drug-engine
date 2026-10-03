"""Lacey's feedback (2026-10-03): show WHEN the deductible is met and WHEN the yearly drug cap is
reached (cheapest pharmacy), and lead with yearly cost; monthly is the least important number."""
from app import drug_year as DY
from app.internal_pdf import pharmacy_summary

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def brand(price, rate=0.25, ded=True):
    return {"ded_applies": ded, "price": price, "fee": 0.0, "cost_type": 2, "cost_amt": rate}


def test_detail_reports_deductible_and_cap_months():
    out, info = DY.simulate_year_detail([brand(300), brand(300)], 12, 615.0, cap=2100.0)
    assert info["ded_met"] == 1                 # Jan pays 600 of 615, Feb finishes it
    assert info["cap_reached"] is not None and info["cap_reached"] > 1
    assert info["deductible"] == 615.0


def test_no_deductible_and_cap_not_reached():
    _, info = DY.simulate_year_detail([brand(20)], 12, 0.0, cap=2100.0)
    assert info["ded_met"] == "none" and info["cap_reached"] is None


def test_deductible_does_not_apply_to_these_drugs():
    _, info = DY.simulate_year_detail([brand(20, ded=False)], 12, 615.0, cap=2100.0)
    assert info["ded_met"] == "n/a"


def test_deductible_never_met():
    _, info = DY.simulate_year_detail([brand(10)], 12, 615.0, cap=2100.0)
    assert info["ded_met"] is None


def _detail(costs_by_drug):
    return [{"plans": {"A": {"pharmacy_costs": [{"name": "Cub Pharmacy", "distance_miles": 1,
             "monthly_costs": [{"month": m, "cost": c} for m, c in zip(MONTHS, costs)],
             "annual_total": sum(costs), "ded_met": "February", "cap_reached": "October"}]}}}
            for costs in costs_by_drug]


def test_summary_carries_months_and_yearly_first():
    sm = pharmacy_summary("A", _detail([[400, 300] + [150] * 8 + [50, 0]]), MONTHS)
    assert sm["ded_met"] == "February" and sm["cap_reached"] == "October"
    assert sm["annual"] == 400 + 300 + 1200 + 50


def test_apply_shared_year_stamps_every_cell():
    results = [{"plans": {"A": {"_terms": brand(300), "monthly_costs": [], "annual_total": 0,
                "pharmacy_costs": [{"name": "Cub", "address": "x", "_terms": brand(300)}],
                "mail_order_costs": {"_terms": brand(300)}}}}]
    DY.apply_shared_year(results, {"A": {"deductible": 615.0}}, MONTHS)
    cell = results[0]["plans"]["A"]
    assert cell["ded_met"] == "March" and cell["pharmacy_costs"][0]["ded_met"] == "March"
    assert cell["mail_order_costs"]["ded_met"] == "March"
    assert "cap_reached" in cell
