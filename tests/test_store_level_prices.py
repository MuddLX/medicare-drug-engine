"""Small-town pharmacies are priced store by store (2026-10-19).

Medicare.gov's price was confirmed to be the same at every store only for the big metro chains (Cub, Walgreens,
CVS, Walmart, Costco, Hy-Vee). A small-town capture (Coborn's, an independent like Lewis Family Drug) is loaded
for THAT store only, matched to its NPI by street address, and shown only at that store. A store with no captured
price is never shown, and never takes one of the nearest-store slots."""
import importlib.util
import os
import shutil
import sqlite3

import pytest

import app.main as M
from app import data_meta as DM

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(HERE, "medicare_mn_2027.db")
pytestmark = pytest.mark.skipif(not os.path.exists(DB), reason="needs medicare_mn_2027.db")

PLAN = ("H2450", "016")          # Medica Prime Solution Basic w/Rx (Cost) - sold in Pipestone County
LABEL = "Atorvastatin 40mg tablet"


def _build():
    spec = importlib.util.spec_from_file_location("build_2027_db_t", os.path.join(HERE, "build_2027_db.py"))
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    return b


def test_street_key_and_store_match():
    b = _build()
    assert b._street_key("1106 8th St Sw") == b._street_key("1106 8TH STREET SW STE 2") == "1106 8TH ST SW"
    conn = sqlite3.connect(DB)
    # Coborn's at its own address; the LTC pharmacy that shares A&S's address is not in this plan's network
    assert b.store_npis(conn, *PLAN, "Coborn's Pharmacy #2034", "1106 8th St Sw, Pipestone, MN 56164") == ["1982968640"]
    assert b.store_npis(conn, *PLAN, "A And S Drugs - Ltc", "101 W Main St, Pipestone, MN 56164") == []
    # a person's NPI at the same address as Walgreens loses to the name match
    assert b.store_npis(conn, "H2001", "119", "Walgreens #04567", "421 Paul Bunyan Dr Nw, Bemidji, MN 56601") == ["1740295971"]
    assert b.store_npis(conn, *PLAN, "Somewhere", "no address") == []


@pytest.fixture()
def small_town_db(tmp_path, monkeypatch):
    """Engine DB copy rebuilt from a tiny capture file: two Pipestone stores for one drug on H2450-016,
    plus one store whose address matches no network pharmacy."""
    db = tmp_path / "eng.db"
    shutil.copyfile(DB, db)
    src = tmp_path / "mpf.db"
    s = sqlite3.connect(src)
    s.execute("""CREATE TABLE drug_prices (plan_id TEXT, batch TEXT, drug TEXT, quantity TEXT, frequency TEXT,
        chain TEXT, store TEXT, in_network INT, covered INT, retail REAL, deductible_phase REAL, initial_phase REAL,
        catastrophic REAL, captured_at TEXT, source_file TEXT, address TEXT)""")
    rows = [("coborn", "Coborn's Pharmacy #2034", "1106 8th St Sw, Pipestone, MN 56164", 9.99),
            ("lewis family drug", "Lewis Family Drug", "712 8th Ave Se, Pipestone, MN 56164", 12.34),
            ("nowhere drug", "Nowhere Drug", "1 Fake St, Pipestone, MN 56164", 1.00)]
    for chain, store, addr, price in rows:
        s.execute("INSERT INTO drug_prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("H2450-016-0", "FILL1", LABEL, "30", "", chain, store, 1, 1, price, price, price, 0,
                   "2026-10-19T12:00:00", "x.json", addr))
    s.commit(); s.close()
    b = _build()
    conn = sqlite3.connect(db)
    b.step_mpf_prices(conn, str(src))
    conn.close()
    monkeypatch.setattr(DM, "DB_PATH", str(db))
    monkeypatch.setattr(M, "DB_PATH", str(db))
    monkeypatch.setattr(M, "geocode_address_live", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(M, "normalize_drugs", lambda d: [
        {"original": x["name"], "normalized": x["name"], "dosage": x["dosage"], "confidence": 1.0} for x in d])
    return str(db)


def test_build_keys_small_town_stores_by_npi(small_town_db, capsys):
    conn = sqlite3.connect(small_town_db)
    keys = dict(conn.execute("SELECT chain, retail FROM mpf_price WHERE contract_id=? AND plan_id=?", PLAN))
    assert keys == {"npi:1982968640": 9.99, "npi:1265185524": 12.34}     # never "coborn" chain-wide
    # plan-level price for a plan with no metro chain captured: the stores' common price (tie -> lower)
    assert {r[0] for r in conn.execute("SELECT unit_cost FROM pricing WHERE contract_id=? AND plan_id=?", PLAN)} == {9.99}


def test_engine_shows_each_store_its_own_price(small_town_db):
    plans = [{"contract_id": PLAN[0], "plan_id": PLAN[1], "carrier": "Medica Cost", "type": "MA"}]
    out = M.compute_drug_costs([{"name": "atorvastatin", "dosage": "40 mg"}], "56164", "01/01/2027", plans_override=plans)
    cell = out["drug_detail"][0]["plans"]["Medica Cost"]
    assert cell["covered"] and not cell.get("price_unknown")
    stores = {p["name"]: p for p in cell["pharmacy_costs"]}
    names = " | ".join(stores).lower()
    assert "coborn" in names and "lewis" in names
    assert "shopko" not in names                       # in network nearby, but nothing captured there
    # each store is priced from its own capture ($0 copay tier here, so check the price itself)
    conn = sqlite3.connect(small_town_db)
    for key, price in (("npi:1982968640", 9.99), ("npi:1265185524", 12.34)):
        t = M.pharmacy_fill_terms(conn, *PLAN, cell["tier"], 9.99, {"name": "x", "price_key": key}, LABEL, ndc=cell["ndc"])
        assert t["price"] == price


def test_chain_name_alone_never_borrows_a_small_town_price(small_town_db):
    """Coborn's in another town has no capture of its own: it must not get Pipestone's price."""
    have = M.mpf_chains_for_plan(sqlite3.connect(small_town_db), *PLAN)
    assert M.mpf_price_key({"name": "Coborn's Pharmacy", "npi": "9999999999"}, have) is None
    assert M.mpf_price_key({"name": "Coborn's Pharmacy", "npi": "1982968640"}, have) == "npi:1982968640"

def test_one_amount_per_drug(tmp_path):
    """2026-10-20: Medicare.gov's default amount changed between searches (alprazolam 60 vs 90 tablets). A price at
    another amount is a different price: it is left out, never mixed in or scaled."""
    db = tmp_path / "eng.db"
    shutil.copyfile(DB, db)
    src = tmp_path / "mpf.db"
    s = sqlite3.connect(src)
    s.execute("""CREATE TABLE drug_prices (plan_id TEXT, batch TEXT, drug TEXT, quantity TEXT, frequency TEXT,
        chain TEXT, store TEXT, in_network INT, covered INT, retail REAL, deductible_phase REAL, initial_phase REAL,
        catastrophic REAL, captured_at TEXT, source_file TEXT, address TEXT)""")
    rows = [("H2001-116-0", "60", 5.0), ("H2001-117-0", "60", 5.1), ("H2001-118-0", "90", 7.5)]
    for pid, q, price in rows:
        s.execute("INSERT INTO drug_prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (pid, "X", LABEL, q, "", "cub", "Cub Pharmacy", 1, 1, price, price, price, 0, "2026-10-20", "x", ""))
    s.commit(); s.close()
    conn = sqlite3.connect(db)
    _build().step_mpf_prices(conn, str(src))
    got = {p: r for p, r in conn.execute("SELECT plan_id, retail FROM mpf_price WHERE contract_id='H2001'")}
    assert got == {"116": 5.0, "117": 5.1}          # the 90-tablet price is not used
