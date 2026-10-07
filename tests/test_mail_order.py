"""Mail order must be priced for every drug, not only insulin and negotiated-price drugs (2026-10-06).
The engine asked CMS's cost table for days_supply 3, which doesn't exist (1 = one month, 2 = three
months), so regular drugs silently got no mail-order cost and the mail total covered only some drugs."""
import pytest
import app.main as M
from app import internal_pdf as IP

HP = [{"contract_id": "H4882", "plan_id": "009", "carrier": "HealthPartners Journey Pace", "type": "MA"}]
STD = [{"contract_id": "H2001", "plan_id": "118", "carrier": "UHC AARP ($0/615)", "type": "MA"}]


def _rxcuis(cid, pid, tier, n):
    conn = M.get_db()
    rows = conn.execute("""SELECT DISTINCT f.rxcui FROM formulary f JOIN plans p ON p.formulary_id=f.formulary_id
        JOIN pricing pr ON pr.ndc=f.ndc AND pr.contract_id=p.contract_id AND pr.plan_id=p.plan_id
        WHERE p.contract_id=? AND p.plan_id=? AND f.tier=? LIMIT ?""", (cid, pid, tier, n)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _run(monkeypatch, plans, rx, names):
    monkeypatch.setattr(M, "normalize_drugs", lambda drugs: [
        {"original": n, "normalized": n, "dosage": "10mg", "confidence": 1.0} for n in names])
    monkeypatch.setattr(M, "lookup_rxcuis", lambda name, dose: [rx[names.index(name)]])
    return M.compute_drug_costs([{"name": n, "dosage": "10mg"} for n in names], "55448", "01/01/2027",
                                plans_override=plans)


def test_regular_drug_gets_90_day_mail_copay(monkeypatch):
    rx = _rxcuis("H4882", "009", 2, 1)
    out = _run(monkeypatch, HP, rx, ["testgenerica"])
    mail = out["drug_detail"][0]["plans"]["HealthPartners Journey Pace"]["mail_order_costs"]
    # Tier 2 by mail: $16 for 90 days, no deductible -> $5.33 a month, every month
    assert [round(m["cost"], 2) for m in mail["monthly_costs"]] == [5.33] * 12


def test_every_covered_drug_has_mail_cost(monkeypatch):
    rx = _rxcuis("H4882", "009", 2, 2)
    out = _run(monkeypatch, HP, rx, ["testgenerica", "testgenericb"])
    for d in out["drug_detail"]:
        assert d["plans"]["HealthPartners Journey Pace"]["mail_order_costs"]["annual_total"] is not None


def test_no_mail_benefit_means_not_available_not_free(monkeypatch, tmp_path):
    """A tier with no 90-day (mail) price must show "not available", never $0.
    (2026-10-07: this used H2001-118, but that plan DOES offer mail - in CMS's standard-mail column,
    which the engine wasn't reading. Every plan in the current file offers mail, so the case is built
    on a copy of the database with one tier's 90-day rows removed.)"""
    import shutil, sqlite3
    rx = _rxcuis("H4882", "009", 2, 1)
    db = tmp_path / "nomail.db"
    shutil.copy(M.DB_PATH, db)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM beneficiary_cost WHERE contract_id='H4882' AND plan_id='009' AND tier=2 AND days_supply=2")
    conn.commit()
    conn.close()
    monkeypatch.setattr(M, "DB_PATH", str(db))
    out = _run(monkeypatch, HP, rx, ["testgenerica"])
    mail = out["drug_detail"][0]["plans"]["HealthPartners Journey Pace"].get("mail_order_costs") or {}
    assert mail.get("available") is False
    assert not mail.get("monthly_costs")


def test_plan_with_only_standard_mail_is_priced(monkeypatch):
    """H2001-118: preferred mail 'not applicable', standard mail priced -> mail order IS available."""
    rx = _rxcuis("H2001", "118", 2, 1)
    out = _run(monkeypatch, STD, rx, ["testgenerica"])
    mail = out["drug_detail"][0]["plans"]["UHC AARP ($0/615)"].get("mail_order_costs") or {}
    assert mail.get("available") is not False
    assert mail.get("annual_total") is not None


def test_summary_shows_no_mail_total_when_a_drug_cannot_be_mailed():
    month = ["January", "February"]
    pc = lambda total: [{"name": "CVS", "address": "1 Main", "distance": 1.0, "preferred": True,
                         "annual_total": total, "monthly_costs": [{"month": m, "cost": total / 2} for m in month]}]
    detail = [
        {"plans": {"P": {"tier": 2, "pharmacy_costs": pc(20.0),
                         "mail_order_costs": {"monthly_costs": [{"month": m, "cost": 5.0} for m in month],
                                              "annual_total": 10.0}}}},
        {"plans": {"P": {"tier": 5, "pharmacy_costs": pc(200.0),
                         "mail_order_costs": {"available": False, "monthly_costs": [], "annual_total": None}}}},
    ]
    s = IP.pharmacy_summary("P", detail, month)
    assert s["mail_annual"] is None          # never show a mail total that leaves a drug out
    assert s["mail_partial"] is True
