"""Build medicare_mn_2027.db, a separate database for plan year 2027. Today's medicare_mn.db is never touched.

Built one step at a time; each step can be re-run on its own:
    python build_2027_db.py zip_county

Steps so far:
  zip_county   : every Minnesota ZIP with ALL the counties it touches and each county's share of the ZIP's land.
                 Source: Census 2020 ZCTA-to-county file (tab20_zcta520_county20_natl.txt in this folder).
  service_area : every Minnesota plan x county with premiums and deductible (CMS 2027 Landscape file).
  plans        : one row per plan with drug coverage, its drug list (formulary) ID and a short label.
  costs        : what the member pays per tier (copay or coinsurance, 30/90-day, preferred/standard, mail)
                 and whether the deductible applies (CMS 2027 Plan Benefit Package files).
"""
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "medicare_mn_2027.db")
CENSUS = os.path.join(HERE, "tab20_zcta520_county20_natl.txt")
MN_FIPS = "27"
LANDSCAPE = os.path.join(HERE, "cy2027_landscape_202609.1", "CY2027_Landscape_202609.1", "CY2027_Landscape_202609.csv")
PBP = os.path.join(HERE, "pbp-benefits-2027")

# Which 2027 drug list each plan uses. Taken from each carrier's 2027 formulary PDF (the plans it names).
# None = the carrier has not published its 2027 list yet; the plan still shows, marked "drug list not loaded".
FORMULARY_2027 = {
    ("H2001", "116"): "00027002", ("H2001", "117"): "00027002", ("H2001", "118"): "00027002", ("H2001", "119"): "00027002",
    ("H3219", "001"): "00027015", ("H3219", "002"): "00027015", ("H3219", "003"): "00027015",
    ("H3219", "004"): "00027015", ("H3219", "008"): "00027015",
    ("H4882", "015"): "00027115", ("H4882", "016"): "00027115", ("H4882", "017"): "00027115", ("H4882", "018"): "00027115",
    ("H5959", "019"): "00027044", ("H5959", "023"): "00027044", ("H2461", "008"): "00027044",
    ("H5959", "020"): "00027045", ("H5959", "021"): "00027045", ("H5959", "024"): "00027045",
    ("H2461", "009"): "00027045", ("H2461", "010"): "00027045",
    ("H3186", "001"): "00027233", ("H3186", "002"): "00027233",
    ("H9834", "001"): "00027354", ("H9834", "003"): "00027354", ("H9834", "006"): "00027354", ("H9834", "007"): "00027354",
    ("S4802", "089"): "00027163", ("S4802", "158"): "00027165",
    ("S5715", "043"): "00027061", ("S5743", "001"): "00027147",
    ("S5921", "370"): "00027001", ("S5921", "406"): "00027000",
    # Medica (H8889, H2450) and Humana (S5884): not published yet
}

# Short labels agents see in the picker (plan names are long). Order matters: first match wins.
LABEL_RULES = [
    (r"^AARP Medicare Advantage from UHC (.+?) \(PPO\)$", r"UHC AARP \1"),
    (r"^AARP Medicare Rx (\w+) from UHC \(PDP\)$", r"AARP Rx \1"),
    (r"^Allina Health Aetna Medicare (.+?) \(PPO\)$", r"Aetna \1"),
    (r"^Blue Cross Medicare Advantage (.+?) \(PPO\)$", r"Blue Cross \1"),
    (r"^Platinum Blue (\w+) Plan with Rx \(Cost\)$", r"Platinum Blue \1 (Cost)"),
    (r"^Medica Advantage (.+?) \(PPO\)$", r"Medica \1"),
    (r"^Medica Prime Solution (\w+) w/Rx \(Cost\)$", r"Medica Cost \1"),
    (r"^Gundersen MN Quartz Med Advantage (\w+) D \(w/Rx\) \(HMO\)$", r"Quartz \1"),
    (r"^(HealthPartners .+?) \(PPO\)$", r"\1"),
    (r"^(Align .+?) \(PPO\)$", r"\1"),
    (r"^(.+?) Plan \(PDP\)$", r"\1"),
    (r"^(.+?) \(PDP\)$", r"\1"),
]


def money(v):
    v = (v or "").strip().replace("$", "").replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None


def landscape_rows():
    if not os.path.exists(LANDSCAPE):
        sys.exit(f"Missing {LANDSCAPE}")
    with open(LANDSCAPE, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["State Territory Abbreviation"].strip() == "MN":
                yield r


def step_service_area(conn):
    conn.execute("DROP TABLE IF EXISTS service_area")
    conn.execute("""CREATE TABLE service_area (
        contract_id TEXT, plan_id TEXT, county_name TEXT, plan_name TEXT,
        premium_c REAL, premium_d REAL, premium_total REAL, deductible REAL,
        plan_type TEXT, org_name TEXT, segment_id TEXT, has_drug_coverage INTEGER,
        PRIMARY KEY (contract_id, plan_id, county_name))""")
    n = 0
    for r in landscape_rows():
        c, d = money(r["Part C Premium"]) or 0.0, money(r["Part D Total Premium"]) or 0.0
        total = money(r["Monthly Consolidated Premium (Part C + D)"])
        if total is None:
            total = c + d
        conn.execute("INSERT OR REPLACE INTO service_area VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            r["Contract ID"].strip(), r["Plan ID"].strip().zfill(3), r["County Name"].strip(), r["Plan Name"].strip(),
            c, d, total, money(r["Annual Part D Deductible Amount"]) or 0.0, r["Plan Type"].strip(),
            r["Organization Marketing Name"].strip(), r["Segment ID"].strip().zfill(3),
            1 if r["Part D Coverage Indicator"].strip().lower() == "yes" else 0))
        n += 1
    conn.execute("CREATE INDEX idx_sa_county ON service_area(county_name)")
    conn.execute("CREATE INDEX idx_sa_plan ON service_area(contract_id, plan_id)")
    conn.commit()
    print(f"service_area: {n} plan-county rows")


def label_for(name):
    for pat, rep in LABEL_RULES:
        if re.match(pat, name):
            return re.sub(pat, rep, name)
    return name


def step_plans(conn):
    plans = {}
    for r in landscape_rows():
        if r["Part D Coverage Indicator"].strip().lower() != "yes" or r["Special Needs Plan (SNP) Indicator"].strip().lower() == "yes":
            continue
        key = (r["Contract ID"].strip(), r["Plan ID"].strip().zfill(3))
        c, d = money(r["Part C Premium"]) or 0.0, money(r["Part D Total Premium"]) or 0.0
        total = money(r["Monthly Consolidated Premium (Part C + D)"])
        total = c + d if total is None else total
        p = plans.setdefault(key, {"name": r["Plan Name"].strip(), "org": r["Organization Marketing Name"].strip(),
                                   "type": "Part D" if r["Plan Type"].strip() == "PDP" else "Medicare Advantage",
                                   "premiums": set(), "deductible": money(r["Annual Part D Deductible Amount"]) or 0.0,
                                   "segments": set()})
        p["premiums"].add(total)
        p["segments"].add(r["Segment ID"].strip().zfill(3))
    labels = {k: label_for(v["name"]) for k, v in plans.items()}
    seen = defaultdict(list)
    for k, lab in labels.items():
        seen[lab].append(k)
    for lab, keys in seen.items():                    # same label twice (e.g. two "Journey Smart"): add the premium
        if len(keys) > 1:
            for k in keys:
                labels[k] = f"{lab} (${min(plans[k]['premiums']):,.0f})"
    conn.execute("DROP TABLE IF EXISTS plans")
    conn.execute("""CREATE TABLE plans (
        contract_id TEXT, plan_id TEXT, segment_id TEXT, carrier TEXT, carrier_name TEXT, plan_name TEXT,
        formulary_id TEXT, premium REAL, deductible REAL, plan_type TEXT, is_default INTEGER DEFAULT 0,
        drug_list_status TEXT,      -- 'loaded' or 'not published yet'
        PRIMARY KEY (contract_id, plan_id, segment_id))""")
    pending = []
    for (cid, pid), p in sorted(plans.items()):
        fid = FORMULARY_2027.get((cid, pid))
        if not fid:
            pending.append(labels[(cid, pid)])
        conn.execute("INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,0,?)", (
            cid, pid, "000", labels[(cid, pid)], p["org"], p["name"], fid, min(p["premiums"]), p["deductible"],
            p["type"], "loaded" if fid else "not published yet"))
    conn.commit()
    print(f"plans: {len(plans)} plans with drug coverage ({sum(1 for p in plans.values() if p['type'] != 'Part D')} MA/Cost, "
          f"{sum(1 for p in plans.values() if p['type'] == 'Part D')} drug-only); drug list not published yet: {len(pending)}")
    for (cid, pid), lab in sorted(labels.items()):
        print(f"   {cid}-{pid}  {lab:38} {plans[(cid, pid)]['name']}")


def pbp_rows(name):
    path = os.path.join(PBP, name)
    if not os.path.exists(path):
        sys.exit(f"Missing {path}")
    csv.field_size_limit(10 ** 9)
    with open(path, encoding="latin-1") as f:
        yield from csv.DictReader(f, delimiter="\t")


def step_costs(conn):
    """Turn the PBP tier table into the engine's beneficiary_cost layout (same codes as the CMS quarterly file):
    coverage_level '1' = initial coverage, '3' = catastrophic ($0); days_supply 1 = one month, 2 = three months;
    cost_type 1 = copay ($), 2 = coinsurance (fraction)."""
    wanted = {(c, p) for c, p in conn.execute("SELECT contract_id, plan_id FROM plans")}
    if not wanted:
        sys.exit("Run the plans step first.")
    no_ded, standard = {}, set()
    for r in pbp_rows("pbp_mrx.txt"):
        key = (r["pbp_a_hnumber"], r["pbp_a_plan_identifier"].zfill(3))
        if key in wanted and r["mrx_benefit_type"].strip() == "1":     # Defined Standard: deductible, then 25%
            standard.add((key, r["segment_id"].zfill(3)))
        if key in wanted:
            flags = r["mrx_alt_no_ded_tier"].strip()      # e.g. "0110000": position n = tier n has no deductible
            no_ded[(key, r["segment_id"].zfill(3))] = {i for i, ch in enumerate(flags) if ch == "1" and i > 0}

    def cost(r, prefix, months):
        cp = (r.get(f"mrx_tier_{prefix}_copay_{months}m") or "").strip()
        co = (r.get(f"mrx_tier_{prefix}_coins_{months}m") or "").strip()
        if cp:
            return 1, float(cp)
        if co:
            return 2, float(co) / 100
        return None

    conn.execute("DROP TABLE IF EXISTS beneficiary_cost")
    conn.execute("""CREATE TABLE beneficiary_cost (
        contract_id TEXT, plan_id TEXT, segment_id TEXT, coverage_level TEXT, tier INTEGER, days_supply INTEGER,
        cost_type_pref INTEGER, cost_amt_pref REAL, cost_type_nonpref INTEGER, cost_amt_nonpref REAL,
        cost_type_mail_pref INTEGER, cost_amt_mail_pref REAL, ded_applies TEXT,
        PRIMARY KEY (contract_id, plan_id, segment_id, coverage_level, tier, days_supply))""")
    n, plans_done, missing = 0, set(), []
    for r in pbp_rows("pbp_mrx_tier.txt"):
        key = (r["pbp_a_hnumber"], r["pbp_a_plan_identifier"].zfill(3))
        if key not in wanted or not r["mrx_tier_id"].strip():
            continue
        seg = r["segment_id"].zfill(3)
        tier = int(r["mrx_tier_id"])
        split = bool(r["mrx_tier_locat_rsplt"].strip())        # plan has preferred + standard retail pharmacies
        ded = "N" if tier in no_ded.get((key, seg), set()) else "Y"
        for months, ds in ((1, 1), (3, 2)):
            pref = cost(r, "rspfd" if split else "rstd", months)
            std = cost(r, "rsstd" if split else "rstd", months)
            mail = cost(r, "mospfd", months) or cost(r, "mosstd", months) or cost(r, "mostd", months)
            if not pref and not std:
                continue
            pref = pref or std
            std = std or pref
            conn.execute("INSERT OR REPLACE INTO beneficiary_cost VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                key[0], key[1], seg, "1", tier, ds, pref[0], pref[1], std[0], std[1],
                mail[0] if mail else 0, mail[1] if mail else 0.0, ded))
            conn.execute("INSERT OR REPLACE INTO beneficiary_cost VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                key[0], key[1], seg, "3", tier, ds, 2, 0.0, 2, 0.0, 2, 0.0, ded))   # catastrophic: $0
            n += 1
        plans_done.add(key)
    for key, seg in sorted(standard):
        if key in plans_done:
            continue
        for tier in range(1, 6):
            for ds in (1, 2):
                conn.execute("INSERT OR REPLACE INTO beneficiary_cost VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (key[0], key[1], seg, "1", tier, ds, 2, 0.25, 2, 0.25, 2, 0.25, "Y"))
                conn.execute("INSERT OR REPLACE INTO beneficiary_cost VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (key[0], key[1], seg, "3", tier, ds, 2, 0.0, 2, 0.0, 2, 0.0, "Y"))
        plans_done.add(key)
        print(f"   {key[0]}-{key[1]}: Defined Standard benefit - deductible, then 25% on every tier")
    conn.commit()
    missing = sorted(wanted - plans_done)
    print(f"beneficiary_cost: {n} tier rows for {len(plans_done)} plans; plans with no tier data: {missing or 'none'}")


def step_zip_county(conn):
    if not os.path.exists(CENSUS):
        sys.exit(f"Missing {CENSUS}. Download it first (see the step notes).")
    parts = defaultdict(list)
    with open(CENSUS, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter="|"):
            if r["GEOID_COUNTY_20"].startswith(MN_FIPS) and r["GEOID_ZCTA5_20"]:
                county = r["NAMELSAD_COUNTY_20"].replace(" County", "")
                parts[r["GEOID_ZCTA5_20"]].append((county, int(r["AREALAND_PART"] or 0)))
    if len(parts) < 800:
        sys.exit(f"Only {len(parts)} Minnesota ZIPs found - the Census file looks wrong. Nothing changed.")
    conn.execute("DROP TABLE IF EXISTS zip_county")
    conn.execute("""CREATE TABLE zip_county (
        zip         TEXT,
        county_name TEXT,
        state       TEXT,
        land_share  REAL,      -- this county's share of the ZIP's land (0-1)
        is_primary  INTEGER,   -- 1 = the county with the most land: the default when nobody picks
        PRIMARY KEY (zip, county_name))""")
    n_multi = 0
    for z, lst in parts.items():
        total = sum(a for _, a in lst) or 1
        top = max(lst, key=lambda x: x[1])[0]
        n_multi += len(lst) > 1
        for county, area in lst:
            conn.execute("INSERT INTO zip_county VALUES (?,?,?,?,?)",
                         (z, county, "MN", round(area / total, 4), 1 if county == top else 0))
    conn.commit()
    print(f"zip_county: {len(parts)} ZIPs, {n_multi} touch more than one county, "
          f"{len({c for l in parts.values() for c, _ in l})} counties")


STEPS = {"zip_county": step_zip_county, "service_area": step_service_area, "plans": step_plans, "costs": step_costs}

if __name__ == "__main__":
    wanted = sys.argv[1:] or list(STEPS)
    conn = sqlite3.connect(DB)
    try:
        for s in wanted:
            if s not in STEPS:
                sys.exit(f"Unknown step {s}. Steps: {', '.join(STEPS)}")
            STEPS[s](conn)
    finally:
        conn.close()
