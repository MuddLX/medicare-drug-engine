"""POST /check-drugs - what the Details tab tells the agent about each drug name (2026-10-07)."""
import app.main as M


def _check(*drugs):
    resp = M.app.test_client().post("/check-drugs", json={"drugs": [{"name": n, "dosage": d} for n, d in drugs]})
    assert resp.status_code == 200
    return resp.get_json()["drugs"]


def test_misspelling_with_strength_gets_a_one_click_fix():
    d = _check(("Atorvastin", "40 mg"), ("Plavics", "75 mg"), ("Metphormin ER", "500 mg"))
    assert [x["status"] for x in d] == ["spelling"] * 3
    assert [x["suggestion"] for x in d] == ["Atorvastatin", "Plavix", "Metformin ER"]


def test_correct_names_are_ok():
    d = _check(("Lisinopril", "10 mg"), ("Trelegy Ellipta", "100/62.5/25 mcg"), ("Januvia", "100 mg"))
    assert [x["status"] for x in d] == ["ok"] * 3 and all(not x["note"] for x in d)


def test_strength_that_does_not_exist_is_pointed_out():
    d = _check(("Lisinopril", "7 mg"))[0]
    assert d["status"] == "ok" and "strength" in d["note"]


def test_unclear_spelling_offers_choices_never_applies_them():
    d = _check(("Seroquil", ""))[0]
    assert d["status"] == "unknown" and d["suggestion"] == "" and "Seroquel" in d["candidates"]


def test_not_a_drug():
    d = _check(("water pill", ""))[0]
    assert d["status"] == "unknown" and d["suggestion"] == ""


def test_empty_and_bad_requests():
    assert _check(("", ""))[0]["status"] == "empty"
    client = M.app.test_client()
    assert client.post("/check-drugs", json={"x": 1}).status_code == 400
    assert client.post("/check-drugs", json={"drugs": [{"name": "a"}] * 61}).status_code == 400
