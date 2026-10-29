"""Check the engine's yearly drug cost against Medicare.gov's own numbers (2026-10-29).

Every saved Medicare.gov Plan Finder search (Desktop/Plan jsons/2027_*/*.json) holds, for each pharmacy it
showed, Medicare.gov's estimated yearly drug cost and the month the deductible is met. This tool re-runs
each search through the REAL engine path (compute_drug_costs: name reading, coverage, tier, the store's own
price, the shared deductible and $2,400 cap) for exactly that plan, drug list and store, and compares.

    python tools/verify_against_mgov.py                     # all searches -> _scratch/verify_mgov/results.jsonl
    python tools/verify_against_mgov.py --start 0 --count 40   # a slice (resumable; already-done files skipped)
    python tools/verify_against_mgov.py --report             # summary from results.jsonl

A store-search counts as a MATCH when the yearly cost is within $5 (Jordon, 2026-10-29: "a few dollars is not a
big deal; $20, $30, $100 is"). Searches the engine is not built to price the same way are reported separately,
never mixed into the match rate:
  * different amount - a drug entered at an amount other than the one the engine stores prices for
  * injectable       - the engine sends injectables to "verify" instead of pricing them
  * not covered      - Medicare.gov counts a non-covered drug's full price; the engine leaves it out and marks it
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PLAN_JSONS = os.environ.get("PLAN_JSONS", os.path.join(os.path.expanduser("~"), "Desktop", "Plan jsons"))
OUT = os.path.join(ROOT, "_scratch", "verify_mgov")
MATCH = 5.0          # dollars a year
RESULTS = os.environ.get("VERIFY_RESULTS", "results.jsonl")
VERIFIED = {"cub", "walgreens", "cvs", "walmart", "costco", "hy-vee", "hyvee"}


def street_key(addr):
    t = re.sub(r"[.,#]", " ", (addr or "").upper()).split()
    return " ".join(t[:2])           # house number + first street word: "3633 BUNKER"


def setup_engine():
    import app.main as M
    from app import data_meta as DM
    db = os.path.join(ROOT, "medicare_mn_2027.db")
    DM.DB_PATH = db
    M.DB_PATH = db
    M.geocode_address_live = lambda *a, **k: (None, None, None)
    M.normalize_drugs = lambda d: [{"original": x["name"], "normalized": x["name"], "dosage": "", "confidence": 1.0}
                                   for x in d]
    return M


def files(plan_jsons):
    out = []
    for f in sorted(glob.glob(os.path.join(plan_jsons, "2027_*", "*.json"))):
        if os.sep + "_" in f.replace(plan_jsons, ""):
            continue
        out.append(f)
    return out


def canonical_qty(conn, contract, plan):
    """label (lower) -> the amount the engine's prices are stored at, for this plan."""
    q = {}
    for label, qty in conn.execute("SELECT DISTINCT label, quantity FROM mpf_price WHERE contract_id=? AND plan_id=?",
                                   (contract, plan)):
        q.setdefault((label or "").lower(), set()).add(str(qty))
    return q


def check_file(M, path):
    d = json.load(open(path, encoding="utf-8"))
    if not isinstance(d, dict):
        return []
    pid = d.get("plan_id") or ""
    parts = pid.split("-")
    if len(parts) < 2 or not d.get("yearly_drug_cost") or not d.get("drugs_entered"):
        return []
    contract, plan = parts[0], parts[1]
    zip_code = d.get("zip") or d.get("route_zip")
    drugs = d["drugs_entered"]
    conn = M.get_db()
    canon = canonical_qty(conn, contract, plan)
    have = M.mpf_chains_for_plan(conn, contract, plan)
    addr_of = {p.get("key"): p.get("address") or "" for p in d.get("pharmacies") or []}
    conn.close()

    # why this search can't be compared like-for-like (reported, not counted)
    reasons = []
    for e in drugs:
        if (e.get("frequency") or "Every month") != "Every month":
            reasons.append(f"not monthly: {e['drug']}")
        q = canon.get(e["drug"].lower())
        if q is not None and str(e.get("quantity")) not in q:
            reasons.append(f"different amount: {e['drug']} {e.get('quantity')} (engine {'/'.join(sorted(q))})")
        if M.is_known_injectable(e["drug"]):
            reasons.append(f"injectable: {e['drug']}")
    if d.get("not_covered"):
        reasons.append("not covered on Medicare.gov: " + "; ".join(d["not_covered"][:3]))

    rows = []
    for store, mgov_year in d["yearly_drug_cost"].items():
        if mgov_year is None:
            continue
        is_mail = store.lower().startswith("mail")
        chain = M.chain_of(store) if not is_mail else "mail order"
        target_addr = street_key(addr_of.get(store, ""))
        real = M.get_nearby_pharmacies

        def pick(conn_, c, p, z, **kw):
            kw.update(max_results=500, max_miles=150, same_store=lambda ph: ph["npi"])
            found = real(conn_, c, p, z, **kw)
            want = [ph for ph in found if (chain in VERIFIED and M.chain_of(ph["name"]) == chain)
                    or (target_addr and street_key(ph.get("address")) == target_addr)]
            exact = [ph for ph in want if target_addr and street_key(ph.get("address")) == target_addr]
            return (exact or want)[:1]

        M.get_nearby_pharmacies = pick
        try:
            out = M.compute_drug_costs([{"name": e["drug"], "dosage": ""} for e in drugs], zip_code, "01/01/2027",
                                       plans_override=[{"contract_id": contract, "plan_id": plan, "carrier": "P",
                                                        "type": "MA"}])
        finally:
            M.get_nearby_pharmacies = real
        det = out["drug_detail"]
        engine_year, ded_met, store_found, per_drug = None, None, None, []
        if is_mail:
            tot, ok = 0.0, True
            for dr in det:
                cell = dr["plans"].get("P") or {}
                mo = cell.get("mail_order_costs") or {}
                if cell.get("covered") and mo.get("annual_total") is not None:
                    tot += mo["annual_total"]
                    ded_met = ded_met or mo.get("ded_met")
                elif cell.get("covered"):
                    ok = False
            engine_year = round(tot, 2) if ok else None
            store_found = "mail order"
        else:
            tot, n = 0.0, 0
            for dr in det:
                cell = dr["plans"].get("P") or {}
                for pc in cell.get("pharmacy_costs") or []:
                    tot += pc.get("annual_total") or 0
                    n += 1
                    store_found = pc["name"]
                    ded_met = ded_met or pc.get("ded_met")
            engine_year = round(tot, 2) if store_found else None
        for dr in det:
            cell = dr["plans"].get("P") or {}
            per_drug.append({"drug": dr.get("original_name"), "read_as": dr.get("read_as"), "tier": cell.get("tier"),
                             "covered": cell.get("covered"), "price_unknown": cell.get("price_unknown"),
                             "error": dr.get("error"), "injectable": dr.get("is_injectable")})
        rows.append({
            "file": os.path.relpath(path, PLAN_JSONS), "plan": f"{contract}-{plan}", "zip": zip_code, "store": store,
            "store_found": store_found, "drugs": len(drugs), "mgov_year": mgov_year, "engine_year": engine_year,
            "diff": None if engine_year is None else round(engine_year - mgov_year, 2),
            "mgov_ded_met": (d.get("deductible_met") or {}).get(store), "engine_ded_met": ded_met,
            "excluded_reasons": reasons,
            "unpriced": [x["drug"] for x in per_drug if x["covered"] and x["price_unknown"]],
            "unread": [x["drug"] for x in per_drug if x["error"]],
            "engine_not_covered": [x["drug"] for x in per_drug if x["covered"] is False and not x["injectable"]],
            "per_drug": per_drug,
        })
    return rows


def run(start, count):
    M = setup_engine()
    os.makedirs(OUT, exist_ok=True)
    res = os.path.join(OUT, RESULTS)
    done = set()
    if os.path.exists(res):
        done = {r["file"] for r in (json.loads(l) for l in open(res, encoding="utf-8") if l.strip()) if "error" not in r}
    todo = files(PLAN_JSONS)[start:start + count if count else None]
    t0 = time.time()
    with open(res, "a", encoding="utf-8") as fh:
        for f in todo:
            rel = os.path.relpath(f, PLAN_JSONS)
            if rel in done:
                continue
            try:
                rows = check_file(M, f)
            except Exception as exc:          # one bad file never stops the check
                rows = [{"file": rel, "error": f"{type(exc).__name__}: {exc}"}]
            if not rows:
                rows = [{"file": rel, "skipped": "no yearly cost / drugs in this save"}]
            for r in rows:
                fh.write(json.dumps(r) + "\n")
            fh.flush()
    print(f"checked {len(todo)} files in {time.time() - t0:.0f}s -> {res}")


def month(v):
    return (v or "").split()[0][:3].lower() if v else ""


def report():
    res = os.path.join(OUT, RESULTS)
    rows = [json.loads(l) for l in open(res, encoding="utf-8") if l.strip()]
    last = {}
    for r in rows:                       # a file re-checked after an error: its latest rows win
        last.setdefault(r["file"], []).append(r)
    rows = [r for rs in last.values() for r in (rs if all("error" not in x for x in rs) else rs[-1:])]
    errors = [r for r in rows if "error" in r]
    rows = [r for r in rows if "mgov_year" in r]
    clean = [r for r in rows if not r["excluded_reasons"]]
    other = [r for r in rows if r["excluded_reasons"]]
    priced = [r for r in clean if r["engine_year"] is not None]
    nostore = [r for r in clean if r["engine_year"] is None]
    def band(r):
        a = abs(r["diff"])
        return "within $5" if a <= MATCH else "$5-20 off" if a <= 20 else "$20-100 off" if a <= 100 else "over $100 off"
    bands = Counter(band(r) for r in priced)
    ded = [r for r in priced if r["mgov_ded_met"]]
    ded_ok = sum(1 for r in ded if month(r["mgov_ded_met"]) == month(r["engine_ded_met"])
                 or (not r["engine_ded_met"] and "not" in (r["mgov_ded_met"] or "").lower()))
    lines = [f"Searches x stores checked: {len(rows)} ({len(errors)} errors)",
             f"Comparable like-for-like: {len(clean)}  (engine priced {len(priced)}, store not matched {len(nostore)})",
             "Yearly drug cost vs Medicare.gov:"]
    for b in ("within $5", "$5-20 off", "$20-100 off", "over $100 off"):
        n = bands.get(b, 0)
        lines.append(f"   {b:<14} {n:>5}  ({100 * n / max(1, len(priced)):.1f}%)")
    lines.append(f"Deductible-met month agrees: {ded_ok} of {len(ded)}")
    lines.append(f"Not comparable (reported separately): {len(other)}")
    why = Counter(re.split(r":", x)[0] for r in other for x in set(re.split(r":", y)[0] for y in r["excluded_reasons"]))
    for k, v in why.most_common():
        lines.append(f"   {k}: {v}")
    lines.append("")
    lines.append("Worst misses (comparable searches):")
    for r in sorted(priced, key=lambda r: -abs(r["diff"]))[:25]:
        if abs(r["diff"]) <= MATCH:
            break
        lines.append(f"   {r['diff']:+9.2f}  mgov {r['mgov_year']:>9.2f}  {r['plan']}  {r['store'][:24]:<24} {r['file']}"
                     + (f"  unpriced={r['unpriced']}" if r["unpriced"] else "")
                     + (f"  not_covered={r['engine_not_covered']}" if r["engine_not_covered"] else "")
                     + (f"  unread={r['unread']}" if r["unread"] else ""))
    txt = "\n".join(lines)
    open(os.path.join(OUT, "report.txt"), "w", encoding="utf-8").write(txt)
    print(txt)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=0)
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    report() if a.report else run(a.start, a.count)
