"""Old vs new drug-name lookup, scored on the test lists (2026-10-07). Needs internet (RxNav).

    python tools/drug_lookup_benchmark.py

OLD = RxNav exact-name lookup (lookup_rxcuis - still the backup), NEW = local resolver (app/drug_resolver.py).
A case passes when every product returned is the right drug and strength (and nothing for "NONE" cases)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import drug_resolver as R            # noqa: E402
from app.rxnorm_core import close, ing_key    # noqa: E402
import app.main as M                          # noqa: E402


def cases():
    for fname in ("drug_cases.psv", "drug_cases_holdout.psv"):
        for line in open(os.path.join(ROOT, "tests", "data", fname), encoding="utf-8"):
            if line.strip() and not line.startswith("#"):
                yield tuple(([x.strip() for x in line.rstrip("\n").split("|")] + [""])[:7])


def right(ids, expect, strengths):
    index, _ = R._load()
    if expect == "NONE":
        return not ids
    prods = [p for p in index.products if p["rxcui"] in set(ids) and not p["pack"]]
    if not prods:
        return False
    want = {ing_key(x) for x in expect.split(" + ")}
    for p in prods:
        if expect != "*" and {frozenset(w for w in k if w != "usp") for k in p["ings"]} != want:
            return False
        if any(not close(float(s), p["strengths"]) for s in strengths.split(";") if s):
            return False
    return True


if __name__ == "__main__":
    old_ok = new_ok = n = 0
    misses = []
    for name, dose, expect, strengths, brand, flag, hint in cases():
        n += 1
        old = right(M.lookup_rxcuis(name, dose), expect, strengths)
        new = right(R.resolve(name, dose, [hint] if hint else ()).rxcuis, expect, strengths)
        old_ok += old
        new_ok += new
        if not old:
            misses.append(f"{name} {dose}".strip())
        print(f"{'ok ' if old else 'MISS'} {'ok ' if new else 'MISS'}  {name} {dose}".rstrip(), flush=True)
    print(f"\nOLD (RxNav exact name): {old_ok}/{n} = {old_ok / n:.1%}")
    print(f"NEW (local resolver):   {new_ok}/{n} = {new_ok / n:.1%}")
