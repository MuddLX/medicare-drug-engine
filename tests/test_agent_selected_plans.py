"""Agent-chosen plans (2026-10-02): /plans-for-zip, and selected_plans on /process-soa and
/client-comparison. Offline: every network call (Claude normalization, RxNav, geocoding) is
mocked; the local medicare_mn.db / pbp_benefits.db are read-only."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app.main as M  # noqa: E402

COVERED_EVERYWHERE = ["1000048"]   # on all 17 formularies in the local DB
PARTLY_COVERED = ["1010603"]       # on 3 formularies only -> "not covered" on most plans


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def fake_normalize(drugs):
        return [{"original": d["name"], "normalized": d["name"], "dosage": d.get("dosage", ""),
                 "confidence": 1.0, "flag": "", "is_injectable": False} for d in drugs]

    def fake_rxcuis(name, dosage=""):
        n = name.lower()
        if n.startswith("unknown"):
            return []                      # engine can't identify it
        if n.startswith("rare"):
            return PARTLY_COVERED
        return COVERED_EVERYWHERE

    monkeypatch.setattr(M, "normalize_drugs", fake_normalize)
    monkeypatch.setattr(M, "lookup_rxcuis", fake_rxcuis)
    monkeypatch.setattr(M, "geocode_address_live", lambda *a, **k: None)


@pytest.fixture
def client():
    M.app.config["TESTING"] = True
    return M.app.test_client()


def plans_for(client, zip_code="55441"):
    r = client.get(f"/plans-for-zip?zip={zip_code}")
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def pick(plans, plan_type, n):
    return [{"contract_id": p["contract_id"], "plan_id": p["plan_id"]}
            for p in plans if p["plan_type"] == plan_type][:n]


BASE = {"client_name": "Test Person", "zip_code": "55441", "dob": "01/01/1955",
        "drug_names": "Atorvastatin,Rarezol,Unknownium", "drug_dosages": "20mg,10mg,5mg",
        "providers": [], "flags": [], "custom_plans": "", "plan_year": 2027}


def pdf_pages(data):
    import re
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


# ------------------------------------------------------------------ /plans-for-zip
def test_plans_for_zip_shape_and_split(client):
    body = plans_for(client)
    assert body["zip_code"] == "55441" and body["county"] == "Hennepin" and body["county_exact"] is True
    assert body["data_vintage"]
    types = [p["plan_type"] for p in body["plans"]]
    assert set(types) == {"MA", "PD"}
    assert types == sorted(types, key=lambda t: 0 if t == "MA" else 1), "all MA first, then PD"
    for p in body["plans"]:
        assert set(p) >= {"contract_id", "plan_id", "plan_type", "carrier", "plan_name", "display_name"}
        assert p["display_name"] == f'{p["carrier"]} — {p["plan_name"]}'
        assert len(p["plan_id"]) == 3


def test_plans_for_zip_sorted_by_carrier_then_name(client):
    plans = plans_for(client)["plans"]
    for t in ("MA", "PD"):
        group = [(p["carrier"].lower(), p["plan_name"].lower()) for p in plans if p["plan_type"] == t]
        assert group == sorted(group)


def test_plans_for_zip_no_snp_or_pace(client):
    import sqlite3
    c = sqlite3.connect(f"file:{M.DB_PATH}?mode=ro", uri=True)
    snp = {(r[0], str(r[1]).zfill(3)) for r in c.execute(
        "select contract_id, plan_id from service_area where plan_type like '%SNP%' or plan_type='PACE'")}
    got = {(p["contract_id"], p["plan_id"]) for p in plans_for(client)["plans"]}
    assert not (snp & got)


def test_plans_for_zip_friendly_names(client):
    plans = plans_for(client)["plans"]
    carriers = {p["carrier"] for p in plans}
    assert "UnitedHealthcare" in carriers and "UHC" not in carriers
    for p in plans:
        assert not p["plan_name"].startswith(("Allina Health Aetna Medicare", "AARP Medicare Advantage from"))
        assert "Medica" != p["carrier"] or not p["contract_id"].startswith("H3219"), "Aetna mislabelled as Medica"


def test_plans_for_zip_bad_and_unknown_zip(client):
    assert client.get("/plans-for-zip?zip=55").status_code == 400
    assert client.get("/plans-for-zip").status_code == 400
    r = client.get("/plans-for-zip?zip=99999")
    assert r.status_code == 404 and "error" in r.get_json()


# ------------------------------------------------------------------ resolve_selected_plans
def test_duplicate_labels_are_disambiguated():
    conn = M.get_db()
    try:
        # H3219-001 and H3219-012 share the friendly label "Aetna Signature" (if both serve the county)
        county, _ = M.resolve_county(conn, "55441")
        ma, _pd = M.eligible_plan_rows(conn, county)
        keys = [(r[0], str(r[1]).zfill(3)) for r in ma]
        labels = {}
        for k in keys:
            labels.setdefault(M.FRIENDLY_NAMES.get(k), []).append(k)
        dup = next((v for lbl, v in labels.items() if lbl and len(v) >= 2), None)
        assert dup, "test needs two eligible plans sharing a friendly label"
        plans, _c, err = M.resolve_selected_plans(conn, "55441",
                                                  [{"contract_id": c, "plan_id": p} for c, p in dup[:2]])
    finally:
        conn.close()
    assert err is None
    assert len({p["carrier"] for p in plans}) == 2


def test_selected_keeps_pick_order_and_types(client):
    plans = plans_for(client)["plans"]
    sel = list(reversed(pick(plans, "MA", 3))) + pick(plans, "PD", 1)
    conn = M.get_db()
    try:
        out, _c, err = M.resolve_selected_plans(conn, "55441", sel)
    finally:
        conn.close()
    assert err is None
    assert [(p["contract_id"], p["plan_id"]) for p in out] == [(s["contract_id"], s["plan_id"]) for s in sel]
    assert [p["type"] for p in out][-1] == "PD"


# ------------------------------------------------------------------ /process-soa
def test_process_soa_with_selection_renders_exactly_those(client, monkeypatch):
    plans = plans_for(client)["plans"]
    sel = pick(plans, "MA", 2) + pick(plans, "PD", 1)
    captured = {}
    real = M.build_pdf

    def spy(*a, **k):
        captured["summaries"] = a[4]
        return real(*a, **k)
    monkeypatch.setattr(M, "build_pdf", spy)
    r = client.post("/process-soa", json={**BASE, "selected_plans": sel})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.mimetype == "application/pdf"
    got = [(v["contract_id"], v["plan_id"].zfill(3)) for v in captured["summaries"].values()]
    assert got == [(s["contract_id"], s["plan_id"]) for s in sel]


def test_process_soa_ineligible_plan_is_400(client):
    r = client.post("/process-soa", json={**BASE, "selected_plans": [{"contract_id": "H0000", "plan_id": "001"}]})
    assert r.status_code == 400 and "not available" in r.get_json()["error"]


def test_process_soa_over_limit_is_400(client):
    plans = plans_for(client)["plans"]
    ma = pick(plans, "MA", 50)
    if len(ma) <= M.MAX_SELECTED_MA:
        pytest.skip("county has too few MA plans to exceed the limit")
    r = client.post("/process-soa", json={**BASE, "selected_plans": ma[:M.MAX_SELECTED_MA + 1]})
    assert r.status_code == 400 and "limit" in r.get_json()["error"]


def test_process_soa_duplicate_pick_is_400(client):
    one = pick(plans_for(client)["plans"], "MA", 1)
    r = client.post("/process-soa", json={**BASE, "selected_plans": one + one})
    assert r.status_code == 400


def test_process_soa_without_selection_is_unchanged(client, monkeypatch):
    seen = {}
    real = M.compute_drug_costs

    def spy(*a, **k):
        seen["override"] = k.get("plans_override")
        return real(*a, **k)
    monkeypatch.setattr(M, "compute_drug_costs", spy)
    r = client.post("/process-soa", json=BASE)
    assert r.status_code == 200
    assert seen["override"] is None


# ------------------------------------------------------------------ /client-comparison
def _payload_spy(monkeypatch):
    import app.client_pdf as P
    captured = {}
    real = P.render_client_comparison

    def spy(data):
        captured["data"] = data
        out = real(data)
        captured["pdf"] = out
        return out
    monkeypatch.setattr(P, "render_client_comparison", spy)
    return captured


def test_client_sheet_first_three_ma_then_first_pd(client, monkeypatch):
    cap = _payload_spy(monkeypatch)
    plans = plans_for(client)["plans"]
    ma, pd = pick(plans, "MA", 5), pick(plans, "PD", 2)
    sel = [ma[0], pd[0], ma[1], ma[2], pd[1], ma[3]]          # interleaved pick order
    r = client.post("/client-comparison", json={**BASE, "selected_plans": sel})
    assert r.status_code == 200, r.get_data(as_text=True)
    cols = cap["data"]["plans"]
    assert len(cols) == 4
    assert [c.get("drug_plan_only", False) for c in cols] == [False, False, False, True]
    assert pdf_pages(cap["pdf"]) == 1


def test_client_sheet_pd_column_rows(client, monkeypatch):
    cap = _payload_spy(monkeypatch)
    pd = pick(plans_for(client)["plans"], "PD", 1)
    r = client.post("/client-comparison", json={**BASE, "selected_plans": pd})
    assert r.status_code == 200, r.get_data(as_text=True)
    col = cap["data"]["plans"][0]
    assert col["drug_plan_only"] is True
    for f in ("deductible", "oop_max", "pcp_visit", "specialist_visit", "emergency_room",
              "dental", "vision", "hearing", "otc", "fitness"):
        assert col[f] == "Not included", f
    assert col["hospital_per_day"]["amount"] == "Not included"
    assert col["part_b_giveback"] is None
    assert col["plan_name"].endswith("(PDP)")


def test_client_sheet_ma_only(client, monkeypatch):
    cap = _payload_spy(monkeypatch)
    sel = pick(plans_for(client)["plans"], "MA", 2)
    r = client.post("/client-comparison", json={**BASE, "selected_plans": sel})
    assert r.status_code == 200
    assert len(cap["data"]["plans"]) == 2
    assert not any(c.get("drug_plan_only") for c in cap["data"]["plans"])


def test_client_sheet_honesty_still_applies(client, monkeypatch):
    cap = _payload_spy(monkeypatch)
    sel = pick(plans_for(client)["plans"], "MA", 3)
    client.post("/client-comparison", json={**BASE, "selected_plans": sel})
    for col in cap["data"]["plans"]:
        assert col["rx"]["unverified"] == 1                # "Unknownium" never claimed not covered
        assert "Unknownium" not in col["rx"]["not_covered"]


def test_client_sheet_without_selection_unchanged(client, monkeypatch):
    cap = _payload_spy(monkeypatch)
    r = client.post("/client-comparison", json=BASE)
    assert r.status_code == 200
    assert not any(c.get("drug_plan_only") for c in cap["data"]["plans"])


def test_client_sheet_ineligible_is_400(client):
    r = client.post("/client-comparison", json={**BASE, "selected_plans": [{"contract_id": "S0000", "plan_id": "001"}]})
    assert r.status_code == 400


def test_internal_report_says_not_identified(client, monkeypatch):
    """Section 2 must not claim 'Not Covered' for a drug the engine couldn't identify."""
    texts = []
    from reportlab.platypus import Paragraph as RLP

    class Spy(RLP):
        def __init__(self, text, *a, **k):
            texts.append(str(text))
            super().__init__(text, *a, **k)
    monkeypatch.setattr("reportlab.platypus.Paragraph", Spy)
    sel = pick(plans_for(client)["plans"], "MA", 2)
    r = client.post("/process-soa", json={**BASE, "selected_plans": sel})
    assert r.status_code == 200
    assert texts.count("Not identified") == 2          # one cell per selected plan for "Unknownium"
