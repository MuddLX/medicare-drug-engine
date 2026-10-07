"""ZIPs that cross county lines (2026-10-06, Jordon). 228 Minnesota ZIPs touch counties with different
plans. The engine picks a county from the street address (Census lookup), else the city, else the
county with most of the ZIP's land - and says which. The plan picker lists every county's plans,
labelled, so an agent who knows the client's county can pick from it. Plans whose carrier has not
published a drug list yet still show, marked. Runs on the local 2027 database (skipped without it)."""
import os
import shutil
import sqlite3
import pytest
import app.main as M
from app import data_meta as DM

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "medicare_mn_2027.db")
pytestmark = pytest.mark.skipif(not os.path.exists(SRC), reason="needs medicare_mn_2027.db")


@pytest.fixture(scope="module")
def db27(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("db") / "m27.db")
    shutil.copy(SRC, p)
    c = sqlite3.connect(p)
    c.execute("DROP TABLE IF EXISTS place_county")
    c.execute("CREATE TABLE place_county (place TEXT, county_name TEXT)")
    c.executemany("INSERT INTO place_county VALUES (?,?)", [("cannon falls", "Goodhue"), ("hampton", "Dakota"),
                                                           ("forest lake", "Washington")])
    c.commit(); c.close()
    return p


@pytest.fixture
def use27(db27, monkeypatch):
    monkeypatch.setattr(DM, "DB_PATH", db27)
    monkeypatch.setattr(M, "DB_PATH", db27)
    monkeypatch.setattr(M, "county_from_address", lambda *a, **k: None)   # no internet in tests
    return db27


def test_single_county_zip_needs_no_note(use27):
    c = M.county_choice(M.get_db(), "55447")
    assert c["county"] == "Hennepin" and c["method"] == "only" and not c["note"]


def test_split_zip_without_address_uses_the_biggest_county_and_says_so(use27):
    c = M.county_choice(M.get_db(), "55009")
    assert c["county"] == "Goodhue" and c["method"] == "largest"
    assert "Dakota" in c["note"] and "Goodhue" in c["note"]
    assert [x["county"] for x in c["counties"]] == ["Goodhue", "Dakota"]


def test_city_settles_a_split_zip(use27):
    c = M.county_choice(M.get_db(), "55009", city="Hampton")
    assert c["county"] == "Dakota" and c["method"] == "city"
    assert M.county_choice(M.get_db(), "55009", city="Cannon Falls")["county"] == "Goodhue"


def test_city_spelling_variants(use27):
    assert M._place_key("St. Paul") == M._place_key("Saint Paul") == M._place_key("st paul")


def test_street_address_wins(use27, monkeypatch):
    monkeypatch.setattr(M, "county_from_address", lambda address, city, state, zip_code: "Dakota")
    c = M.county_choice(M.get_db(), "55009", address="1 Main St", city="Cannon Falls")
    assert c["county"] == "Dakota" and c["method"] == "address"


def test_address_lookup_switch_off(use27, monkeypatch):
    called = []
    monkeypatch.setattr(M, "county_from_address", lambda *a: called.append(1) or "Dakota")
    monkeypatch.setenv("COUNTY_ADDRESS_LOOKUP", "off")
    c = M.county_choice(M.get_db(), "55009", address="1 Main St", city="Cannon Falls")
    assert not called and c["method"] == "city"


def test_address_county_outside_the_zip_is_ignored(use27, monkeypatch):
    monkeypatch.setattr(M, "county_from_address", lambda *a: "Hennepin")
    assert M.county_choice(M.get_db(), "55009", address="1 Main St")["method"] == "largest"


def test_picker_lists_every_countys_plans_labelled(use27):
    r = M.app.test_client().get("/plans-for-zip?zip=55009")
    j = r.get_json()
    assert r.status_code == 200 and j["county"] == "Goodhue" and j["county_method"] == "largest"
    assert {x["county"] for x in j["counties"]} == {"Goodhue", "Dakota"}
    dakota_only = [p for p in j["plans"] if p["counties"] == ["Dakota"]]
    assert dakota_only and all(p["in_county"] is False for p in dakota_only)
    assert any(p["in_county"] for p in j["plans"])


def test_picker_uses_city_when_given(use27):
    j = M.app.test_client().get("/plans-for-zip?zip=55009&city=Hampton").get_json()
    assert j["county"] == "Dakota" and j["county_method"] == "city"


def test_agent_can_pick_a_plan_from_the_other_county(use27):
    conn = M.get_db()
    j = M.app.test_client().get("/plans-for-zip?zip=55009").get_json()
    pick = next(p for p in j["plans"] if p["counties"] == ["Dakota"] and p["plan_type"] == "MA")
    plans, county, err = M.resolve_selected_plans(conn, "55009", [{"contract_id": pick["contract_id"],
                                                                   "plan_id": pick["plan_id"]}])
    assert err is None and county == "Dakota"


def test_plans_without_a_drug_list_still_show_marked(use27):
    j = M.app.test_client().get("/plans-for-zip?zip=55447").get_json()
    medica = [p for p in j["plans"] if p["contract_id"] == "H8889"]
    assert medica and all(p["drug_list_loaded"] is False for p in medica)
    assert all(p["drug_list_loaded"] for p in j["plans"] if p["contract_id"] == "H3219")


def test_report_for_a_plan_without_a_drug_list_says_so(use27, monkeypatch):
    conn = M.get_db()
    rx = conn.execute("SELECT rxcui FROM formulary WHERE formulary_id='00027015' AND tier=1 LIMIT 1").fetchone()[0]
    monkeypatch.setattr(M, "normalize_drugs", lambda d: [{"original": "x", "normalized": "testgeneric",
                                                          "dosage": "10mg", "confidence": 1.0}])
    monkeypatch.setattr(M, "lookup_rxcuis", lambda n, d: [rx])
    plans = [{"contract_id": "H3219", "plan_id": "001", "carrier": "Aetna Signature", "type": "MA"},
             {"contract_id": "H8889", "plan_id": "022", "carrier": "Medica Essential ($20)", "type": "MA"}]
    out = M.compute_drug_costs([{"name": "testgeneric", "dosage": "10mg"}], "55447", "01/01/2027", plans_override=plans)
    cell = out["drug_detail"][0]["plans"]["Medica Essential ($20)"]
    assert cell["drug_list_missing"] is True and cell.get("covered") is not False
    s = out["plan_summaries"]["Medica Essential ($20)"]
    assert s["drug_list_missing"] is True and s["total_drug_cost"] is None
    assert out["plan_summaries"]["Aetna Signature"]["total_drug_cost"] is not None
    pdf = M.build_pdf("T", "01/01/1950", "55447", "01/01/2027", out["plan_summaries"], out["drug_detail"],
                      out["months_remaining"], plan_year=2027, county="Hennepin")
    assert pdf
    from app import client_comparison as CC
    sel = CC.agent_selection(out["plan_summaries"], ["Aetna Signature", "Medica Essential ($20)"])
    payload = CC.assemble_renderer_payload(sel, out["plan_summaries"], out["drug_detail"], {}, {}, 2027)
    medica = next(p for p in payload["plans"] if "Essential" in (p.get("plan_name") or ""))
    assert "drug list" in medica["est_annual_drug_cost"].lower()
    assert medica["rx"]["not_covered"] == []          # never "not covered" for a list we don't have
