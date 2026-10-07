"""READ-ONLY: compare the CY2027 Landscape (MN) with what the engine shows today (2026 data)."""
import csv, sqlite3, collections, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
L27 = os.path.join(ROOT, "cy2027_landscape_202609.1", "CY2027_Landscape_202609.1", "CY2027_Landscape_202609.csv")
db = sqlite3.connect(os.path.join(ROOT, "medicare_mn.db"))

def money(v):
    v = (v or "").replace("$", "").replace(",", "").strip()
    try: return float(v)
    except ValueError: return None

rows = [r for r in csv.DictReader(open(L27, encoding="utf-8-sig", errors="replace"))
        if r["State Territory Abbreviation"].strip() == "MN"]
print("2027 MN rows:", len(rows), "| OOP threshold values:", collections.Counter(r["Part D Out-of-Pocket (OOP) Threshold"] for r in rows).most_common(3))
print("Plan types:", collections.Counter(r["Plan Type"] for r in rows).most_common())
print("Part D indicator:", collections.Counter(r["Part D Coverage Indicator"] for r in rows).most_common())

def eligible27(r):
    pt = r["Plan Type"]
    if r["Contract Category Type"].strip().upper().startswith("PDP") or pt == "Medicare Prescription Drug Plan":
        return "PD"
    if "SNP" in pt or "SNP" in r["Special Needs Plan (SNP) Indicator"] and r["Special Needs Plan (SNP) Indicator"].strip().lower() == "yes":
        return None
    if any(x in pt for x in ("PACE", "MSA")):
        return None
    if r["Part D Coverage Indicator"].strip().lower() != "yes":
        return None
    return "MA"

p27 = {}
county27 = collections.defaultdict(set)
for r in rows:
    k = eligible27(r)
    if not k: continue
    key = (r["Contract ID"].strip(), r["Plan ID"].strip().zfill(3))
    p27.setdefault(key, {"kind": k, "name": r["Plan Name"].strip(), "org": r["Organization Marketing Name"].strip(),
                         "prem": money(r["Monthly Consolidated Premium (Part C + D)"]), "ded": money(r["Annual Part D Deductible Amount"]),
                         "moop": r["In-Network Maximum Out-of-Pocket (MOOP) Amount"].strip(), "type": r["Plan Type"].strip()})
    county27[r["County Name"].strip()].add(key)

# 2026: what the picker can show today (service area + a formulary)
p26 = {}
for cid, pid, name, org, ptype, fid in db.execute("""SELECT sa.contract_id, sa.plan_id, sa.plan_name, sa.org_name, sa.plan_type, p.formulary_id
        FROM service_area sa JOIN plans p ON p.contract_id=sa.contract_id AND p.plan_id=sa.plan_id
        WHERE sa.plan_type NOT IN ('PACE') AND sa.plan_type NOT LIKE '%SNP%' AND p.formulary_id IS NOT NULL"""):
    p26[(cid, str(pid).zfill(3))] = {"name": name, "org": org, "kind": "PD" if ptype == "PDP" else "MA"}
prem26 = {(c, str(p).zfill(3)): (pr, de) for c, p, pr, de in db.execute("SELECT contract_id, plan_id, MIN(premium_total), deductible FROM service_area GROUP BY contract_id, plan_id")}

new, gone, both = sorted(set(p27) - set(p26)), sorted(set(p26) - set(p27)), sorted(set(p27) & set(p26))
print(f"\nPLANS AGENTS COULD PICK — 2026 today: {len(p26)} (MA {sum(v['kind']=='MA' for v in p26.values())}, PD {sum(v['kind']=='PD' for v in p26.values())})"
      f" | 2027: {len(p27)} (MA {sum(v['kind']=='MA' for v in p27.values())}, PD {sum(v['kind']=='PD' for v in p27.values())})")
print(f"Continuing (same plan ID): {len(both)} | New in 2027: {len(new)} | Gone in 2027: {len(gone)}")

print("\n2027 by carrier (MA / PD):")
byorg = collections.defaultdict(lambda: [0, 0])
for v in p27.values(): byorg[v["org"]][0 if v["kind"] == "MA" else 1] += 1
for org, (ma, pd) in sorted(byorg.items(), key=lambda x: -sum(x[1])): print(f"  {org:<55} MA {ma:>2}  PD {pd:>2}")

print("\nNEW in 2027:")
for k in new: v = p27[k]; print(f"  {k[0]}-{k[1]}  {v['kind']}  {v['name']}  (${v['prem']}/mo, ded ${v['ded']})")
print("\nGONE in 2027 (in today's picker, not in the 2027 file):")
for k in gone: print(f"  {k[0]}-{k[1]}  {p26[k]['kind']}  {p26[k]['name']}")
print("\nCONTINUING — name / premium / deductible changes:")
for k in both:
    v, (pr, de) = p27[k], prem26.get(k, (None, None))
    changes = []
    if v["name"].lower() != (p26[k]["name"] or "").lower(): changes.append(f"name '{p26[k]['name']}' -> '{v['name']}'")
    if pr is not None and v["prem"] is not None and abs(pr - v["prem"]) > 0.01: changes.append(f"premium ${pr} -> ${v['prem']}")
    if de is not None and v["ded"] is not None and abs(de - v["ded"]) > 0.01: changes.append(f"deductible ${de} -> ${v['ded']}")
    print(f"  {k[0]}-{k[1]}  " + ("; ".join(changes) if changes else "no change"))

# counties: names must match the zip->county table
zc = {r[0] for r in db.execute("SELECT DISTINCT county_name FROM zip_county")}
c27 = set(county27) - {"All Counties"}
print("\nCounty names in 2027 file not in our ZIP table:", sorted(c27 - zc)[:20])
print("ZIP-table counties with no 2027 rows:", sorted(zc - c27)[:20])

print("\nMA plans per county, 2026 -> 2027 (biggest drops first):")
c26 = collections.defaultdict(set)
for cid, pid, cty in db.execute("""SELECT sa.contract_id, sa.plan_id, sa.county_name FROM service_area sa JOIN plans p
        ON p.contract_id=sa.contract_id AND p.plan_id=sa.plan_id WHERE sa.plan_type NOT IN ('PDP','PACE') AND sa.plan_type NOT LIKE '%SNP%'"""):
    c26[cty].add((cid, str(pid).zfill(3)))
diffs = []
for cty in sorted(c27 | set(c26) - {"All Counties"}):
    a = len(c26.get(cty, set())); b = len([k for k in county27.get(cty, set()) if p27[k]["kind"] == "MA"])
    diffs.append((b - a, cty, a, b))
for d, cty, a, b in sorted(diffs)[:12]: print(f"  {cty:<20} {a:>2} -> {b:>2}  ({d:+d})")
for cty in ("Hennepin", "Anoka", "Ramsey", "Washington", "Dakota", "Wright", "Sherburne", "Olmsted"):
    a = len(c26.get(cty, set())); b = len([k for k in county27.get(cty, set()) if p27[k]["kind"] == "MA"])
    print(f"  metro check {cty:<12} {a:>2} -> {b:>2}")
