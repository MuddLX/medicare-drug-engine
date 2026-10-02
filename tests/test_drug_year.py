"""Shared deductible + Part D out-of-pocket cap (2026-10-02 evening).

Bug found on the Harold Lindgren test: yearly drug cost $2,928.25 on a 2026-data run, above
Medicare's $2,100 out-of-pocket cap, because (1) every drug was charged its own full
deductible and (2) the cap was never applied. Mail order also skipped the deductible, which
produced fake "saves $1,305/yr" lines."""
import pytest
from app import drug_year as DY


def brand(price, rate=0.25, fee=0.0):
    return {"ded_applies": True, "price": price, "fee": fee, "cost_type": 2, "cost_amt": rate}


def test_cap_values_follow_the_data_year():
    assert DY.PART_D_OOP_CAP[2026] == 2100.0
    assert DY.PART_D_OOP_CAP[2027] == 2400.0
    assert DY.oop_cap() == DY.PART_D_OOP_CAP[DY.DATA_YEAR]


def test_one_deductible_shared_by_all_drugs():
    # three brand drugs at $300/month, $615 deductible, cap out of the way
    out = DY.simulate_year([brand(300), brand(300), brand(300)], 12, 615.0, cap=99999)
    jan = sum(d[0] for d in out)
    # Jan: drug1 300 (ded), drug2 300 (ded), drug3 15 ded + 285*25% = 86.25
    assert jan == pytest.approx(300 + 300 + 15 + 71.25)
    total_ded_paid = 615
    rest = 3 * 300 * 12 - 615
    assert sum(sum(d) for d in out) == pytest.approx(total_ded_paid + rest * 0.25)


def test_yearly_total_never_exceeds_the_cap_and_later_months_are_zero():
    out = DY.simulate_year([brand(500), brand(500), brand(500)], 12, 615.0, cap=2100.0)
    total = sum(sum(d) for d in out)
    assert total == pytest.approx(2100.0)
    assert all(d[-1] == 0 for d in out)


def test_flat_insulin_counts_toward_cap_without_deductible():
    out = DY.simulate_year([{"flat": 35.0}], 12, 615.0, cap=2100.0)
    assert out[0] == [35.0] * 12


def test_drug_without_deductible_pays_copay_from_month_one():
    t = {"ded_applies": False, "price": 20.0, "fee": 1.5, "cost_type": 1, "cost_amt": 2.0}
    out = DY.simulate_year([t], 3, 615.0, cap=2100.0)
    assert out[0] == [3.5, 3.5, 3.5]


def test_zero_copay_generic_adds_no_fee():
    t = {"ded_applies": False, "price": 4.0, "fee": 1.5, "cost_type": 0, "cost_amt": 0}
    assert DY.simulate_year([t], 2, 615.0, cap=2100.0)[0] == [0.0, 0.0]


def test_missing_terms_stay_none():
    out = DY.simulate_year([None, brand(100)], 2, 0.0, cap=2100.0)
    assert out[0] is None and out[1] == [25.0, 25.0]


def test_report_typical_month_and_cap_marker():
    from app.internal_pdf import pharmacy_summary, _typical_month
    months = ["January", "February", "March", "April"]
    detail = [{"plans": {"A": {"pharmacy_costs": [{"name": "Cub Pharmacy", "distance_miles": 1,
              "monthly_costs": [{"month": m, "cost": c} for m, c in zip(months, [1500, 400, 200, 0])],
              "annual_total": 2100}],
              "mail_order_costs": {"monthly_costs": [{"month": m, "cost": c} for m, c in zip(months, [1500, 300, 300, 0])],
                                   "annual_total": 2100}}}}]
    sm = pharmacy_summary("A", detail, months)
    assert sm["cap_from"] == "Apr"
    assert sm["steady"] > 0                      # never "$0.00/mo" because the cap was hit
    assert sm["mail_monthly"] == 300             # typical month, not the yearly average
    assert _typical_month([541, 190.75] + [135.25] * 10) == 135.25
