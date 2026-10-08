"""Only official data is shown as fact (Jordon, 2026-10-08): no 2026-price estimates, no 2026 stars or
pharmacy networks, and no "Not covered" from our reading of a carrier PDF. What IS official - each plan's
2027 copay / coinsurance per tier (CMS plan benefit files), premiums, deductibles, the $35 insulin cap -
is shown. Each section switches back on by itself when its official data loads (app/availability.py)."""
import os
import sqlite3

import pytest

import app.main as M
from app import availability as AV
from app import client_comparison as CC
from app import data_meta as DM

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB27 = os.path.join(ROOT, "medicare_mn_2027.db")
needs27 = pytest.mark.skipif(not os.path.exists(DB27), reason="needs medicare_mn_2027.db")
DRUGS = [{"name": "Eliquis", "dosage": "5 mg"}, {"name": "Lantus SoloStar", "dosage": "100 units/ml"},
         {"name": "Januvia", "dosage": "100 mg"}, {"name": "Advair Diskus", "dosage": "250/50"}]


@pytest.fixture
def use27(monkeypatch):
    monkeypatch.setenv("COUNTY_ADDRESS_LOOKUP", "off")
    monkeypatch.setattr(DM, "DB_PATH", DB27)
    monkeypatch.setattr(M, "DB_PATH", DB27)
    monkeypatch.setattr(M, "geocode_address_live", lambda *a, **k: (45.0941, -93.3563, "Brooklyn Park"))
    monkeypatch.setattr(M, "normalize_drugs", lambda d: [
        {"original": x["name"], "normalized": x["name"], "dosage": x["dosage"], "confidence": 1.0, "flag": ""} for x in d])


@needs27
def test_cost_share_is_the_cms_2027_plan_file(use27):
    """Aetna Signature H3219-001, straight from CMS pbp_mrx_tier.txt (checked by hand 2026-10-08):
    Tier 1 $0 preferred / $5 standard, no deductible; Tier 3 13% coinsurance, deductible applies."""
    conn = sqlite3.connect(DB27)
    t1 = AV.cost_share(conn, "H3219", "001", 1)
    assert t1["retail"] == "$0 copay" and t1["retail_standard"] == "$5 copay" and t1["deductible_applies"] is False
    t3 = AV.cost_share(conn, "H3219", "001", 3)
    assert t3["retail"] == "13% coinsurance" and t3["deductible_applies"] is True
    assert AV.cost_share(conn, "H3219", "001", 3, insulin=True)["text"] == "$35/month max (insulin)"
    assert AV.cost_share(conn, "H3219", "001", None) is None


@needs27
def test_nothing_estimated_is_shown_before_official_prices(use27):
    """Today's 2027 database carries 2026 prices as estimates -> no drug-cost dollar figure anywhere."""
    assert not AV.prices_official(DB27) and not AV.networks_official(DB27)
    j = M.app.test_client().post("/compare-plans", json={"zip_code": "55443", "drugs": DRUGS}).get_json()
    av = j["availability"]
    assert av["prices"] is False and av["prices_note"] == AV.PRICES_NOTE
    assert av["networks"] is False and av["stars"] is False
    for p in j["plans"]:
        assert p["prices_available"] is False
        assert p["est_drug_cost_year"] is None and p["est_total_year"] is None
        assert p["deductible_met"] is None and p["cap_reached"] is None and p["pharmacy"] is None
        assert p["star_rating"] is None
        for c in p["drugs"]:
            assert c["est_year"] is None and c["monthly"] == [] and c["price_unknown"] is False
            if c["status"] == "covered":
                assert c["cost_share"] and c["cost_share"]["text"]
            else:
                assert c["cost_share"] is None


@needs27
def test_carrier_pdf_lists_never_say_not_covered(use27):
    j = M.app.test_client().post("/compare-plans", json={"zip_code": "55443", "drugs": DRUGS}).get_json()
    statuses = {c["status"] for p in j["plans"] for c in p["drugs"]}
    assert "not_covered" not in statuses and "not_on_list" in statuses
    assert all(p["drug_list_official"] is False for p in j["plans"] if p["drug_list_loaded"])
    assert all(p["coverage"]["not_covered"] == 0 for p in j["plans"])


@needs27
def test_official_list_brings_not_covered_back(use27, monkeypatch):
    monkeypatch.setattr(AV, "list_official", lambda *a, **k: True)
    j = M.app.test_client().post("/compare-plans", json={"zip_code": "55443", "drugs": DRUGS}).get_json()
    statuses = {c["status"] for p in j["plans"] for c in p["drugs"]}
    assert "not_on_list" not in statuses and "not_covered" in statuses


@needs27
def test_official_prices_bring_the_dollars_back(use27, monkeypatch):
    monkeypatch.setattr(AV, "prices_official", lambda *a, **k: True)
    monkeypatch.setattr(AV, "networks_official", lambda *a, **k: True)
    j = M.app.test_client().post("/compare-plans", json={"zip_code": "55443", "drugs": DRUGS[:1]}).get_json()
    priced = [p for p in j["plans"] if p["priced"]]
    assert priced and all(p["prices_available"] and p["est_total_year"] is not None for p in priced)


def test_stars_need_a_matching_stars_year(tmp_path):
    db = tmp_path / "pbp.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE plan_benefits (contract_id TEXT)")
    conn.commit()
    assert AV.stars_year(str(db)) is None                       # no record -> not shown
    conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('stars_year', '2026')")
    conn.commit()
    assert AV.stars_year(str(db)) == 2026
    conn.execute("UPDATE meta SET value='2027'")
    conn.commit()
    conn.close()
    assert AV.stars_year(str(db)) == 2027


@needs27
def test_client_sheet_payload_holds_back_estimates_and_carrier_list_gaps(use27):
    out = M.compute_drug_costs(DRUGS, "55443", "01/01/2027", plans_override=[
        {"contract_id": "H5959", "plan_id": "023", "carrier": "Blue Cross Essential", "type": "MA"}])
    sel = CC.agent_selection(out["plan_summaries"], ["Blue Cross Essential"])
    payload = CC.assemble_renderer_payload(sel, out["plan_summaries"], out["drug_detail"], {}, {}, 2027)
    plan = payload["plans"][0]
    assert plan["est_annual_drug_cost"] == "Available January 2027"
    assert plan["rx"]["not_covered"] == []           # Lantus / Advair: "confirm with your agent", never named
    assert plan["rx"]["unverified"] >= 1


def test_internal_report_holds_back_costs_and_still_fits_one_page(monkeypatch):
    from tests import test_internal_report_v2 as T
    monkeypatch.setattr(AV, "prices_official", lambda *a, **k: False)
    s = T.summaries(7, 3)
    pdf = T.render(s, T.drugs(list(s), 8), T.doctors(3))
    assert T.pages(pdf) == 1
    txt = T.text_of(pdf)
    assert "Available January" in txt and "$900" not in txt and "$1,000" not in txt
    assert "Costs are estimates" not in txt and "official 2027 amount per fill" in txt
