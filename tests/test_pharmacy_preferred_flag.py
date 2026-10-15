"""2026-10-15: a pharmacy is "preferred" only when the CMS network file says so for that pharmacy.
The old rule guessed "chain = preferred" in ZIPs that have both kinds (wrong for e.g. Walgreens on
Allina/Aetna, where Walgreens is a standard pharmacy)."""
import sqlite3

import app.main as M


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
    CREATE TABLE pharmacy_network (contract_id TEXT, plan_id TEXT, npi TEXT, pharmacy_zip TEXT,
        preferred_retail TEXT, preferred_mail TEXT, is_retail INTEGER, is_mail INTEGER, floor_price REAL,
        brand_fee_30 REAL, generic_fee_30 REAL, selected_fee_30 REAL);
    CREATE TABLE pharmacy_names (npi TEXT, name TEXT, address TEXT, city TEXT, state TEXT, zip TEXT,
        lat REAL, lon REAL, is_chain INTEGER);
    CREATE TABLE zip_coords (zip TEXT, lat REAL, lon REAL, city TEXT, state TEXT);
    INSERT INTO zip_coords VALUES ('55303', 45.2, -93.4, 'Anoka', 'MN');
    """)
    for i, (npi, name, chain, pref) in enumerate(rows):     # 1+ mile apart, so not merged as co-located
        conn.execute("INSERT INTO pharmacy_names VALUES (?,?,?,?,?,?,?,?,?)",
                     (npi, name, f"{i + 1} Main St", "Anoka", "MN", "55303", 45.2 + 0.02 * i, -93.4, chain))
        conn.execute("INSERT INTO pharmacy_network VALUES ('H0001','001',?,'55303',?,'N',1,0,0,0,0,0)", (npi, pref))
    return conn


def _flags(conn):
    out = M.get_nearby_pharmacies(conn, "H0001", "001", "55303", max_results=10,
                                  client_coords=(45.2, -93.4, "Anoka"), zip_coords=M.load_zip_coords(conn))
    return {p["name"]: p["preferred"] for p in out}


def test_preferred_follows_each_pharmacys_own_flag():
    conn = _db([("1000000001", "Walgreens", 1, "N"), ("1000000002", "Coborn's Pharmacy", 0, "Y")])
    assert _flags(conn) == {"Walgreens": False, "Coborn's Pharmacy": True}


def test_plan_without_preferred_pharmacies_treats_all_alike():
    conn = _db([("1000000001", "Walgreens", 1, "N"), ("1000000002", "Coborn's Pharmacy", 0, "N")])
    assert _flags(conn) == {"Walgreens": True, "Coborn's Pharmacy": True}
