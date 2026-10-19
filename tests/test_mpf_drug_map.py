"""app/data/mpf_drug_map.json: each Medicare.gov label maps only to products of ONE drug (2026-10-19).

The label 'Hydrochlorothiazide / lisinopril 20-25mg tablet' also listed quinapril/HCTZ, a different drug that sat
on the same tier, so a client on quinapril/HCTZ would have been priced with lisinopril/HCTZ's price."""
import gzip
import importlib.util
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("mpf_load", os.path.join(HERE, "tools", "mpf_load.py"))
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)
MAP = json.load(open(os.path.join(HERE, "app", "data", "mpf_drug_map.json"), encoding="utf-8"))["drugs"]
NAMES = {c["rxcui"]: c["name"] for c in json.load(gzip.open(os.path.join(HERE, "app/data/rxnorm_concepts.json.gz"), "rt"))["concepts"]}


def test_every_label_maps_to_one_drug():
    mixed = {lab: sorted({tuple(sorted(L.ingredients(NAMES[r]))) for r in e["rxcuis"] if r in NAMES})
             for lab, e in MAP.items()}
    assert {lab: v for lab, v in mixed.items() if len(v) > 1} == {}


def test_lisinopril_hctz_is_not_quinapril():
    assert MAP["Hydrochlorothiazide / lisinopril 20-25mg tablet"]["rxcuis"] == ["197887"]


def test_ingredients_reader():
    assert L.ingredients("3 ML insulin lispro 100 UNT/ML Pen Injector [Humalog]") == {"insulin", "lispro"}
    assert "sulfate" not in L.ingredients("polyethylene glycol 3350 420000 MG / potassium chloride 1480 MG / "
                                          "sodium bicarbonate 5720 MG / sodium chloride 11200 MG Powder for Oral Solution")
