"""Creams, ointments and powders read at the right product (2026-10-27, Saralee test client).

"clobetasol 0.05% ointment" was read as the shampoo/foam (ointment strengths are MG/MG, 0.05% = 0.0005 MG/MG);
"nystatin 100,000 unit/gram cream" as the powder (the comma split 100,000 into 100 and 000); a topical powder
as the oral suspension. Each one priced a different product or none at all."""
import pytest
from app import drug_resolver as R

CASES = [
    ("clobetasol", "0.05% ointment", "clobetasol propionate 0.0005 MG/MG Topical Ointment"),
    ("clobetasol", "0.05% cream", "clobetasol propionate 0.5 MG/ML Topical Cream"),
    ("nystatin", "100,000 unit/gram cream", "nystatin 100000 UNT/ML Topical Cream"),
    ("nystatin", "100,000 unit/gram powder", "nystatin 100 UNT/MG Topical Powder"),
    ("nystatin", "100000 unit/g ointment", "nystatin 100 UNT/MG Topical Ointment"),
    ("mupirocin", "2% ointment", "mupirocin 0.02 MG/MG Topical Ointment"),
    ("lidocaine", "5% patch", "lidocaine 0.05 MG/MG Medicated Patch"),
    ("polyethylene glycol 3350", "17 gram powder", "polyethylene glycol 3350 17000 MG Powder for Oral Solution"),
]


@pytest.mark.parametrize("name,dose,expect", CASES)
def test_reads_the_right_product(name, dose, expect):
    import gzip, json, os
    names = {c["rxcui"]: c["name"] for c in json.load(gzip.open(os.path.join(os.path.dirname(R.__file__), "data",
                                                                                "rxnorm_concepts.json.gz"), "rt"))["concepts"]}
    r = R.resolve(name, dose)
    assert r.strength_matched
    assert any(names[x].startswith(expect) for x in r.rxcuis), [names[x] for x in r.rxcuis]
    assert all(names[x].split(" [")[0].endswith(expect.split(" ", 3)[-1].split(" [")[0]) or expect in names[x]
               for x in r.rxcuis[:1])
