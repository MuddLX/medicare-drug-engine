"""
Load new Medicare.gov captures into the engine - one command (2026-10-19).

  python tools\\mpf_load.py              check + load (writes medicare_mn_2027.db; nothing is uploaded)
  python tools\\mpf_load.py --dry-run    check only: shows what would be added, changes nothing

Steps:
  1. Rebuild the checked capture database  (Plan jsons\\_tools\\mpf_consolidate.py)
  2. Match every NEW Medicare.gov drug label to its RxNorm product. A label is accepted only when the engine,
     using that product's tier on each plan's official 2027 drug list, reproduces Medicare.gov's own
     deductible-phase and after-deductible costs on EVERY captured row (and at least 2 rows, at least one of
     them covered). Anything less goes to a review list - it is never loaded on a guess.
     Labels already in app\\data\\mpf_drug_map.json are never changed.
  3. Back up medicare_mn_2027.db to _scratch\\, then reload prices (build_2027_db.py mpf_prices).
  4. Print what changed.

Afterwards: run the tests, upload the database (python r2_files.py upload medicare_mn_2027.db --as medicare_mn.db),
and commit app\\data\\mpf_drug_map.json.
"""
import collections
import datetime
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

ENG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ENG)
PJ = os.environ.get("PLAN_JSONS") or os.path.join(os.path.expanduser("~"), "Desktop", "Plan jsons")
ENG_DB = os.environ.get("BUILD_2027_DB") or os.path.join(ENG, "medicare_mn_2027.db")
MAP = os.path.join(ENG, "app", "data", "mpf_drug_map.json")

from app import drug_resolver as R                 # noqa: E402
from app.main import retail_cost_share, is_insulin  # noqa: E402

VERIFIED_CHAINS = {"cub", "walgreens", "cvs", "walmart", "costco", "hy-vee"}


def numbers(name):
    head = re.split(r"\b(?:oral|topical|ophthalmic|otic|nasal|injectable|injection|inhal\w*|transdermal|rectal|"
                    r"vaginal|extended|delayed|chewable|disintegrating)\b", name.lower())[0]
    return tuple(sorted(re.findall(r"\d+(?:\.\d+)?", head)))


def ingr(name):
    return re.split(r"\s\d", name.lower())[0].replace("[", "").strip()


_UNITS = {"mg", "ml", "mcg", "unt", "meq", "hr", "actuat", "g", "gm", "mg/ml", "unt/ml", "day", "pack", "pk", "doses",
          "dose", "tablet", "capsule", "solution", "cream", "ointment", "suspension", "unit", "units", "aug"}
_FORMS = r"\b(?:oral|topical|ophthalmic|otic|nasal|injectable|injection|inhal\w*|transdermal|rectal|vaginal|" \
         r"extended|delayed|chewable|disintegrating|pen|auto-injector|prefilled|powder|cartridge|dry|metered|" \
         r"medicated|enteric|sublingual|buccal|\d+\s*day)\b"


def ingredients(name):
    """'polyethylene glycol 3350 236000 MG / potassium chloride 2970 MG / ... Powder for Oral Solution'
    -> frozenset of ingredient words (numbers, units, dose forms and [Brand] dropped)."""
    head = re.split(_FORMS, re.sub(r"\[.*?\]", "", name.lower()))[0]
    return frozenset(w for w in re.findall(r"[a-z][a-z-]*", head) if w not in _UNITS)


class Matcher:
    """Same rule that confirmed the first 193 labels (Plan jsons/_tools/verify_2026-10-17/verify2.py step 1)."""

    def __init__(self, en):
        self.en = en
        con = json.load(gzip.open(os.path.join(ENG, "app/data/rxnorm_concepts.json.gz"), "rt"))["concepts"]
        self.prod = [c for c in con if c["tty"] in ("SCD", "SBD", "GPCK", "BPCK")]
        self.byid = {c["rxcui"]: c for c in con}
        self.key_index = collections.defaultdict(list)
        for c in self.prod:
            self.key_index[(ingr(c["name"]), numbers(c["name"]))].append(c["rxcui"])
        self.plans = {(c, p): fid for c, p, fid in en.execute("SELECT contract_id, plan_id, formulary_id FROM plans")}

    def siblings(self, rxs):
        keys = {(ingr(self.byid[r]["name"]), numbers(self.byid[r]["name"])) for r in rxs if r in self.byid}
        return [x for k in keys for x in self.key_index[k]]

    def tiers(self, fid, rxs):
        if not rxs:
            return {}
        return dict(self.en.execute(f"SELECT rxcui, MIN(tier) FROM formulary WHERE formulary_id=? AND rxcui IN "
                                    f"({','.join('?' * len(rxs))}) GROUP BY rxcui", (fid, *rxs)))

    def phase(self, c, p, tier, price, pref, label):
        if is_insulin(label) or is_insulin(label.split()[0]):
            r = self.en.execute("SELECT copay_pref, copay_nonpref, coins_pref, coins_nonpref FROM insulin_cost "
                                "WHERE contract_id=? AND plan_id=? AND tier IN (?, '.') AND days_supply=1 "
                                "ORDER BY tier = '.' LIMIT 1", (c, p, str(tier))).fetchone()
            v = 35.0
            if r:
                cp, co = (r[0], r[2]) if pref else (r[1], r[3])
                v = min([x for x in (cp, (co * price if co is not None else None), 35.0) if x is not None])
            v = min(v, price) if price else v
            return v, v
        row = self.en.execute("""SELECT cost_type_pref, cost_amt_pref, cost_type_nonpref, cost_amt_nonpref, ded_applies
            FROM beneficiary_cost WHERE contract_id=? AND plan_id=? AND tier=? AND days_supply=1
            ORDER BY (coverage_level <> '1'), coverage_level LIMIT 1""", (c, p, tier)).fetchone()
        if not row:
            return None
        ct, amt = retail_cost_share(row[0], row[1], row[2], row[3], pref)
        init = 0.0 if ct == 0 else (price * amt if ct == 2 else min(amt, price))
        return (price if row[4] != "N" else init), init

    def match(self, label, rows):
        """rows: [(contract, plan, covered, retail, ded_phase, init_phase)] at in-network retail stores."""
        res = R.resolve(label)
        base = list(dict.fromkeys(res.rxcuis))
        cands = list(dict.fromkeys(base + self.siblings(base)))
        score, seen, covered_hits = collections.Counter(), collections.Counter(), collections.Counter()
        for c, p, covered, retail, dph, iph in rows:
            fid = self.plans.get((c, p))
            if fid is None:
                continue
            tmap = self.tiers(fid, cands)
            for rx in cands:
                seen[rx] += 1
                t = tmap.get(rx)
                if (t is not None) != bool(covered):
                    continue
                if not covered:
                    score[rx] += 1
                    continue
                if any((ph := self.phase(c, p, t, retail, pref, label)) and abs(ph[1] - iph) <= .02
                       and abs(ph[0] - dph) <= .02 for pref in (True, False)):
                    score[rx] += 1
                    covered_hits[rx] += 1
        best = max(score.values()) if score else 0
        top = [rx for rx in cands if score[rx] == best and best > 0]
        of = seen[top[0]] if top else len(rows)
        ok = bool(top) and best == of and of >= 2 and covered_hits[top[0]] >= 1
        # Tied products must contain the same ingredients (pen/vial/brand may differ). Different products
        # tying (2026-10-19: GoLYTELY and NuLYTELY - NuLYTELY has no sodium sulfate - same tier) -> review.
        # A product whose ingredients are exactly the label's wins over look-alikes (lisinopril/HCTZ, not quinapril/HCTZ)
        sets = {x: ingredients(self.byid[x]["name"]) for x in top if x in self.byid}
        known = frozenset().union(*sets.values()) if sets else frozenset()
        want = frozenset(re.findall(r"[a-z][a-z-]*", label.lower())) & known
        exact = [x for x, st in sets.items() if st == want]
        if exact:
            top = exact
        keys = {ingredients(self.byid[x]["name"]) for x in top if x in self.byid}
        if len(keys) > 1:
            ok = False
        return {"ok": ok, "rxcuis": top, "agree": best, "of": of, "products_tied": len(keys),
                "names": sorted({self.byid[x]["name"] for x in top if x in self.byid})[:4]}


def main():
    dry = "--dry-run" in sys.argv
    print(f"Capture folder: {PJ}\nEngine database: {ENG_DB}\n")

    print("1) Rebuilding the checked capture database ...")
    r = subprocess.run([sys.executable, os.path.join(PJ, "_tools", "mpf_consolidate.py"), PJ],
                       capture_output=True, text=True)
    print("   " + "\n   ".join((r.stdout or "").strip().splitlines()[-6:]))
    if r.returncode != 0:
        sys.exit("   STOPPED: the consolidate step failed:\n" + (r.stderr or "")[-2000:])
    mpf_db = os.path.join(PJ, "_consolidated", "mpf_prices_2027.db")

    print("\n2) Matching new drug labels ...")
    dmap_doc = json.load(open(MAP, encoding="utf-8"))
    dmap = dmap_doc["drugs"]
    en = sqlite3.connect(f"file:{ENG_DB}?mode=ro", uri=True)
    src = sqlite3.connect(f"file:{mpf_db}?mode=ro", uri=True)
    rows = collections.defaultdict(list)
    for pid, drug, chain, inn, cov, retail, dph, iph in src.execute(
            "SELECT plan_id, drug, chain, in_network, covered, retail, deductible_phase, initial_phase FROM drug_prices"):
        if chain == "mail order" or not inn or drug in dmap or retail is None:
            continue
        c, p = pid.split("-")[0], pid.split("-")[1].zfill(3)
        rows[drug].append((c, p, cov, float(retail), float(dph or 0), float(iph or 0)))
    m = Matcher(en)
    accepted, review = {}, {}
    for label in sorted(rows):
        res = m.match(label, rows[label])
        (accepted if res["ok"] else review)[label] = res
    for label, res in accepted.items():
        print(f"   + {label}  ->  {res['names'][:1]}  ({res['agree']}/{res['of']} rows)")
    for label, res in review.items():
        print(f"   ? {label}  ->  {res['names'][:1] or 'no product found'}  ({res['agree']}/{res['of']} rows) - NOT loaded")
    print(f"   {len(accepted)} new labels accepted, {len(review)} need a look, {len(dmap)} already loaded")
    json.dump(review, open(os.path.join(PJ, "_consolidated", "labels_to_review.json"), "w"), indent=1)

    # where the prices come from: chain-wide vs store by store
    stores = collections.Counter()
    for (ch,) in src.execute("SELECT chain FROM drug_prices"):
        stores["chain" if ch in VERIFIED_CHAINS or ch == "mail order" else "store"] += 1
    print(f"   prices: {stores['chain']:,} at metro chains, {stores['store']:,} at small-town stores (priced store by store)")
    en.close(); src.close()

    if dry:
        print("\nDry run: nothing changed.")
        return

    print("\n3) Loading ...")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    bdir = os.path.join(os.path.dirname(os.path.abspath(ENG_DB)), "_scratch")
    os.makedirs(bdir, exist_ok=True)
    backup = os.path.join(bdir, f"medicare_mn_2027.before_load_{stamp}.db")
    shutil.copyfile(ENG_DB, backup)
    shutil.copyfile(MAP, os.path.join(bdir, f"mpf_drug_map.before_load_{stamp}.json"))
    if accepted:
        for label, res in accepted.items():
            dmap[label] = {"rxcuis": res["rxcuis"], "rows_checked": res["of"], "names": res["names"],
                           "added": datetime.date.today().isoformat()}
        dmap_doc["drugs"] = dict(sorted(dmap.items(), key=lambda kv: kv[0].lower()))
        with open(MAP, "w", encoding="utf-8") as f:
            json.dump(dmap_doc, f, indent=1, ensure_ascii=False)
    before = sqlite3.connect(backup)
    n_before = before.execute("SELECT COUNT(*) FROM mpf_price").fetchone()[0]
    before.close()
    r = subprocess.run([sys.executable, os.path.join(ENG, "build_2027_db.py"), "mpf_prices", mpf_db],
                       capture_output=True, text=True, cwd=ENG, env={**os.environ, "BUILD_2027_DB": ENG_DB})
    print("   " + "\n   ".join((r.stdout or "").strip().splitlines()))
    if r.returncode != 0:
        shutil.copyfile(backup, ENG_DB)
        sys.exit("   STOPPED: loading failed, the database was put back as it was:\n" + (r.stderr or "")[-2000:])
    after = sqlite3.connect(ENG_DB)
    n_after = after.execute("SELECT COUNT(*) FROM mpf_price").fetchone()[0]
    after.close()

    print(f"\n4) Done. Prices on file: {n_before:,} -> {n_after:,}. Backup: {backup}")
    print("   Next: python -m pytest tests -q   then upload + commit (see the top of this file).")


if __name__ == "__main__":
    main()
