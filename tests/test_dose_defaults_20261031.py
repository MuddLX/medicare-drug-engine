"""2026-10-31 (Jill's notes): a drug with no dose on the sheet is priced at Medicare.gov's usual strength and says
so; when two strengths/forms are equally likely it asks for the dose instead of guessing; an amount written on the
sheet that differs from the amount the price is for is flagged (prices are captured at one standard amount)."""
import os
import pytest
from app import dose_defaults as D
from app.drug_resolver import resolve

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("MEDICARE_TEST_DB") or os.path.join(ROOT, "medicare_mn_2027.db")
needs_db = pytest.mark.skipif(not os.path.exists(DB), reason="needs medicare_mn_2027.db")


def test_has_strength():
    assert D.has_strength("Gabapentin", "300 mg")
    assert D.has_strength("Gabapentin 300mg", "")
    assert not D.has_strength("Gabapentin", "")
    assert not D.has_strength("Vitamin D3", "")              # a digit inside a name is not a strength
    assert not D.has_strength("Atorvastatin", "twice daily")


def _choose(name):
    r = resolve(name, "")
    return D.choose(name, r.rxcuis, r.generic)


def test_no_dose_uses_medicare_gov_usual_strength():
    c = _choose("Gabapentin")
    assert c.kind == "default" and c.label.startswith("Gabapentin 300mg")
    for name, start in [("Lisinopril", "Lisinopril 10mg"), ("Eliquis", "Eliquis 5mg"),
                        ("Synthroid", "Synthroid 75mcg"), ("Levothyroxine", "Levothyroxine sodium 50mcg"),
                        ("Ventolin", "Ventolin"), ("Albuterol sulfate hfa", "Albuterol")]:
        assert _choose(name).label.startswith(start), name


def test_two_equally_likely_forms_ask_for_the_dose():
    c = _choose("Ketoconazole")                              # 2% cream or 2% shampoo
    assert c.kind == "ambiguous" and not c.label and len(c.options) == 2


def test_written_amount_per_month():
    assert D.monthly_amount("40 mg twice daily") == 60
    assert D.monthly_amount("300 mg 3 times a day") == 90
    assert D.monthly_amount("2 tabs daily") == 60
    assert D.monthly_amount("1 tablet BID") == 60
    assert D.monthly_amount("70 mg weekly") == 4
    assert D.monthly_amount("1 tab at bedtime") == 30
    assert D.monthly_amount("1 tab daily as needed") is None   # can't know how many
    assert D.monthly_amount("10 mg") is None                   # no amount written: nothing to compare


@pytest.fixture
def engine(monkeypatch):
    import app.main as M
    from app import data_meta as DM
    monkeypatch.setattr(M, "DB_PATH", DB)
    monkeypatch.setattr(DM, "DB_PATH", DB)
    monkeypatch.setattr(M, "geocode_address_live", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(M, "normalize_drugs", lambda d: [{"original": x["name"], "normalized": x["name"],
                                                          "dosage": x["dosage"], "confidence": 1.0} for x in d])
    monkeypatch.setattr(M, "generic_equivalents", lambda rx: [])          # offline

    def run(name, dosage):
        out = M.compute_drug_costs([{"name": name, "dosage": dosage}], "55443", "01/01/2027",
                                   plans_override=[{"contract_id": "H3219", "plan_id": "002", "carrier": "P",
                                                    "type": "MA"}])
        return out["drug_detail"][0], out["warnings"]
    return run


@needs_db
def test_blank_gabapentin_priced_as_the_usual_300mg_and_flagged(engine):
    blank, warns = engine("Gabapentin", "")
    written, _ = engine("Gabapentin", "300 mg")
    assert blank["dose_assumed"] and blank["dosage"].startswith("300mg")
    assert blank["plans"]["P"]["annual_total"] == written["plans"]["P"]["annual_total"]
    assert any("no dose on the sheet" in w["flag"] for w in warns)


@needs_db
def test_written_dose_is_untouched(engine):
    d, warns = engine("Gabapentin", "100 mg")
    assert not d.get("dose_assumed") and d["dosage"] == "100 mg"
    assert not any("no dose" in w["flag"] for w in warns)


@needs_db
def test_ambiguous_blank_dose_is_left_out_and_asks(engine):
    d, warns = engine("Ketoconazole", "")
    cell = d["plans"]["P"]
    assert d["dose_needed"] and cell.get("price_unknown") and cell.get("annual_total") is None
    assert any("add the dose" in w["flag"] for w in warns)


@needs_db
def test_amount_different_from_the_priced_amount_is_flagged(engine):
    d, warns = engine("Atorvastatin", "40 mg twice daily")          # priced at Medicare.gov's 30 a month
    assert "60" in d["amount_note"] and "30" in d["amount_note"]
    assert any(w["flag"] == d["amount_note"] for w in warns)
    same, _ = engine("Eliquis", "5 mg twice daily")                 # Eliquis is priced at 60 a month: no note
    assert not same.get("amount_note")


def test_an_amount_is_not_a_strength():
    assert D.strip_amounts("1 tab daily") == "daily"
    assert not D.has_strength("Atorvastatin", "1 tab daily")
    assert D.has_strength("Atorvastatin", "40 mg 2 times a day")


@needs_db
def test_one_tab_daily_is_priced_at_the_usual_strength(engine):
    d, _ = engine("Atorvastatin", "1 tab daily")
    assert d["dose_assumed"] and d["dosage"].startswith("40mg") and not d.get("strength_not_found")
    assert d["plans"]["P"]["annual_total"] is not None
    two, _ = engine("Atorvastatin", "2 tabs twice daily")
    assert "120" in two["amount_note"]


@needs_db
def test_the_dose_column_says_what_was_assumed(engine):
    """The app's Compare view shows only the dose text (not warnings), so the note lives there (2026-10-31)."""
    assert engine("Gabapentin", "")[0]["dosage"] == "300mg capsule" + D.ASSUMED_SUFFIX
    assert engine("Ketoconazole", "")[0]["dosage"] == D.NEEDED_TEXT
    assert engine("Atorvastatin", "40 mg twice daily")[0]["dosage"] == "40 mg twice daily (priced for 30 a month)"
