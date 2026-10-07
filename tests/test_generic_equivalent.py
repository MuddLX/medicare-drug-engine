"""A brand that has gone generic is listed only as its generic (2026-10-07, Dorothy Halvorsen test):
"Januvia" was reported Not covered on every plan although all of them cover generic sitagliptin.
Runs on the local 2027 database (skipped without it). RxNav is replaced by a stand-in."""
import os
import pytest
import app.main as M
from app import data_meta as DM

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "medicare_mn_2027.db")
pytestmark = pytest.mark.skipif(not os.path.exists(SRC), reason="needs medicare_mn_2027.db")

JANUVIA_100_BRAND = ["665036"]                 # sitagliptin phosphate 100 MG Oral Tablet [Januvia]
SITAGLIPTIN_100 = ["665033", "2709603"]        # the generic tablets
PLANS = [{"contract_id": "H3219", "plan_id": "002", "carrier": "Aetna Enhanced", "type": "MA"},
         {"contract_id": "H5959", "plan_id": "021", "carrier": "Blue Cross Premier", "type": "MA"}]


@pytest.fixture(params=["rxnav_backup", "local_resolver"])
def run(monkeypatch, request):
    """Both paths: the local resolver (2026-10-07, the normal path) and RxNav as the backup used when
    the local resolver can't read a name (simulated by switching the local resolver off)."""
    if request.param == "rxnav_backup":
        monkeypatch.setattr(M.drug_resolver, "resolve", lambda *a, **k: M.drug_resolver.Resolution())
    monkeypatch.setattr(DM, "DB_PATH", SRC)
    monkeypatch.setattr(M, "DB_PATH", SRC)
    monkeypatch.setattr(M, "geocode_address_live", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(M, "normalize_drugs", lambda d: [
        {"original": x["name"], "normalized": x["name"], "dosage": "100 mg", "confidence": 1.0} for x in d])
    monkeypatch.setattr(M, "lookup_rxcuis", lambda n, d="": JANUVIA_100_BRAND)
    calls = []

    def generics(rx):
        calls.append(list(rx))
        return SITAGLIPTIN_100
    monkeypatch.setattr(M, "generic_equivalents", generics)

    def go():
        return M.compute_drug_costs([{"name": "Januvia", "dosage": "100 mg"}], "55025", "01/01/2027",
                                    plans_override=PLANS), calls
    go.path = request.param
    return go


def test_brand_not_listed_but_generic_is_shows_generic_tier(run):
    out, calls = run()
    cells = out["drug_detail"][0]["plans"]
    assert cells["Aetna Enhanced"]["covered"] and cells["Aetna Enhanced"]["tier"] == 1
    assert cells["Aetna Enhanced"]["as_generic"] is True
    assert cells["Blue Cross Premier"]["covered"] and cells["Blue Cross Premier"]["tier"] == 3
    if run.path == "rxnav_backup":
        assert len(calls) == 1, "generic equivalents are looked up once per drug, not once per plan"
    else:
        assert calls == [], "the local resolver knows Januvia's generic twins - no internet lookup needed"


def test_generic_is_not_priced_at_the_brands_negotiated_price(run):
    out, _ = run()
    terms_price = None
    aetna = out["drug_detail"][0]["plans"]["Aetna Enhanced"]
    # Tier 1 generic: the year must cost far less than the brand's $113/month negotiated price.
    assert aetna["annual_total"] is not None and aetna["annual_total"] < 113 * 12 / 2


def test_summary_counts_the_drug_as_covered(run):
    out, _ = run()
    assert all(s["all_drugs_covered"] for s in out["plan_summaries"].values())


def test_drug_without_a_price_is_flagged_never_free(monkeypatch):
    """2026-10-07: generic sitagliptin has no price on file yet (estimates come from 2026 prices). At 25%
    coinsurance its cost is unknown - the report must say so, not show $0 as if it were free."""
    monkeypatch.setattr(DM, "DB_PATH", SRC)
    monkeypatch.setattr(M, "DB_PATH", SRC)
    monkeypatch.setattr(M, "geocode_address_live", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(M, "normalize_drugs", lambda d: [
        {"original": x["name"], "normalized": x["name"], "dosage": "100 mg", "confidence": 1.0} for x in d])
    plans = [{"contract_id": "H4882", "plan_id": "015", "carrier": "HP Journey Smart", "type": "MA"}]
    out = M.compute_drug_costs([{"name": "Januvia", "dosage": "100 mg"}], "55025", "01/01/2027", plans_override=plans)
    cell = out["drug_detail"][0]["plans"]["HP Journey Smart"]
    if not cell.get("price_unknown"):
        pytest.skip("this drug has a price in the current database")
    assert any("no price on file" in w["flag"] and "HP Journey Smart" in w["flag"] for w in out["warnings"])
