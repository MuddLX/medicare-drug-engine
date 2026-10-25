"""A comma inside a drug name or dose must never move a dose onto another drug (2026-10-24).

The app sends drug_names / drug_dosages as comma-joined strings. 'artificial tears, peg 400 (Systane)' (a name with
a comma) and 'nystatin 100,000 unit/gram' (a dose with a comma) shifted every dose after them: Lipitor got
calcium citrate's 250 mg, furosemide got gabapentin's 300 mg, gabapentin got '100' - read on the report as
handwriting misreads."""
import app.main as M


def test_structured_list_wins():
    drugs, note = M.request_drugs({"drugs": [{"name": "artificial tears, peg 400 (Systane)", "dosage": "0.4-0.3%"},
                                             {"name": "nystatin", "dosage": "100,000 unit/gram"}],
                                   "drug_names": "ignored", "drug_dosages": "ignored"})
    assert drugs == [{"name": "artificial tears, peg 400 (Systane)", "dosage": "0.4-0.3%"},
                     {"name": "nystatin", "dosage": "100,000 unit/gram"}] and note == ""


def test_thousands_comma_in_a_dose_is_kept():
    drugs, note = M.request_drugs({"drug_names": "gabapentin,nystatin,omeprazole",
                                   "drug_dosages": "300 mg,100,000 unit/gram,20 mg"})
    assert [d["dosage"] for d in drugs] == ["300 mg", "100,000 unit/gram", "20 mg"] and note == ""


def test_lists_that_dont_line_up_use_no_doses():
    drugs, note = M.request_drugs({"drug_names": "artificial tears, peg 400 (Systane),atorvastatin,calcium citrate",
                                   "drug_dosages": "0.4-0.3%,10 mg,250 mg"})
    assert all(d["dosage"] == "" for d in drugs) and "didn't line up" in note
