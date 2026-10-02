"""compute_drug_costs end to end on the real local database with Harold-style brand drugs
(Claude normalisation + RxNav mocked). Pharmacy, plan-level and mail-order totals must all
respect one shared deductible and the out-of-pocket cap."""
import pytest
import app.main as M
from app import drug_year as DY

PLANS = [{"contract_id": "H3219", "plan_id": "008", "carrier": "Aetna Signature Fit", "type": "MA"}]


def _brand_rxcuis(n):
    conn = M.get_db()
    rows = conn.execute("""SELECT DISTINCT f.rxcui FROM formulary f JOIN plans p ON p.formulary_id=f.formulary_id
        WHERE p.contract_id='H3219' AND p.plan_id='008' AND f.tier=3 LIMIT ?""", (n,)).fetchall()
    conn.close()
    return [r[0] for r in rows]


@pytest.fixture
def run(monkeypatch):
    rx = _brand_rxcuis(3)
    names = ["januvia", "jardiance", "eliquis"]
    monkeypatch.setattr(M, "normalize_drugs", lambda drugs: [
        {"original": n.title(), "normalized": n.title(), "dosage": "10mg", "confidence": 1.0} for n in names])
    monkeypatch.setattr(M, "lookup_rxcuis", lambda name, dose: [rx[names.index(name.lower())]])
    return M.compute_drug_costs([{"name": n, "dosage": "10mg"} for n in names], "55448", "01/01/2027",
                                plans_override=PLANS)


def test_plan_level_total_respects_cap(run):
    s = run["plan_summaries"]["Aetna Signature Fit"]
    assert s["total_drug_cost"] <= DY.oop_cap() + 0.01


def test_every_pharmacy_total_respects_cap(run):
    totals = {}
    for d in run["drug_detail"]:
        for pc in d["plans"]["Aetna Signature Fit"].get("pharmacy_costs", []):
            totals[pc["name"]] = totals.get(pc["name"], 0) + pc["annual_total"]
    assert totals and max(totals.values()) <= DY.oop_cap() + 0.01


def test_mail_order_pays_the_deductible_too(run):
    jan = sum(d["plans"]["Aetna Signature Fit"]["mail_order_costs"]["monthly_costs"][0]["cost"]
              for d in run["drug_detail"])
    dec = sum(d["plans"]["Aetna Signature Fit"]["mail_order_costs"]["monthly_costs"][-1]["cost"]
              for d in run["drug_detail"])
    assert jan > dec  # deductible month costs more than a later month


def test_no_internal_keys_leak_into_output(run):
    import json
    assert "_terms" not in json.dumps(run)
