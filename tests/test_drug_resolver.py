"""The local drug-name reader (app/drug_resolver.py) against the scored test lists (2026-10-07).

tests/data/drug_cases.psv          ~210 names as people write them (tuning list)
tests/data/drug_cases_holdout.psv  ~125 more, written after tuning (honest check)
Every case must pass: right drug, right strength, right brand, misspellings flagged, and names that
are not one specific drug ("water pill", "insulin") NOT identified. A new miss found in real use
goes into drug_cases.psv first, then gets fixed."""
import os
import pytest
from app import drug_resolver as R
from app.rxnorm_core import close, ing_key

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _cases(fname):
    out = []
    for line in open(os.path.join(DATA, fname), encoding="utf-8"):
        if line.strip() and not line.startswith("#"):
            parts = [x.strip() for x in line.rstrip("\n").split("|")] + [""]
            out.append(tuple(parts[:7]))
    return out


CASES = _cases("drug_cases.psv") + _cases("drug_cases_holdout.psv")


@pytest.mark.parametrize("case", CASES, ids=[f"{c[0]} {c[1]}".strip() for c in CASES])
def test_reads_drug(case):
    name, dose, expect, strengths, brand, flag, hint = case
    r = R.resolve(name, dose, [hint] if hint else ())
    index, _ = R._load()
    if expect == "NONE":
        assert not r.rxcuis, f"{name!r} must not be identified (got {r.matched})"
        return
    assert r.rxcuis, f"{name!r} not identified"
    want = {ing_key(x) for x in expect.split(" + ")}
    ids = set(r.rxcuis)
    for p in (p for p in index.products if p["rxcui"] in ids and not p["pack"]):
        if expect != "*":
            assert {frozenset(w for w in k if w != "usp") for k in p["ings"]} == want, p["name"]
        for s in (float(x) for x in strengths.split(";") if x):
            assert close(s, p["strengths"]), f"strength {s} not in {p['name']}"
        if brand:
            assert p["brand"].startswith(brand), p["name"]
    assert ("spelling" in r.flag) == (flag == "spell"), f"flag {r.flag!r}"


def test_brand_comes_with_generic_twins():
    r = R.resolve("Januvia", "100 mg")
    names = {p["name"] for p in R._load()[0].products if p["rxcui"] in set(r.generic)}
    assert names and all("[" not in n and "sitagliptin" in n and "100 MG" in n for n in names)


def test_misspelling_needs_the_written_strength_to_exist():
    """Zofran 8 mg is never 'corrected' into a different drug that doesn't come in 8 mg."""
    r = R.resolve("Zofran", "8 mg")
    assert not r.rxcuis or "ondansetron" in r.matched


def test_empty_name():
    assert not R.resolve("", "10 mg")


# Reach: what the resolver returns must be what plans actually list (2026-10-07: "Omeprazole 20 mg" was
# read as a rare plain tablet no plan carries, and showed "Not covered" everywhere). Checked against
# CMS's own drug lists (the 2026 file in medicare_mn.db): those list drugs by RxNorm ID, so this tests
# the resolver - not how well we matched carrier PDFs.
DB_CMS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "medicare_mn.db")
NOT_ON_DRUG_LISTS = {"aspirin", "vitamin B12", "cholecalciferol", "acetaminophen", "omega-3 acid ethyl esters",
                     "thyroid", "niacinamide + insulin aspart, human", "*"}


@pytest.fixture(scope="module")
def listed_rxcuis():
    import sqlite3
    if not os.path.exists(DB_CMS):
        pytest.skip("needs medicare_mn.db (CMS drug lists)")
    conn = sqlite3.connect(f"file:{DB_CMS}?mode=ro", uri=True)
    ids = {r[0] for r in conn.execute("SELECT DISTINCT rxcui FROM formulary")}
    conn.close()
    return ids


# Brands no Minnesota plan lists (a real "not covered", checked by hand 2026-10-07):
NOT_LISTED_BRANDS = {"Basaglar KwikPen"}      # plans list Lantus, Toujeo and insulin glargine-yfgn instead
REACH = [c for c in CASES if c[2] != "NONE" and c[2] not in NOT_ON_DRUG_LISTS and c[0] not in NOT_LISTED_BRANDS]


@pytest.mark.parametrize("case", REACH, ids=[f"{c[0]} {c[1]}".strip() for c in REACH])
def test_reading_is_on_a_cms_drug_list(case, listed_rxcuis):
    name, dose, expect, strengths, brand, flag, hint = case
    r = R.resolve(name, dose, [hint] if hint else ())
    assert (set(r.rxcuis) | set(r.generic)) & listed_rxcuis, f"{name} {dose}: read as products no Minnesota plan lists"
