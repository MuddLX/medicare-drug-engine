"""2026-10-08: pricing many plans for one client reads ZIP centroids once, geocodes the client once,
and reads each plan's pharmacies with one query. The nearby-pharmacy lists must be IDENTICAL to the
old one-query-per-ZIP path, and the address must be geocoded once per request, not once per plan."""
import os
import sqlite3

import pytest

import app.main as M

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "medicare_mn.db")
pytestmark = pytest.mark.skipif(not os.path.exists(SRC), reason="needs the local plan database")

ZIPS = ("55443", "55025", "55009", "55369")


def _plans(conn, zip_code):
    choice = M.county_choice(conn, zip_code, None, None, None)
    keys = []
    for cty in [c["county"] for c in choice["counties"]] or [choice["county"]]:
        ma, pd = M.eligible_plan_rows(conn, cty)
        for r in list(ma) + list(pd):
            key = (r[0], str(r[1]).zfill(3))
            if key not in keys:
                keys.append(key)
    return keys


@pytest.mark.parametrize("with_address", [False, True])
def test_fast_path_returns_exactly_the_old_lists(monkeypatch, with_address):
    monkeypatch.setenv("COUNTY_ADDRESS_LOOKUP", "off")
    monkeypatch.setattr(M, "geocode_address_live", lambda *a, **k: (45.0941, -93.3563, "Brooklyn Park"))
    addr = ("8120 Zane Ave N", "Brooklyn Park", "MN") if with_address else (None, None, None)
    conn = sqlite3.connect(SRC)
    zc = M.load_zip_coords(conn)
    compared = 0
    for z in ZIPS:
        coords = M.get_client_coords(conn, z, address=addr[0], city=addr[1], state=addr[2])
        for cid, pid in _plans(conn, z):
            old = M.get_nearby_pharmacies(conn, cid, pid, z, client_address=addr[0], client_city=addr[1],
                                          client_state=addr[2])
            new = M.get_nearby_pharmacies(conn, cid, pid, z, client_address=addr[0], client_city=addr[1],
                                          client_state=addr[2], client_coords=coords, zip_coords=zc)
            assert new == old, f"{z} {cid}-{pid}"
            compared += 1
    assert compared > 20


def test_client_address_geocoded_once_per_request(monkeypatch):
    monkeypatch.setenv("COUNTY_ADDRESS_LOOKUP", "off")
    monkeypatch.setattr(M, "DB_PATH", SRC)
    calls = []
    monkeypatch.setattr(M, "geocode_address_live", lambda *a, **k: calls.append(a) or (45.0941, -93.3563, "BP"))
    monkeypatch.setattr(M, "normalize_drugs", lambda d: [
        {"original": x["name"], "normalized": x["name"], "dosage": x["dosage"], "confidence": 1.0, "flag": ""} for x in d])
    conn = sqlite3.connect(SRC)
    keys = [{"contract_id": c, "plan_id": p} for c, p in _plans(conn, "55443")]
    plans, _county, err = M.resolve_selected_plans(conn, "55443", keys, max_ma=999, max_pd=999)
    assert not err and len(plans) > 5
    M.compute_drug_costs([{"name": "Atorvastatin", "dosage": "20 mg"}], "55443", "01/01/2027",
                         "8120 Zane Ave N", "Brooklyn Park", "MN", plans_override=plans)
    assert len(calls) == 1
