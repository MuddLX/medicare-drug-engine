"""Download the RxNorm drug name list from the National Library of Medicine (RxNav).

Run this on a computer with internet (PowerShell, from the engine folder):
    python formulary_2027\\download_rxnorm.py

It makes 2 requests and writes formulary_2027\\rxnorm_concepts.json (about 8 MB) - used by the carrier
drug-list matcher - AND app\\data\\rxnorm_concepts.json.gz, the engine's drug-name reader (2026-10-07).
Monthly routine (README_REFRESH.md): run this, run the tests, commit app/data, push.
"""
import json
import os
import sys
import time
import urllib.request

BASE = "https://rxnav.nlm.nih.gov/REST/allconcepts.json?tty="
GROUPS = {
    "products": "SCD+SBD+GPCK+BPCK",   # every drug product at strength + form (what plans list)
    "names": "IN+PIN+MIN+BN",          # ingredient and brand names
}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rxnorm_concepts.json")


def fetch(url, tries=4):
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TCHealth-formulary-matcher/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except Exception as exc:                                  # network hiccup: wait and retry
            print(f"  attempt {attempt} failed: {exc}")
            if attempt == tries:
                raise
            time.sleep(5 * attempt)


def main():
    out = {"downloaded": time.strftime("%Y-%m-%d %H:%M"), "concepts": []}
    for label, ttys in GROUPS.items():
        print(f"Downloading {label} ({ttys}) ...")
        data = fetch(BASE + ttys)
        rows = (data.get("minConceptGroup") or {}).get("minConcept") or []
        if not rows:
            sys.exit(f"ERROR: RxNav returned no {label}. Nothing was saved; try again later.")
        out["concepts"].extend({"rxcui": r["rxcui"], "name": r["name"], "tty": r["tty"]} for r in rows)
        print(f"  {len(rows):,} rows")
    counts = {}
    for c in out["concepts"]:
        counts[c["tty"]] = counts.get(c["tty"], 0) + 1
    if counts.get("SCD", 0) < 10000 or counts.get("SBD", 0) < 5000:
        sys.exit(f"ERROR: download looks incomplete {counts}. Nothing was saved; try again.")
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f)
    os.replace(tmp, OUT)                                          # only replace the file once it is complete
    print(f"Saved {OUT}")
    import gzip                                                   # the engine's copy (app/drug_resolver.py)
    engine_copy = os.path.join(os.path.dirname(os.path.dirname(OUT)), "app", "data", "rxnorm_concepts.json.gz")
    with gzip.open(engine_copy + ".tmp", "wt", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(engine_copy + ".tmp", engine_copy)
    print(f"Saved {engine_copy}")
    print("Counts:", counts)
    print("Done. Next: python -m pytest tests  (the drug-name test lists must still pass), then commit app/data and push.")


if __name__ == "__main__":
    main()
