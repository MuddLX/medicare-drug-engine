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


@pytest.fixture
def run(monkeypatch):
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
    return go


def test_brand_not_listed_but_generic_is_shows_generic_tier(run):
    out, calls = run()
    cells = out["drug_detail"][0]["plans"]
    assert cells["Aetna Enhanced"]["covered"] and cells["Aetna Enhanced"]["tier"] == 1
    assert cells["Aetna Enhanced"]["as_generic"] is True
    assert cells["Blue Cross Premier"]["covered"] and cells["Blue Cross Premier"]["tier"] == 3
    assert len(calls) == 1, "generic equivalents are looked up once per drug, not once per plan"


def test_generic_is_not_priced_at_the_brands_negotiated_price(run):
    out, _ = run()
    terms_price = None
    aetna = out["drug_detail"][0]["plans"]["Aetna Enhanced"]
    # Tier 1 generic: the year must cost far less than the brand's $113/month negotiated price.
    assert aetna["annual_total"] is not None and aetna["annual_total"] < 113 * 12 / 2


def test_summary_counts_the_drug_as_covered(run):
    out, _ = run()
    assert all(s["all_drugs_covered"] for s in out["plan_summaries"].values())
