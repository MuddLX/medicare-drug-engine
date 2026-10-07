"""A Medicare-negotiated drug (Eliquis etc.) costs the plan's OWN tier cost-sharing applied to the
negotiated price (2026-10-06). The engine used to force 25% coinsurance on every plan, which
overcharged plans with lower coinsurance (2027 Aetna Signature: 13%) and ignored flat copays."""
import app.main as M

PLAN = [{"contract_id": "H3219", "plan_id": "008", "carrier": "Aetna Signature Fit", "type": "MA"}]


def _eliquis_rx():
    conn = M.get_db()
    row = conn.execute("""SELECT f.rxcui FROM formulary f JOIN plans p ON p.formulary_id=f.formulary_id
        WHERE p.contract_id='H3219' AND p.plan_id='008' AND f.tier=3 LIMIT 1""").fetchone()
    conn.close()
    return row[0]


def test_negotiated_drug_uses_the_plans_tier_coinsurance(monkeypatch):
    rx = _eliquis_rx()
    monkeypatch.setattr(M, "normalize_drugs", lambda drugs: [
        {"original": "Eliquis", "normalized": "eliquis", "dosage": "5mg", "confidence": 1.0}])
    monkeypatch.setattr(M, "lookup_rxcuis", lambda name, dose: [rx])
    out = M.compute_drug_costs([{"name": "eliquis", "dosage": "5mg"}], "55448", "01/01/2027", plans_override=PLAN)
    cell = out["drug_detail"][0]["plans"]["Aetna Signature Fit"]
    # 2026 Aetna Signature Fit Tier 3 = 24% coinsurance; negotiated price $231/month -> $55.44 once the deductible is met
    assert round(cell["monthly_costs"][-1]["cost"], 2) == round(231 * 0.24, 2)
