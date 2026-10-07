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
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("BUILD_2027_DB") or os.path.join(HERE, "medicare_mn_2027.db")   # override for dry runs
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



# ----------------------------------------------------------------------------------------------
# CMS monthly / quarterly drug files (2026-10-06)
# The monthly file (Oct 15 2026 on) carries the 2027 formularies, tier costs and pharmacy networks.
# The quarterly file adds per-drug prices; 2027 prices arrive in the Q4 file (Jan 20 2027). Until
# then prices are ESTIMATES carried from the newest 2026 quarterly file (step: prices).
# Both files are a zip of zips with pipe-delimited text; columns are read by NAME, so the two
# layouts (and CMS column re-ordering) both work.
# ----------------------------------------------------------------------------------------------
import io
import statistics
import time
import zipfile

WORK = os.path.join(HERE, "cms_files", "_work")
MN_ZIPS = {str(z).zfill(5) for z in range(55001, 56764)}

# Medicare negotiated monthly prices in effect for 2027 (CMS: the 2026 ten continue, 15 new drugs
# start Jan 1 2027). Keys are what agents write; matched like the engine's 2026 list (first word).
NEGOTIATED_2027 = {
    "eliquis": 231.00, "apixaban": 231.00, "xarelto": 197.00, "jardiance": 197.00, "empagliflozin": 197.00,
    "januvia": 113.00, "farxiga": 178.50, "dapagliflozin": 178.50, "entresto": 295.00, "enbrel": 2355.00,
    "etanercept": 2355.00, "imbruvica": 9319.00, "ibrutinib": 9319.00, "stelara": 4695.00,
    "novolog": 119.00, "fiasp": 119.00, "insulin aspart": 119.00,
    "ozempic": 274.00, "rybelsus": 274.00, "wegovy": 274.00, "semaglutide": 274.00,
    "trelegy": 175.00, "xtandi": 7004.00, "enzalutamide": 7004.00, "pomalyst": 8650.00, "ofev": 6350.00,
    "nintedanib": 6350.00, "ibrance": 7871.00, "palbociclib": 7871.00, "linzess": 136.00, "linaclotide": 136.00,
    "calquence": 8600.00, "acalabrutinib": 8600.00, "austedo": 4093.00, "deutetrabenazine": 4093.00,
    "breo": 67.00, "xifaxan": 1000.00, "rifaximin": 1000.00, "vraylar": 770.00, "cariprazine": 770.00,
    "tradjenta": 78.00, "linagliptin": 78.00, "janumet": 80.00, "otezla": 1650.00, "apremilast": 1650.00,
}


def _inner_txt(outer_zip, keyword):
    """Yield (name, open text stream) for every inner .txt whose zip name contains `keyword`.
    Inner zips are extracted once to cms_files/_work/<outer name>/ (random access is needed)."""
    outer = zipfile.ZipFile(outer_zip)
    dest = os.path.join(WORK, os.path.splitext(os.path.basename(outer_zip))[0])
    os.makedirs(dest, exist_ok=True)
    for name in sorted(outer.namelist()):
        low = name.lower()
        if keyword not in low or "sample" in low or not low.endswith(".zip"):
            continue
        local = os.path.join(dest, os.path.basename(name))
        if not os.path.exists(local) or os.path.getsize(local) != outer.getinfo(name).file_size:
            with outer.open(name) as src, open(local + ".part", "wb") as out:
                while True:
                    buf = src.read(1 << 22)
                    if not buf:
                        break
                    out.write(buf)
            os.replace(local + ".part", local)
        inner = zipfile.ZipFile(local)
        for n in inner.namelist():
            if n.lower().endswith(".txt"):
                yield n, io.TextIOWrapper(inner.open(n), encoding="latin-1", newline="")


def _rows(outer_zip, keyword, contracts=None):
    """contracts: optional set of contract IDs; other lines are skipped before splitting (fast path for
    the 20+ GB national pharmacy file - every data line starts with its contract ID)."""
    for _, stream in _inner_txt(outer_zip, keyword):
        header = stream.readline().rstrip("\r\n").split("|")
        idx = {h.strip().upper(): i for i, h in enumerate(header)}
        for line in stream:
            if contracts is not None and line[:5] not in contracts:
                continue
            parts = line.rstrip("\r\n").split("|")
            yield idx, parts


def _get(idx, parts, col, default=""):
    i = idx.get(col)
    return parts[i].strip() if i is not None and i < len(parts) else default


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0               # CMS writes "." or blanks for some fees


def _npi(pharmacy_number):
    """CMS writes "10" + the 10-digit NPI; pharmacy_names (and the 2026 database) use the bare NPI."""
    return pharmacy_number[2:] if len(pharmacy_number) == 12 and pharmacy_number.startswith("10") else pharmacy_number


def _file_label(zip_path):
    """SPUF_2026_20260701.zip -> 'CMS July 2026 file'; 2026_20261015.zip -> 'CMS Oct 2026 file'."""
    import calendar
    m = re.search(r"(\d{4})(\d{2})\d{2}", os.path.basename(zip_path))
    return f"CMS {calendar.month_abbr[int(m.group(2))]} {m.group(1)} file" if m else os.path.basename(zip_path)


def _our_plans(conn):
    return {(c, p) for c, p in conn.execute("SELECT contract_id, plan_id FROM plans")}


def _set_meta(conn, **kv):
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    for k, v in kv.items():
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
    conn.commit()


def step_meta(conn):
    """Year facts the engine reads (app/data_meta.py) + the 2027 negotiated prices."""
    caps = {money(r["Part D Out-of-Pocket (OOP) Threshold"]) for r in landscape_rows()} - {None}
    if len(caps) != 1:
        sys.exit(f"Expected one out-of-pocket cap in the Landscape file, found {caps}")
    _set_meta(conn, data_year=2027, oop_cap=f"{caps.pop():.0f}")
    conn.execute("DROP TABLE IF EXISTS negotiated_prices")
    conn.execute("CREATE TABLE negotiated_prices (drug TEXT PRIMARY KEY, price REAL)")
    conn.executemany("INSERT INTO negotiated_prices VALUES (?,?)", sorted(NEGOTIATED_2027.items()))
    conn.commit()
    print(f"meta: {dict(conn.execute('SELECT key, value FROM meta'))}; negotiated prices: {len(NEGOTIATED_2027)} names")


def step_cms(conn, zip_path, year="2027"):
    """Load a CMS monthly or quarterly file for plan year `year`: official formulary IDs, formularies,
    tier costs, pharmacy networks, and prices when the file has them for that year."""
    t0 = time.time()
    ours = _our_plans(conn)
    # 1. plan information: official formulary ID per plan
    fids, seen_year_plans = {}, set()
    for idx, p in _rows(zip_path, "plan information"):
        key = (_get(idx, p, "CONTRACT_ID"), _get(idx, p, "PLAN_ID").zfill(3))
        if key in ours and _get(idx, p, "FORMULARY_ID"):
            fids[key] = _get(idx, p, "FORMULARY_ID")
    # 2. formulary: keep only rows of the asked-for contract year
    years, rows = set(), []
    wanted_f = set(fids.values())
    for idx, p in _rows(zip_path, "basic drugs formulary"):
        y = _get(idx, p, "CONTRACT_YEAR")
        years.add(y)
        if y == year and _get(idx, p, "FORMULARY_ID") in wanted_f:
            rows.append((_get(idx, p, "FORMULARY_ID"), _get(idx, p, "RXCUI"), _get(idx, p, "NDC"),
                         int(_num(_get(idx, p, "TIER_LEVEL_VALUE"))), "N",
                         _get(idx, p, "QUANTITY_LIMIT_YN"), _get(idx, p, "PRIOR_AUTHORIZATION_YN"),
                         _get(idx, p, "STEP_THERAPY_YN")))
    if year not in years:
        print(f"This file has contract years {sorted(years)}, not {year}. Nothing changed.")
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS formulary (formulary_id TEXT, rxcui TEXT, ndc TEXT, tier INTEGER,
        ded_applies TEXT, quantity_limit TEXT, prior_auth TEXT, step_therapy TEXT, source TEXT,
        PRIMARY KEY (formulary_id, ndc))""")
    conn.execute("DELETE FROM formulary WHERE source='cms'")
    conn.executemany("INSERT OR REPLACE INTO formulary VALUES (?,?,?,?,?,?,?,?,'cms')", rows)
    for key, fid in fids.items():
        conn.execute("UPDATE plans SET formulary_id=?, drug_list_status='loaded' WHERE contract_id=? AND plan_id=?",
                     (fid, key[0], key[1]))
    # carrier-built lists stay only for plans CMS did not cover (e.g. a carrier missing from the file)
    conn.execute("""DELETE FROM formulary WHERE source='carrier' AND formulary_id NOT IN
        (SELECT formulary_id FROM plans WHERE formulary_id IS NOT NULL)""")
    conn.commit()
    print(f"formulary: {len(rows):,} CMS rows for {len(wanted_f)} drug lists ({len(fids)} of {len(ours)} plans)")
    # 3. tier costs (replaces the PBP-derived table)
    bc = []
    for idx, p in _rows(zip_path, "beneficiary cost"):
        key = (_get(idx, p, "CONTRACT_ID"), _get(idx, p, "PLAN_ID").zfill(3))
        if key in ours:
            f = lambda c, cast=float: cast(_num(_get(idx, p, c)))
            bc.append((key[0], key[1], _get(idx, p, "SEGMENT_ID"), _get(idx, p, "COVERAGE_LEVEL"), f("TIER", int),
                       f("DAYS_SUPPLY", int), f("COST_TYPE_PREF", int), f("COST_AMT_PREF"), f("COST_TYPE_NONPREF", int),
                       f("COST_AMT_NONPREF"), f("COST_TYPE_MAIL_PREF", int), f("COST_AMT_MAIL_PREF"),
                       _get(idx, p, "DED_APPLIES_YN") or "Y"))
    if bc:
        conn.execute("DELETE FROM beneficiary_cost")
        conn.executemany("INSERT OR REPLACE INTO beneficiary_cost VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", bc)
        conn.commit()
    print(f"beneficiary_cost: {len(bc):,} CMS rows")
    # 4. pharmacy networks, Minnesota pharmacies only
    net = _load_networks(conn, zip_path, ours)
    print(f"pharmacy_network: {net:,} plan-pharmacy rows")
    # 5. prices, only if this file has the contract year's prices (quarterly Q4 file and later)
    n_price = _load_prices(conn, zip_path, ours, year_check=year) if any(
        "pricing" in n.lower() for n in zipfile.ZipFile(zip_path).namelist()) else 0
    _set_meta(conn, data_vintage=f"{_file_label(zip_path)} ({year} plans)",
              network_estimated=0)
    print(f"prices: {n_price:,} rows from this file" if n_price else "prices: none in this file for this year (keep estimates)")
    print(f"done in {time.time() - t0:.0f}s")


def _load_networks(conn, zip_path, ours, fallback_from=None):
    conn.execute("DROP TABLE IF EXISTS pharmacy_network")
    conn.execute("""CREATE TABLE pharmacy_network (contract_id TEXT, plan_id TEXT, npi TEXT, pharmacy_zip TEXT,
        preferred_retail TEXT, preferred_mail TEXT, is_retail INTEGER, is_mail INTEGER, floor_price REAL,
        brand_fee_30 REAL, generic_fee_30 REAL, selected_fee_30 REAL, PRIMARY KEY (contract_id, plan_id, npi))""")
    n = 0
    batch = []
    contracts = {k[0] for k in ours} | {k[0] for k in (fallback_from or {})}
    for idx, p in _rows(zip_path, "pharmacy networks", contracts):
        z = _get(idx, p, "PHARMACY_ZIPCODE").zfill(5)
        if z not in MN_ZIPS:
            continue
        key = (_get(idx, p, "CONTRACT_ID"), _get(idx, p, "PLAN_ID").zfill(3))
        target = [key] if key in ours else (fallback_from or {}).get(key, [])
        for tk in target:
            fee = lambda c: _num(_get(idx, p, c))
            batch.append((tk[0], tk[1], _npi(_get(idx, p, "PHARMACY_NUMBER")), z, _get(idx, p, "PREFERRED_STATUS_RETAIL"),
                          _get(idx, p, "PREFERRED_STATUS_MAIL"), 1 if _get(idx, p, "PHARMACY_RETAIL") == "Y" else 0,
                          1 if _get(idx, p, "PHARMACY_MAIL") == "Y" else 0, fee("FLOOR_PRICE"),
                          fee("BRAND_DISPENSING_FEE_30"), fee("GENERIC_DISPENSING_FEE_30"),
                          fee("SELECTED_DISPENSING_FEE_30")))
        if len(batch) >= 50000:
            conn.executemany("INSERT OR IGNORE INTO pharmacy_network VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            n += len(batch)
            batch = []
    conn.executemany("INSERT OR IGNORE INTO pharmacy_network VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    n += len(batch)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_net_plan ON pharmacy_network(contract_id, plan_id)")
    conn.commit()
    return n


def _load_prices(conn, zip_path, ours, year_check=None):
    """Prices straight from a file for these exact plans (used when CMS publishes the year's prices)."""
    fid_ndcs = defaultdict(set)
    for fid, ndc in conn.execute("SELECT formulary_id, ndc FROM formulary"):
        fid_ndcs[fid].add(ndc)
    rows = []
    for idx, p in _rows(zip_path, "pricing", {k[0] for k in ours}):
        key = (_get(idx, p, "CONTRACT_ID"), _get(idx, p, "PLAN_ID").zfill(3))
        if key in ours:
            rows.append((key[0], key[1], _get(idx, p, "SEGMENT_ID"), _get(idx, p, "NDC"),
                         int(_num(_get(idx, p, "DAYS_SUPPLY"))), _num(_get(idx, p, "UNIT_COST"))))
    if not rows:
        return 0
    conn.execute("DROP TABLE IF EXISTS pricing")
    conn.execute("""CREATE TABLE pricing (contract_id TEXT, plan_id TEXT, segment_id TEXT, ndc TEXT, days_supply INTEGER,
        unit_cost REAL, estimated INTEGER DEFAULT 0, PRIMARY KEY (contract_id, plan_id, segment_id, ndc, days_supply))""")
    conn.executemany("INSERT OR IGNORE INTO pricing VALUES (?,?,?,?,?,?,0)", rows)
    conn.commit()
    _set_meta(conn, prices_estimated=0, prices_vintage=_file_label(zip_path))
    return len(rows)


# 2027 plan -> 2026 plan to borrow prices / pharmacy network from, when the same plan ID did not
# exist in 2026 (renumbered or new plans). Same plan ID in the 2026 file always wins.
ANALOG_2026 = {
    ("H5959", "019"): ("H5959", "013"), ("H5959", "023"): ("H5959", "013"),      # Essential <- Core $0
    ("H5959", "020"): ("H5959", "009"), ("H5959", "024"): ("H5959", "014"),      # Select <- Choice
    ("H5959", "021"): ("H5959", "010"),                                          # Premier <- Complete
    ("H4882", "015"): ("H4882", "014"), ("H4882", "018"): ("H4882", "014"),      # Journey Smart
    ("H4882", "016"): ("H4882", "009"), ("H4882", "017"): ("H4882", "009"),      # Journey Pace
    ("H8889", "019"): ("H8889", "010"), ("H8889", "020"): ("H8889", "012"),      # Medica Value / Select
    ("H8889", "021"): ("H8889", "011"), ("H8889", "022"): ("H8889", "005"),      # Medica Preferred / Essential
    ("H8889", "023"): ("H8889", "005"), ("H8889", "024"): ("H8889", "015"),
    ("S4802", "158"): ("S4802", "146"),                                          # Wellcare Value Script
    ("S5884", "204"): ("S5884", "190"),                                          # Humana Value Rx
}


def step_carrier_formularies(conn, zip_2026):
    """Before CMS publishes 2027 formularies: load our matched carrier drug lists (formulary_2027/matched)
    and give each drug ID the NDCs (package codes) it had anywhere in the 2026 national file, so prices
    can attach. CMS's own 2027 formularies (step cms) replace these when they arrive."""
    files = {"00027002": "uhc_27002", "00027015": "aetna_27015", "00027115": "healthpartners",
             "00027044": "bcbs_27044", "00027045": "bcbs_27045", "00027233": "align", "00027354": "quartz",
             "00027163": "wellcare_classic_27163_2027", "00027165": "wellcare_valuescript_27165_2027",
             "00027061": "healthspring", "00027147": "medicareblue_27147", "00027001": "aarp_saver_27001",
             "00027000": "aarp_preferred_27000"}
    rx_ndc = defaultdict(set)
    for idx, p in _rows(zip_2026, "basic drugs formulary"):
        rx_ndc[_get(idx, p, "RXCUI")].add(_get(idx, p, "NDC"))
    conn.execute("DROP TABLE IF EXISTS formulary")
    conn.execute("""CREATE TABLE formulary (formulary_id TEXT, rxcui TEXT, ndc TEXT, tier INTEGER, ded_applies TEXT,
        quantity_limit TEXT, prior_auth TEXT, step_therapy TEXT, source TEXT, PRIMARY KEY (formulary_id, ndc))""")
    n_rows, no_ndc = 0, 0
    for fid, base in files.items():
        path = os.path.join(HERE, "formulary_2027", "matched", base + ".json")
        best = {}
        for r in json.load(open(path, encoding="utf-8")):
            for rx in r["rxcuis"]:
                if rx not in best or r["tier"] < best[rx][0]:
                    best[rx] = (r["tier"], r.get("limits", ""))
        for rx, (tier, limits) in best.items():
            ndcs = rx_ndc.get(rx) or {"RX" + rx}          # no 2026 package code: covered, price unknown
            no_ndc += not rx_ndc.get(rx)
            for ndc in ndcs:
                conn.execute("INSERT OR IGNORE INTO formulary VALUES (?,?,?,?,?,?,?,?,'carrier')", (
                    fid, rx, ndc, tier, "N", "Y" if "QL" in limits else "N", "Y" if "PA" in limits else "N",
                    "Y" if re.search(r"\bST\b", limits) else "N"))
                n_rows += 1
    conn.commit()
    print(f"formulary (carrier lists): {n_rows:,} rows for {len(files)} drug lists; drug IDs with no 2026 package code: {no_ndc:,}")


def step_estimates(conn, zip_2026, with_networks=True):
    """2026 prices and pharmacy networks carried into 2027 as ESTIMATES (CMS publishes 2027 prices Jan 20 2027):
    the same plan ID's 2026 price/network, else its ANALOG_2026 plan, else (prices only) the median 2026
    price across every Minnesota plan for that package code."""
    t0 = time.time()
    ours = _our_plans(conn)
    mn_2026, all_2026 = set(), set()
    for idx, p in _rows(zip_2026, "plan information"):
        key = (_get(idx, p, "CONTRACT_ID"), _get(idx, p, "PLAN_ID").zfill(3))
        all_2026.add(key)
        if _get(idx, p, "STATE") == "MN":
            mn_2026.add(key)
    source = {k: (k if k in all_2026 else ANALOG_2026.get(k)) for k in ours}
    source = {k: v for k, v in source.items() if v}
    print(f"2026 source plan found for {len(source)} of {len(ours)} plans; none: "
          f"{sorted(k for k in ours if k not in source)}")
    # networks: route each 2026 source plan's MN pharmacies to the 2027 plans that borrow from it
    fallback = defaultdict(list)
    for k27, k26 in source.items():
        if k26 != k27:
            fallback[k26].append(k27)
    if with_networks:
        same = {k for k, v in source.items() if v == k}
        n_net = _load_networks(conn, zip_2026, same, fallback_from=fallback)
        _set_meta(conn, network_estimated=1)
        print(f"pharmacy_network (2026 carried over): {n_net:,} rows  [{time.time() - t0:.0f}s]")
    # prices: CMS lists a price for SOME package codes (NDCs) of a drug; every NDC of the same drug ID
    # (RxCUI = same ingredient, strength and form) has the same per-unit price, so prices are pooled
    # by drug ID and given to every NDC on the 2027 list.
    ndc_rx = {}
    for idx, p in _rows(zip_2026, "basic drugs formulary"):
        ndc_rx[_get(idx, p, "NDC")] = _get(idx, p, "RXCUI")
    need_rx = {rx for (rx,) in conn.execute("SELECT DISTINCT rxcui FROM formulary")}
    by_plan = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))   # 2026 plan -> rxcui -> days -> [costs]
    pool = defaultdict(lambda: defaultdict(list))                          # rxcui -> days -> [costs, all MN plans]
    wanted_src = set(source.values())
    mn_2026 |= wanted_src                             # drug-only plans are listed by region, not state
    for idx, p in _rows(zip_2026, "pricing", {k[0] for k in mn_2026}):
        key = (_get(idx, p, "CONTRACT_ID"), _get(idx, p, "PLAN_ID").zfill(3))
        if key not in mn_2026:
            continue
        rx = ndc_rx.get(_get(idx, p, "NDC"))
        if rx not in need_rx:
            continue
        days, cost = int(_num(_get(idx, p, "DAYS_SUPPLY"))), _num(_get(idx, p, "UNIT_COST"))
        pool[rx][days].append(cost)
        if key in wanted_src:
            by_plan[key][rx][days].append(cost)
    print(f"2026 prices read for {len(pool):,} drug IDs  [{time.time() - t0:.0f}s]")
    conn.execute("DROP TABLE IF EXISTS pricing")
    conn.execute("""CREATE TABLE pricing (contract_id TEXT, plan_id TEXT, segment_id TEXT, ndc TEXT, days_supply INTEGER,
        unit_cost REAL, estimated INTEGER DEFAULT 1, PRIMARY KEY (contract_id, plan_id, segment_id, ndc, days_supply))""")
    plan_fid = dict(((c, p), f) for c, p, f in conn.execute("SELECT contract_id, plan_id, formulary_id FROM plans"))
    fid_rows = defaultdict(list)
    for fid, rx, ndc in conn.execute("SELECT formulary_id, rxcui, ndc FROM formulary"):
        fid_rows[fid].append((rx, ndc))
    med = lambda d: {days: statistics.median(v) for days, v in d.items()}
    medians = {rx: med(d) for rx, d in pool.items()}
    stats = defaultdict(int)
    for key in sorted(ours):
        own = by_plan.get(source.get(key), {})
        done = set()
        for rx, ndc in fid_rows.get(plan_fid.get(key), ()):
            if rx not in done:
                stats["plan" if rx in own else ("median" if rx in medians else "none")] += 1
                done.add(rx)
            prices = med(own[rx]) if rx in own else medians.get(rx)
            for days, cost in (prices or {}).items():
                conn.execute("INSERT OR IGNORE INTO pricing VALUES (?,?,?,?,?,?,1)", (key[0], key[1], "000", ndc, days, cost))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price ON pricing(contract_id, plan_id, ndc)")
    conn.commit()
    _set_meta(conn, prices_estimated=1, prices_vintage=_file_label(zip_2026),
              **({} if not with_networks else {"data_vintage": "CMS 2027 plan files (Sept 2026) + carrier drug lists"}))
    print(f"pricing (estimates), per plan x drug ID: plan's own 2026 price {stats['plan']:,} | MN median "
          f"{stats['median']:,} | no 2026 price {stats['none']:,}  [{time.time() - t0:.0f}s]")


def step_reference(conn):
    """Pharmacy names/addresses and ZIP coordinates, copied from the 2026 database (not year-specific)."""
    src = os.path.join(HERE, "medicare_mn.db")
    conn.execute(f"ATTACH DATABASE 'file:{src}?mode=ro' AS old")
    for t in ("pharmacy_names", "zip_coords"):
        conn.execute(f"DROP TABLE IF EXISTS main.{t}")
        sql = conn.execute("SELECT sql FROM old.sqlite_master WHERE name=?", (t,)).fetchone()[0]
        conn.execute(sql)
        conn.execute(f"INSERT INTO main.{t} SELECT * FROM old.{t}")
    conn.commit()
    conn.execute("DETACH DATABASE old")
    print("reference: " + ", ".join(f"{t} {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:,}"
                                   for t in ("pharmacy_names", "zip_coords")))


STEPS = {"zip_county": step_zip_county, "service_area": step_service_area, "plans": step_plans, "costs": step_costs,
         "meta": step_meta, "cms": step_cms, "carrier_formularies": step_carrier_formularies,
         "estimates": step_estimates, "reference": step_reference,
         "estimate_prices": lambda conn, z: step_estimates(conn, z, with_networks=False)}

if __name__ == "__main__":
    # python build_2027_db.py                      -> the base steps (zip_county service_area plans costs meta)
    # python build_2027_db.py plans costs          -> just those steps
    # python build_2027_db.py cms <zip> [year]     -> one step with its arguments
    args = sys.argv[1:] or ["zip_county", "service_area", "plans", "costs", "meta"]
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=TRUNCATE")   # never needs to delete its journal file
    try:
        if args[0] in STEPS and len(args) > 1 and args[1] not in STEPS:
            STEPS[args[0]](conn, *args[1:])
        else:
            for s in args:
                if s not in STEPS:
                    sys.exit(f"Unknown step {s}. Steps: {', '.join(STEPS)}")
                STEPS[s](conn)
    finally:
        conn.close()
