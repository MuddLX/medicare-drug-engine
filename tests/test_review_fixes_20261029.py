"""2026-10-29 test-sheet review: combination strengths, mcg doses, deductible months, one yearly cost."""
from app import drug_resolver as R
from app import internal_pdf as IP
from app import main as M


def test_strength_only_made_as_a_combination_reads_as_the_combination():
    r = R.resolve("Triamterene", "37.5 mg")
    assert r.strength_matched and r.combo == "Triamterene / HCTZ" and r.combo_dose == "37.5-25 mg"
    assert "310812" in r.rxcuis                       # triamterene 37.5 / HCTZ 25 tablet
    assert "combination" in r.flag


def test_misspelled_combination_is_still_found_and_flagged():
    r = R.resolve("Tridamterene", "37.5 mg")
    assert r.combo == "Triamterene / HCTZ" and r.flag == "check spelling"


def test_real_single_strength_is_untouched():
    r = R.resolve("Triamterene", "50 mg")
    assert r.strength_matched and not r.combo and not r.flag


def test_no_combination_guess_when_none_fits_or_for_salts():
    assert not R.resolve("Atorvastatin", "250 mg").combo          # no such strength anywhere: stays "not found"
    assert not R.resolve("Atorvastatin", "250 mg").strength_matched
    r = R.resolve("Micro-K", "10 mEq", alternates=["potassium chloride (MICRO-K) 10 mEq", "potassium chloride"])
    assert not r.combo and "312504" in r.rxcuis


def test_microgram_drugs_show_mcg():
    assert IP.display_dose("Levothyroxine", "0.075 mg") == "75 mcg"
    assert IP.display_dose("Synthroid", ".1 mg") == "100 mcg"
    assert IP.display_dose("Liothyronine", "0.005 mg") == "5 mcg"
    assert IP.display_dose("Alprazolam", "0.5 mg") == "0.5 mg"          # written in mg by everyone
    assert IP.display_dose("Levothyroxine", "75 mcg") == "75 mcg"


def test_deductible_months_show_their_span():
    months = ["January", "February", "March", "April", "May"]
    detail = [{"plans": {"P": {"pharmacy_costs": [{"name": "Cub Pharmacy", "distance_miles": 1.5, "annual_total": 0,
                                                    "monthly_costs": [{"month": m, "cost": c} for m, c in
                                                                      zip(months, [284, 284, 170, 67.93, 67.93])]}]}}}]
    sm = IP.pharmacy_summary("P", detail, months)
    assert IP.cost_rows(sm)["first"][0] == "Jan–Feb $284/mo · Mar $170"


def test_yearly_cost_is_the_cheapest_pharmacy_with_every_drug():
    def pc(name, total, dist):
        return {"name": name, "annual_total": total, "distance_miles": dist}
    results = [{"plans": {"P": {"annual_total": 500, "pharmacy_costs": [pc("Cub", 480, 1.5), pc("Walgreens", 450, 2)]}}},
               {"plans": {"P": {"annual_total": 100, "pharmacy_costs": [pc("Cub", 100, 1.5)]}}}]   # Walgreens lacks one
    assert M.cheapest_pharmacy_year(results, "P") == 580
    assert M.cheapest_pharmacy_year([{"plans": {"P": {"annual_total": 5, "pharmacy_costs": []}}}], "P") is None


def test_cap_month_matches_medicare_gov_month_by_month():
    """2026-10-29: plans with deductible-free tiers reach the $2,400 cap the way Medicare.gov does (each fill counts
    the larger of what the client pays and the standard $700-deductible benefit). Cases are Medicare.gov's own
    month-by-month tables from saved searches (tests/data/mgov_cap_cases.json)."""
    import json, os
    from app import drug_year as DY
    cases = json.load(open(os.path.join(os.path.dirname(__file__), "data", "mgov_cap_cases.json")))
    assert len(cases) >= 3
    for c in cases:
        out, info = DY.simulate_year_detail(c["terms"], 12, c["deductible"], cap=2400.0, gross_rule=True)
        months = [sum(x[m] for x in out if x) for m in range(12)]
        # within $15 a year (the old rule was ~$190 high on H3219-002); a small January (deductible-month) difference
        # remains on some Blue Cross / HealthPartners searches and is tracked by tools/verify_against_mgov.py
        assert abs(sum(months) - sum(c["mgov_monthly"])) <= 15, (c["plan"], round(sum(months), 2), sum(c["mgov_monthly"]))
        assert info["cap_reached"] == next(i for i, v in enumerate(c["mgov_monthly"]) if v == 0) - (
            1 if c["mgov_monthly"][next(i for i, v in enumerate(c["mgov_monthly"]) if v == 0) - 1] > 0 else 0)
