"""2026-11-08 (Jordon): the pharmacy section is a grid - every nearby store's price per plan, the cheapest marked,
mail order, then the deductible story (when it's met, the monthly cost until then and after) at the cheapest store."""
from app import internal_pdf as IP

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


def store(name, dist, monthly, preferred=False, ded_met=None):
    pc = {"name": name, "distance_miles": dist, "preferred": preferred,
          "annual_total": round(sum(monthly), 2),
          "monthly_costs": [{"month": m, "cost": c} for m, c in zip(MONTHS, monthly)]}
    if ded_met:
        pc["ded_met"] = ded_met
    return pc


def summary(label, stores, mail=None):
    cell = {"pharmacy_costs": stores}
    if mail is not None:
        cell["mail_order_costs"] = {"available": True,
                                    "monthly_costs": [{"month": m, "cost": c} for m, c in zip(MONTHS, mail)]}
    return IP.pharmacy_summary(label, [{"plans": {label: cell}}], MONTHS)


def test_every_store_gets_a_row_nearest_first_then_mail_and_the_deductible_story():
    a = summary("A", [store("Walgreens #3832", 3.4, [10] * 12), store("Cub Pharmacy #12", 2.9, [5] * 12)], mail=[4] * 12)
    rows, cells = IP.pharmacy_grid({"A": a}, {"A": 300})
    assert [k for k, _, _ in rows] == ["store:Cub", "store:Walgreens", "mail", "until", "after"]
    assert rows[0][1:] == ("Cub · 2.9 mi", "")


def test_each_store_shows_its_own_price_and_the_cheapest_is_marked():
    a = summary("A", [store("Cub Pharmacy #12", 2.9, [5] * 12), store("Walgreens #3832", 3.4, [10] * 12, preferred=True)])
    _, cells = IP.pharmacy_grid({"A": a}, {"A": 300})
    assert cells["A"]["store:Cub"] == ("$5.00/mo · $60/yr", "", True)
    assert cells["A"]["store:Walgreens"] == ("$10.00/mo · $120/yr", "preferred", False)


def test_same_price_everywhere_marks_no_store_cheapest():
    a = summary("A", [store("Cub Pharmacy #12", 2.9, [0] * 12), store("CVS #11070", 4.1, [0] * 12)])
    _, cells = IP.pharmacy_grid({"A": a}, {"A": 300})
    assert not any(cheap for key, (_, _, cheap) in cells["A"].items() if key.startswith("store:"))


def test_a_store_a_plan_does_not_have_is_a_dash_not_a_price():
    a = summary("A", [store("Cub Pharmacy #12", 2.9, [5] * 12), store("CVS #11070", 4.1, [7] * 12)])
    b = summary("B", [store("Cub Pharmacy #12", 2.9, [6] * 12)])
    rows, cells = IP.pharmacy_grid({"A": a, "B": b}, {"A": 300, "B": 300})
    assert "store:CVS" in [k for k, _, _ in rows]
    assert cells["B"]["store:CVS"] == ("—", "not nearby in network", False)


def test_a_farther_store_of_the_same_chain_says_its_own_distance():
    a = summary("A", [store("Walgreens #1", 1.2, [5] * 12)])
    b = summary("B", [store("Walgreens #2", 3.6, [5] * 12)])
    rows, cells = IP.pharmacy_grid({"A": a, "B": b}, {})
    assert ("store:Walgreens", "Walgreens · 1.2 mi", "") in rows
    assert "3.6 mi" in cells["B"]["store:Walgreens"][1] and "mi" not in cells["A"]["store:Walgreens"][1]


def test_a_plans_cheapest_store_is_never_cut_off():
    near = [store(f"Store{i} #1", i, [9] * 12) for i in range(1, 7)]           # six stores nearer than the cheapest
    a = summary("A", near + [store("Far Drug", 9.0, [1] * 12)])
    rows, cells = IP.pharmacy_grid({"A": a}, {})
    keys = [k for k, _, _ in rows]
    assert "store:Far Drug" in keys and cells["A"]["store:Far Drug"][2] is True


def test_mail_order_shows_monthly_yearly_and_the_difference():
    a = summary("A", [store("Cub Pharmacy #12", 2.9, [24.49] * 12)], mail=[14.83] * 12)
    _, cells = IP.pharmacy_grid({"A": a}, {"A": 700})
    assert cells["A"]["mail"] == ("$14.83/mo · $178/yr", "saves $116", True)


def test_deductible_met_shows_the_month_the_ramp_and_the_monthly_after():
    ramp = [284, 284, 170] + [67.93] * 9
    a = summary("A", [store("Cub Pharmacy #12", 1.5, ramp, ded_met="March")])
    _, cells = IP.pharmacy_grid({"A": a}, {"A": 615})
    assert cells["A"]["until"] == ("Jan–Feb $284/mo · Mar $170", "met in March", False)
    assert cells["A"]["after"][0] == "$67.93 a month"


def test_deductible_never_met_has_no_after_price():
    """Blue Cross on the 2026-11-08 test sheet: 'After the deductible $24.49' sat under 'deductible not met'."""
    a = summary("A", [store("Cub Pharmacy #12", 2.9, [24.49] * 12)])
    _, cells = IP.pharmacy_grid({"A": a}, {"A": 700})
    assert cells["A"]["until"] == ("$24.49 a month", "not met this year", False)
    assert cells["A"]["after"] == ("—", "", False)


def test_no_deductible_plan_says_so():
    a = summary("A", [store("Cub Pharmacy #12", 2.9, [10] * 12)])
    _, cells = IP.pharmacy_grid({"A": a}, {"A": 0})
    assert cells["A"]["until"][0] == "No deductible" and cells["A"]["after"][0] == "$10.00 a month"


def test_a_plan_with_no_prices_gets_no_cells():
    a = summary("A", [store("Cub Pharmacy #12", 2.9, [10] * 12)])
    rows, cells = IP.pharmacy_grid({"A": a, "B": None}, {})
    assert cells["B"] == {} and rows


def test_cap_row_only_when_a_plan_reaches_the_cap():
    a = summary("A", [store("Cub Pharmacy #12", 2.9, [10] * 12)])
    rows, _ = IP.pharmacy_grid({"A": a}, {"A": 300})
    assert "cap" not in [k for k, _, _ in rows]
    big = summary("B", [store("Cub Pharmacy #12", 2.9, [600] * 4 + [0] * 8)])
    big["cap_reached"] = "April"
    rows, cells = IP.pharmacy_grid({"A": a, "B": big}, {})
    assert "cap" in [k for k, _, _ in rows] and cells["B"]["cap"][0] == "Reached April"


def test_many_plans_show_the_monthly_price_alone_so_the_page_still_fits():
    plans = {f"P{i}": summary(f"P{i}", [store("Cub Pharmacy #12", 2.9, [5] * 12)]) for i in range(7)}
    _, cells = IP.pharmacy_grid(plans, {})
    assert cells["P0"]["store:Cub"][0] == "$5.00/mo"
