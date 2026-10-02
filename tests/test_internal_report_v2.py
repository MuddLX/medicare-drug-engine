"""One-table internal report (app/internal_pdf.py, 2026-10-02). Offline: renders straight
from engine-shaped data."""
import os
import re

import pytest

import app.main as M
from app import internal_pdf as R


def pages(pdf):
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))


def text_of(pdf):
    import io
    import pypdf
    return "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages)


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def summaries(n_ma=3, n_pd=1):
    out = {}
    ma_ids = [("H3219", "002"), ("H3219", "003"), ("H5959", "014"), ("H4882", "009"), ("H2001", "116"),
              ("H8889", "005"), ("H6154", "001")]
    pd_ids = [("S5601", "050"), ("S5884", "145"), ("S5921", "370")]
    for i, (cid, pid) in enumerate(ma_ids[:n_ma]):
        out[f"MA {i}"] = {"contract_id": cid, "plan_id": pid, "plan_name": f"Plan {i} (PPO)", "plan_type": "MA",
                          "premium_monthly": 10.0 * i, "deductible": 300.0, "total_drug_cost": 900.0 + i,
                          "total_drug_plus_premium": 1000.0 + i, "all_drugs_covered": True}
    for i, (cid, pid) in enumerate(pd_ids[:n_pd]):
        out[f"PD {i}"] = {"contract_id": cid, "plan_id": pid, "plan_name": f"Rx {i} (PDP)", "plan_type": "PD",
                          "premium_monthly": 5.0, "deductible": 615.0, "total_drug_cost": 1100.0,
                          "total_drug_plus_premium": 1160.0, "all_drugs_covered": True}
    return out


def drugs(labels, n=4, tiers=(1, 3, 4)):
    out = []
    for d in range(n):
        plans = {}
        for j, l in enumerate(labels):
            tier = tiers[(d + j) % len(tiers)]
            monthly = [{"month": m, "cost": 50.0 if k == 0 else 10.0} for k, m in enumerate(MONTHS)]
            plans[l] = {"tier": tier, "covered": True, "monthly_costs": monthly, "annual_total": 160.0,
                        "pharmacy_costs": [{"name": "CVS Pharmacy #123", "distance_miles": 1.0, "monthly_costs": monthly,
                                            "annual_total": 160.0},
                                           {"name": "Walgreens", "distance_miles": 1.7, "monthly_costs": monthly,
                                            "annual_total": 170.0}],
                        "mail_order_costs": {"annual_total": 120.0}}
        out.append({"drug_name": f"Drug{d}", "original_name": f"Drug{d}", "dosage": "10mg", "plans": plans})
    return out


def doctors(n):
    return [{"raw_text": f"Dr. Doc {i}", "first_name": "Doc", "last_name": f"Number{i}", "specialty": "Cardiology",
             "bcbs_status": "In Network", "bcbs_detail": "Clinic · City", "aetna_status": "Not Found",
             "hp_status": "In Network", "hp_detail": "HP Clinic · City"} for i in range(n)]


def render(s, d, provs=None, **kw):
    return R.render("Test Person", "01/01/1955", "55441", "10/02/2026", s, d, MONTHS,
                    confidence=0.9, warnings=[], provider_results=provs, plan_year=2027, county="Hennepin", **kw)


def test_typical_report_is_one_page_with_everything():
    s = summaries(3, 1)
    pdf = render(s, drugs(list(s), 6), doctors(2))
    assert pages(pdf) == 1
    txt = text_of(pdf)
    for needle in ("COST", "2027 PLAN YEAR", "MEDICATIONS", "PHARMACY", "DOCTORS", "PART D", "H3219-002",
                   "In network", "Not found", "drug plan", "Mail order", "Hennepin County"):
        assert needle in txt, needle


def test_ten_plan_worst_case_still_one_page():
    s = summaries(7, 3)
    assert pages(render(s, drugs(list(s), 8), doctors(3))) == 1


def test_overflow_continues_on_page_two_with_header_repeated():
    s = summaries(7, 3)
    pdf = render(s, drugs(list(s), 25), doctors(12))
    assert pages(pdf) >= 2
    txt = text_of(pdf).split("Page 1")[1] if "Page 1" in text_of(pdf) else ""
    assert "H3219-002" in txt                 # column headers repeated on page 2


def test_not_found_is_never_called_out_of_network():
    s = summaries(2, 0)
    txt = text_of(render(s, drugs(list(s), 2), doctors(1)))
    assert "out of network" not in txt.lower() and "Not found" in txt


def test_fallback_switch_uses_previous_layout(monkeypatch):
    s = summaries(2, 1)
    monkeypatch.setenv("INTERNAL_REPORT_V1", "1")
    pdf = M.build_pdf("Test Person", "01/01/1955", "55441", "10/02/2026", s, drugs(list(s), 2), MONTHS)
    assert "SECTION 1" in text_of(pdf)
    monkeypatch.delenv("INTERNAL_REPORT_V1")
    pdf = M.build_pdf("Test Person", "01/01/1955", "55441", "10/02/2026", s, drugs(list(s), 2), MONTHS)
    assert "SECTION 1" not in text_of(pdf) and "COST" in text_of(pdf)


def test_soa_era_flags_are_dropped_from_alerts():
    groups = R.group_warnings([{"drug": "Form appears to be a client intake sheet, not a Scope of Appointment"},
                               {"drug": "Document marked as SAMPLE/TEST DATA"},
                               {"drug": "Amlodipine 20mg unusually high dose"}], [])
    flat = [i for _, items in groups for i in items]
    assert flat == ["Amlodipine 20mg unusually high dose"]


# ------------------------------------------------------------------ 2026-10-02 follow-ups
def test_wrong_first_name_match_is_possible_match_not_in_network():
    s = summaries(1, 0)
    prov = [{"raw_text": "Dr. Sarah Johnson - HealthPartners Roseville", "first_name": "Sarah", "last_name": "Johnson",
             "specialty": "Family Medicine", "matched_first": "Steven", "matched_last": "Johnson", "credentials": "DDS",
             "aetna_status": "In Network", "aetna_detail": "Some Clinic · City"}]
    txt = text_of(render(s, drugs(list(s), 1), prov))
    assert "Dr. Sarah Johnson" in txt                       # row shows what the client wrote
    assert "Possible match" in txt and "Steven Johnson" in txt
    assert "In network" not in txt


def test_clinic_only_entry_is_labelled_by_the_clinic():
    s = summaries(1, 0)
    prov = [{"raw_text": "Park Nicollet Clinic", "clinic_name": "Park Nicollet Clinic", "first_name": "", "last_name": "",
             "matched_first": "Amanda", "matched_last": "Christ", "credentials": "MD",
             "aetna_status": "In Network", "aetna_detail": "Park Nicollet Clinic Bloomington · Bloomington"}]
    txt = text_of(render(s, drugs(list(s), 1), prov))
    assert "Park Nicollet Clinic" in txt and "Amanda" not in txt.split("MEDICATIONS")[0]


def test_part_d_columns_are_labelled():
    s = summaries(2, 1)
    assert "PART D" in text_of(render(s, drugs(list(s), 1)))


@pytest.mark.parametrize("name,dose,expected", [
    ("Ozempic", "0.5mg", True), ("Trulicity", "1.5mg", True), ("Mounjaro", "5mg", True),
    ("Semaglutide", "0.5mg pen", True), ("Rybelsus", "7mg", False), ("Insulin glargine", "10 units", False),
    ("Lantus pen", "", False), ("Eliquis", "5mg", False), ("Metformin", "500mg", False),
])
def test_known_injectables_safety_net(name, dose, expected):
    assert M.is_known_injectable(name, name, dose) is expected


def test_cost_rows_say_what_they_exclude():
    s = summaries(2, 0)
    d = drugs(list(s), 2)
    d.append({"drug_name": "Ozempic", "original_name": "Ozempic", "dosage": "0.5mg", "is_injectable": True,
              "plans": {l: {"covered": False, "injectable": True} for l in s}})
    txt = text_of(render(s, d))
    assert "excludes Ozempic" in txt
