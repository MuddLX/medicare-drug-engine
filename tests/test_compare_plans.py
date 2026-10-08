"""POST /compare-plans (2026-10-08): every plan for one client, priced - the Roundabout Plans page.
The page must never disagree with the PDFs, so most tests compare it to the report code itself."""
import os
import sqlite3

import pytest

import app.main as M
from app import compare_plans as CP
from app import data_meta as DM
from app.internal_pdf import pharmacy_summary

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB27 = os.path.join(ROOT, "medicare_mn_2027.db")
DB26 = os.path.join(ROOT, "medicare_mn.db")
needs27 = pytest.mark.skipif(not os.path.exists(DB27), reason="needs medicare_mn_2027.db")

SUSAN = [{"name": "Eliquis", "dosage": "5 mg"}, {"name": "Trelegy Ellipta", "dosage": "100/62.5/25 mcg"},
         {"name": "Atorvastatin", "dosage": "20 mg"}, {"name": "Lisinopril", "dosage": "10 mg"},
         {"name": "Metformin ER", "dosage": "500 mg"}]


@pytest.fixture
def engine(monkeypatch):
    def use(db):
        monkeypatch.setenv("COUNTY_ADDRESS_LOOKUP", "off")
        monkeypatch.setattr(DM, "DB_PATH", db)
        monkeypatch.setattr(M, "DB_PATH", db)
        monkeypatch.setattr(M, "geocode_address_live", lambda *a, **k: (45.0941, -93.3563, "Brooklyn Park"))
        monkeypatch.setattr(M, "normalize_drugs", lambda d: [
            {"original": x["name"], "normalized": x["name"], "dosage": x["dosage"], "confidence": 1.0, "flag": ""}
            for x in d])
        return M.app.test_client()
    return use


def _post(client, body):
    r = client.post("/compare-plans", json=body)
    return r.status_code, r.get_json()


@needs27
def test_every_plan_matches_what_the_report_shows_for_that_plan(engine, monkeypatch):
    # The dollar math is checked as it will run once official prices load (January); until then the
    # figures are held back (test_nothing_estimated_is_shown_before_official_prices).
    from app import availability as AV
    monkeypatch.setattr(AV, "prices_official", lambda *a, **k: True)
    monkeypatch.setattr(AV, "networks_official", lambda *a, **k: True)
    client = engine(DB27)
    status, j = _post(client, {"zip_code": "55443", "client_address": "8120 Zane Ave N",
                               "client_city": "Brooklyn Park", "drugs": SUSAN})
    assert status == 200 and j["plan_count"] > 10
    checked = 0
    for p in j["plans"]:
        if not p["priced"]:
            continue
        conn = sqlite3.connect(DB27)
        plans, _c, err = M.resolve_selected_plans(conn, "55443", [{"contract_id": p["contract_id"],
                                                                   "plan_id": p["plan_id"]}])
        conn.close()
        assert not err
        rep = M.compute_drug_costs(SUSAN, "55443", CP.cost_start(j["plan_year"]), "8120 Zane Ave N",
                                   "Brooklyn Park", "MN", plans_override=plans)
        label = plans[0]["carrier"]
        s = rep["plan_summaries"][label]
        assert p["est_drug_cost_year"] == pytest.approx(s["total_drug_cost"]), p["display_name"]
        assert p["est_total_year"] == pytest.approx(s["total_drug_plus_premium"]), p["display_name"]
        ph = pharmacy_summary(label, rep["drug_detail"], rep["months_remaining"])
        if ph is None:                     # no in-network pharmacy nearby in our data: both say so
            assert p["pharmacy"] is None
        else:
            assert p["pharmacy"]["name"] == ph["name"] and p["pharmacy"]["est_year"] == pytest.approx(ph["annual"])
        for cell, d in zip(p["drugs"], rep["drug_detail"]):
            rc = d["plans"][label]
            assert cell["tier"] == (rc.get("tier") if rc.get("covered") else None)
            assert cell["as_generic"] == bool(rc.get("as_generic"))
        checked += 1
    assert checked >= 10


@needs27
def test_lists_exactly_the_plans_the_picker_lists(engine):
    client = engine(DB27)
    for z in ("55443", "55025", "55009"):
        picker = client.get(f"/plans-for-zip?zip={z}").get_json()["plans"]
        _s, j = _post(client, {"zip_code": z, "drugs": SUSAN[:2]})
        assert [(p["contract_id"], p["plan_id"]) for p in j["plans"]] == \
               [(p["contract_id"], p["plan_id"]) for p in picker]
        assert all(p["in_county"] == q["in_county"] and p["counties"] == q["counties"]
                   for p, q in zip(j["plans"], picker))


@needs27
def test_plans_without_a_drug_list_are_never_priced_or_called_not_covered(engine):
    client = engine(DB27)
    _s, j = _post(client, {"zip_code": "55443", "drugs": SUSAN})
    missing = [p for p in j["plans"] if not p["drug_list_loaded"]]
    assert missing, "expected 2027 plans whose drug list isn't out yet"
    for p in missing:
        assert p["priced"] is False and p["est_drug_cost_year"] is None and p["est_total_year"] is None
        assert p["coverage"]["not_covered"] == 0 and p["coverage"]["list_not_out"] == len(SUSAN)
        assert {c["status"] for c in p["drugs"]} == {"list_not_out"}


@needs27
def test_restrictions_come_from_the_priced_package(engine):
    client = engine(DB27)
    _s, j = _post(client, {"zip_code": "55443", "drugs": SUSAN})
    cells = [c for p in j["plans"] for c in p["drugs"] if c["status"] == "covered"]
    assert cells and all(c["quantity_limit"] in (True, False) for c in cells)
    assert any(c["quantity_limit"] for c in cells)          # Trelegy has a quantity limit on some plans


def test_restrictions_unknown_on_a_database_without_them(engine):
    if not os.path.exists(DB26):
        pytest.skip("needs medicare_mn.db")
    client = engine(DB26)
    status, j = _post(client, {"zip_code": "55441", "drugs": SUSAN[2:4]})
    assert status == 200
    cells = [c for p in j["plans"] for c in p["drugs"] if c["status"] == "covered"]
    assert cells and all(c["prior_auth"] is None and c["quantity_limit"] is None for c in cells)


@needs27
def test_zip_only_lookup_lists_plans_unpriced(engine):
    client = engine(DB27)
    status, j = _post(client, {"zip_code": "55443"})
    assert status == 200 and j["plans"] and j["drugs"] == []
    for p in j["plans"]:
        assert p["priced"] is False and p["est_total_year"] is None
        assert p["premium_year"] == pytest.approx(round(p["premium_monthly"] * 12, 2))


@pytest.mark.parametrize("body, words", [
    ({"zip_code": "554"}, "5-digit ZIP"),
    ({}, "5-digit ZIP"),
    ({"zip_code": "55443", "drugs": "Eliquis"}, "must be a list"),
    ({"zip_code": "55443", "drugs": [{"name": f"Drug {i}"} for i in range(26)]}, "Up to 25"),
    ({"zip_code": "55443", "plan_year": "next"}, "plan_year"),
    ({"zip_code": "55443", "providers": "Dr. Smith"}, "providers must be a list"),
])
def test_bad_requests_get_a_plain_english_400(engine, body, words):
    client = engine(DB27 if os.path.exists(DB27) else DB26)
    status, j = _post(client, body)
    assert status == 400 and words in j["error"]


def test_drug_status_mirrors_the_internal_report_order():
    assert CP.drug_status({"error": "x"}, {"covered": True}) == "not_identified"
    assert CP.drug_status({}, {"drug_list_missing": True, "covered": None}) == "list_not_out"
    assert CP.drug_status({"is_injectable": True}, {"covered": True}) == "verify_injectable"
    assert CP.drug_status({}, {"covered": False}) == "not_covered"
    assert CP.drug_status({}, {}) == "not_covered"
    assert CP.drug_status({}, {"covered": True, "tier": 2}) == "covered"


def test_flat_drug_strings_and_cost_period():
    assert CP.parse_drugs({"drug_names": "Eliquis, Januvia,", "drug_dosages": "5 mg, 100 mg"}) == \
        [{"name": "Eliquis", "dosage": "5 mg"}, {"name": "Januvia", "dosage": "100 mg"}]
    assert CP.parse_drugs({"drugs": ["Lipitor", {"name": " ", "dosage": "1"}]}) == [{"name": "Lipitor", "dosage": ""}]
    from datetime import date
    assert CP.cost_start(2027, today=date(2026, 10, 8)) == "01/01/2027"
    assert CP.cost_start(2026, today=date(2026, 10, 8)) == "10/08/2026"


@needs27
def test_a_drug_with_no_price_on_file_is_blank_never_zero(engine):
    """2026-10-08 (Medica check): generic sitagliptin / rivaroxaban have no 2026 price yet. Their cell
    must have no yearly cost and no monthly series - not $0 - and the plan counts it as price_unknown."""
    client = engine(DB27)
    _s, j = _post(client, {"zip_code": "55443", "drugs": [{"name": "Januvia", "dosage": "100 mg"},
                                                          {"name": "Xarelto", "dosage": "20 mg"}]})
    unknown = [(p, c) for p in j["plans"] for c in p["drugs"] if c["price_unknown"]]
    if not unknown:
        pytest.skip("every drug has a price in the current database")
    for p, c in unknown:
        assert c["est_year"] is None and c["monthly"] == [], (p["display_name"], c["drug"])
        assert p["coverage"]["price_unknown"] >= 1


@needs27
def test_medica_2027_lists_loaded_and_priced(engine):
    """2026-10-08: Medica's 2027 lists (00027425 Medica Advantage, 00027424 Prime Solution Cost) are loaded."""
    client = engine(DB27)
    _s, j = _post(client, {"zip_code": "55443", "drugs": [{"name": "Atorvastatin", "dosage": "20 mg"},
                                                          {"name": "Eliquis", "dosage": "5 mg"}]})
    medica = [p for p in j["plans"] if p["contract_id"] == "H8889"]
    assert medica and all(p["drug_list_loaded"] and p["priced"] for p in medica)
    atorva = {p["plan_number"]: p["drugs"][0] for p in medica}
    assert all(c["status"] == "covered" and c["tier"] == 6 for c in atorva.values())   # PDF: tier 6
