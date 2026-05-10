"""
quarterly_refresh.py — Medicare DB Quarterly Refresh
=====================================================
Run this every quarter when CMS releases new SPUF data (January, April, July, October).
Also run when the annual Landscape file drops (October/November).

WHAT TO DO:
1. Download new files from CMS (see README_REFRESH.md for exact URLs)
2. Drop them into the refresh_input/ folder in this directory
3. Run: python quarterly_refresh.py
4. Wait ~30-45 minutes (mostly geocoding)
5. Done — Railway will redeploy automatically

WHAT THIS SCRIPT DOES:
- Detects input files automatically (no renaming needed)
- Builds a fresh DB in a temp location (never touches live DB until done)
- Validates the new DB passes all quality checks
- Backs up the current DB
- Uploads new DB to Cloudflare R2
- Triggers Railway redeploy
- Verifies Railway is live with the new data
- Sends Pushover notification with results
"""

import os
import sys
import time
import shutil
import sqlite3
import zipfile
import csv
import json
import math
import subprocess
import urllib.request as urlreq
import urllib.parse as urlparse
import boto3
from botocore.exceptions import ClientError
from datetime import datetime
from pathlib import Path

# ============================================================
# CONFIG — edit these if paths change
# ============================================================
BASE_DIR         = Path(__file__).parent
REFRESH_INPUT    = BASE_DIR / "refresh_input"
TEMP_DIR         = BASE_DIR / "refresh_temp"
OUTPUT_DB        = BASE_DIR / "medicare_mn.db"
BACKUP_DB        = BASE_DIR / "medicare_mn_backup.db"
NEW_DB           = BASE_DIR / "medicare_mn_new.db"

PUSHOVER_TOKEN   = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER    = os.environ.get("PUSHOVER_USER", "")
RAILWAY_TOKEN    = os.environ.get("RAILWAY_TOKEN", "")
RAILWAY_SERVICE  = os.environ.get("RAILWAY_SERVICE_URL", "https://web-production-dcce3.up.railway.app")

# Minnesota zip code range
MN_ZIPS = set(str(z).zfill(5) for z in range(55001, 56764))

# All MN target plans — update this list when new plans are added
TARGET_CONTRACT_PLAN = {
    ("H4882", "009"), ("H4882", "003"), ("H4882", "011"), ("H4882", "014"),
    ("H6309", "001"), ("H6309", "002"),
    ("H5959", "009"), ("H5959", "010"), ("H5959", "011"), ("H5959", "012"),
    ("H5959", "013"), ("H5959", "014"), ("H5959", "015"), ("H5959", "016"),
    ("H6154", "001"),
    ("H8889", "001"), ("H8889", "002"), ("H8889", "003"), ("H8889", "004"),
    ("H8889", "005"), ("H8889", "008"), ("H8889", "010"), ("H8889", "011"),
    ("H8889", "012"), ("H8889", "013"), ("H8889", "014"), ("H8889", "015"),
    ("H8889", "017"), ("H8889", "018"),
    ("H2450", "002"), ("H2450", "007"), ("H2450", "016"), ("H2450", "035"),
    ("H2450", "037"), ("H2450", "039"), ("H2450", "049"),
    ("H5216", "275"), ("H5216", "063"), ("H5216", "092"), ("H5216", "359"),
    ("H8145", "006"),
    ("H3219", "001"), ("H3219", "002"), ("H3219", "003"), ("H3219", "004"),
    ("H3219", "005"), ("H3219", "008"), ("H3219", "012"), ("H3219", "014"),
    ("H2001", "116"), ("H2001", "117"), ("H2001", "118"),
    ("H2001", "119"), ("H2001", "120"), ("H2001", "123"),
    ("H3186", "001"), ("H3186", "002"),
    ("S5884", "190"), ("S5884", "145"), ("S5884", "171"),
    ("S4802", "146"), ("S4802", "089"),
    ("S5601", "050"), ("S5743", "001"),
    ("S5921", "370"), ("S5921", "406"),
}
TARGET_NORMALIZED = {(cid, pid.zfill(3)) for cid, pid in TARGET_CONTRACT_PLAN}

log_lines = []


# ============================================================
# UTILITIES
# ============================================================

def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {level}: {msg}"
    print(line)
    log_lines.append(line)


def abort(msg):
    log(msg, "ERROR")
    notify(f"❌ Refresh FAILED\n{msg}", title="Medicare DB Refresh Failed")
    sys.exit(1)


def notify(message, title="Medicare DB Refresh"):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log("Pushover not configured — skipping notification", "WARN")
        return
    try:
        data = urlparse.urlencode({
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER,
            "title": title,
            "message": message,
        }).encode("utf-8")
        req = urlreq.Request("https://api.pushover.net/1/messages.json",
                             data=data, method="POST")
        with urlreq.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("status") != 1:
                log(f"Pushover send failed: {result}", "WARN")
    except Exception as e:
        log(f"Pushover error: {e}", "WARN")


def haversine_distance(lat1, lon1, lat2, lon2):
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


# ============================================================
# STEP 0: PRE-FLIGHT
# ============================================================

def preflight():
    log("=== STEP 0: Pre-flight checks ===")

    # Check input folder exists
    if not REFRESH_INPUT.exists():
        REFRESH_INPUT.mkdir(parents=True)
        abort(f"refresh_input/ folder was empty. Add your CMS files and re-run.\nSee README_REFRESH.md for download links.")

    files = list(REFRESH_INPUT.iterdir())
    if not files:
        abort("refresh_input/ is empty. Add your CMS files and re-run.\nSee README_REFRESH.md for download links.")

    # Find SPUF zip
    spuf_zip = None
    for f in files:
        if f.suffix.lower() == ".zip" and "SPUF" in f.name.upper() or "spuf" in f.name.lower():
            spuf_zip = f
            break
    if not spuf_zip:
        # Try any zip file
        zips = [f for f in files if f.suffix.lower() == ".zip"]
        if zips:
            spuf_zip = zips[0]
            log(f"Found zip file: {spuf_zip.name} (assuming SPUF)", "WARN")
        else:
            abort("No SPUF zip file found in refresh_input/. Download from CMS and add it.")

    # Find Landscape CSV
    landscape_csv = None
    for f in files:
        if f.suffix.lower() == ".csv" and ("landscape" in f.name.lower() or "CY20" in f.name):
            landscape_csv = f
            break
    if not landscape_csv:
        csvs = [f for f in files if f.suffix.lower() == ".csv"]
        if csvs:
            landscape_csv = csvs[0]
            log(f"Found CSV file: {landscape_csv.name} (assuming Landscape)", "WARN")
        else:
            abort("No Landscape CSV found in refresh_input/. Download from CMS and add it.")

    log(f"SPUF zip: {spuf_zip.name} ({spuf_zip.stat().st_size / 1024 / 1024:.0f} MB)")
    log(f"Landscape CSV: {landscape_csv.name} ({landscape_csv.stat().st_size / 1024:.0f} KB)")

    # Check R2 credentials
    for var in ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME"]:
        if not os.environ.get(var):
            abort(f"Missing environment variable: {var}\nAdd it to your .env file or system environment.")

    # Clean temp dir
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
    TEMP_DIR.mkdir(parents=True)

    log("Pre-flight checks passed.")
    return spuf_zip, landscape_csv


# ============================================================
# STEP 1: EXTRACT SPUF ZIP
# ============================================================

def extract_spuf(spuf_zip):
    log(f"=== STEP 1: Extracting SPUF zip ({spuf_zip.stat().st_size / 1024 / 1024:.0f} MB) ===")
    extract_dir = TEMP_DIR / "spuf"
    extract_dir.mkdir()

    log("Extracting... (this may take a few minutes)")
    start = time.time()
    with zipfile.ZipFile(spuf_zip, "r") as zf:
        zf.extractall(extract_dir)
    log(f"Extracted in {time.time() - start:.0f}s")

    # Auto-detect the pipe-delimited files by scanning for known headers
    file_map = {}
    HEADER_PATTERNS = {
        "plan_info":        ["CONTRACT_ID", "PLAN_ID", "PLAN_TYPE"],
        "formulary":        ["CONTRACT_ID", "PLAN_ID", "RXCUI", "TIER_LEVEL_VALUE"],
        "beneficiary_cost": ["CONTRACT_ID", "PLAN_ID", "TIER", "COST_AMT_PREFERRED"],
        "pricing":          ["CONTRACT_ID", "PLAN_ID", "LABEL_NAME", "AVG_MO_COST_AMT"],
        "pharmacy_network": ["CONTRACT_ID", "PLAN_ID", "NPI", "PHARMACY_ZIPCODE"],
    }

    log("Auto-detecting SPUF files by content...")
    all_txt_files = list(extract_dir.rglob("*.txt"))
    log(f"Found {len(all_txt_files)} .txt files in zip")

    for txt_file in all_txt_files:
        try:
            with open(txt_file, "r", encoding="utf-8", errors="replace") as f:
                header = f.readline().strip().upper()
            cols = set(header.split("|"))
            for file_type, required_cols in HEADER_PATTERNS.items():
                if file_type not in file_map and all(c in cols for c in required_cols):
                    file_map[file_type] = txt_file
                    log(f"  {file_type}: {txt_file.name}")
                    break
        except Exception:
            continue

    missing = [k for k in HEADER_PATTERNS if k not in file_map]
    if missing:
        abort(f"Could not find these SPUF files in zip: {', '.join(missing)}\nCheck that the zip is the correct quarterly SPUF file.")

    log("All SPUF files detected.")
    return file_map


# ============================================================
# STEP 2: BUILD DATABASE
# ============================================================

def build_database(file_map, landscape_csv):
    log("=== STEP 2: Building database ===")
    conn = sqlite3.connect(str(NEW_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    _create_tables(conn)
    _load_plan_info(conn, file_map["plan_info"])
    _load_formulary(conn, file_map["formulary"])
    _load_beneficiary_cost(conn, file_map["beneficiary_cost"])
    _load_pricing(conn, file_map["pricing"])
    _extract_mn_pharmacies(conn, file_map["pharmacy_network"])
    _build_service_area(conn, landscape_csv)

    conn.commit()
    conn.close()
    log("Database build complete.")


def _create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS plans (
            contract_id TEXT, plan_id TEXT, plan_name TEXT, plan_type TEXT,
            formulary_id TEXT, premium REAL, deductible REAL,
            PRIMARY KEY (contract_id, plan_id)
        );
        CREATE TABLE IF NOT EXISTS formulary (
            formulary_id TEXT, rxcui TEXT, name TEXT, tier_level INTEGER,
            prior_auth INTEGER, step_therapy INTEGER, quantity_limit INTEGER,
            PRIMARY KEY (formulary_id, rxcui)
        );
        CREATE TABLE IF NOT EXISTS beneficiary_cost (
            contract_id TEXT, plan_id TEXT, tier INTEGER, cost_type TEXT,
            pref_retail_30 REAL, pref_retail_60 REAL, pref_retail_90 REAL,
            std_retail_30 REAL, std_retail_60 REAL, std_retail_90 REAL,
            mail_order_90 REAL
        );
        CREATE TABLE IF NOT EXISTS pricing (
            contract_id TEXT, plan_id TEXT, formulary_id TEXT,
            rxcui TEXT, name TEXT, days_supply INTEGER, avg_mo_cost REAL
        );
        CREATE TABLE IF NOT EXISTS pharmacy_network (
            contract_id TEXT, plan_id TEXT, npi TEXT,
            pharmacy_zip TEXT, preferred_retail INTEGER, mail_order INTEGER
        );
        CREATE TABLE IF NOT EXISTS pharmacy_names (
            npi TEXT PRIMARY KEY, name TEXT, address TEXT,
            city TEXT, state TEXT, zip TEXT, lat REAL, lon REAL
        );
        CREATE TABLE IF NOT EXISTS zip_coords (
            zip TEXT PRIMARY KEY, lat REAL, lon REAL, city TEXT, state TEXT
        );
        CREATE TABLE IF NOT EXISTS service_area (
            contract_id TEXT, plan_id TEXT, county_name TEXT,
            plan_name TEXT, premium_c REAL, premium_d REAL,
            premium_total REAL, deductible REAL, plan_type TEXT, org_name TEXT,
            PRIMARY KEY (contract_id, plan_id, county_name)
        );
        CREATE TABLE IF NOT EXISTS zip_county (
            zip TEXT PRIMARY KEY, county_name TEXT, state TEXT
        );
    """)
    conn.commit()
    log("Tables created.")


def _load_plan_info(conn, filepath):
    log("Loading plan info...")
    loaded = 0
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        header = f.readline().strip().upper()
        cols = header.split("|")
        idx = {c: i for i, c in enumerate(cols)}

        for line in f:
            parts = line.strip().split("|")
            if len(parts) < 5:
                continue
            cid = parts[idx.get("CONTRACT_ID", 0)].strip()
            pid = parts[idx.get("PLAN_ID", 1)].strip().zfill(3)
            if (cid, pid) not in TARGET_NORMALIZED:
                continue
            conn.execute("INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?,?,?)", (
                cid, pid,
                parts[idx.get("PLAN_NAME", 2)].strip() if "PLAN_NAME" in idx else "",
                parts[idx.get("PLAN_TYPE", 3)].strip() if "PLAN_TYPE" in idx else "",
                parts[idx.get("FORMULARY_ID", 4)].strip() if "FORMULARY_ID" in idx else "",
                0.0, 0.0  # premiums/deductibles come from Landscape
            ))
            loaded += 1
    conn.commit()
    log(f"  Loaded {loaded} plan records")


def _load_formulary(conn, filepath):
    log("Loading formulary (drug tiers)...")
    loaded = 0
    formulary_ids = set(r[0] for r in conn.execute("SELECT formulary_id FROM plans WHERE formulary_id != ''").fetchall())

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        header = f.readline().strip().upper()
        cols = header.split("|")
        idx = {c: i for i, c in enumerate(cols)}

        for line in f:
            parts = line.strip().split("|")
            if len(parts) < 4:
                continue
            fid = parts[idx.get("FORMULARY_ID", 0)].strip() if "FORMULARY_ID" in idx else ""
            if fid not in formulary_ids:
                continue
            try:
                conn.execute("INSERT OR REPLACE INTO formulary VALUES (?,?,?,?,?,?,?)", (
                    fid,
                    parts[idx.get("RXCUI", 1)].strip() if "RXCUI" in idx else "",
                    parts[idx.get("LABEL_NAME", 2)].strip() if "LABEL_NAME" in idx else "",
                    int(parts[idx.get("TIER_LEVEL_VALUE", 3)].strip() or 0) if "TIER_LEVEL_VALUE" in idx else 0,
                    int(parts[idx.get("PRIOR_AUTH", 4)].strip() or 0) if "PRIOR_AUTH" in idx else 0,
                    int(parts[idx.get("STEP_THERAPY_APPLIES", 5)].strip() or 0) if "STEP_THERAPY_APPLIES" in idx else 0,
                    int(parts[idx.get("QUANTITY_LIMIT_APPLIES", 6)].strip() or 0) if "QUANTITY_LIMIT_APPLIES" in idx else 0,
                ))
                loaded += 1
            except (ValueError, IndexError):
                continue

    conn.commit()
    log(f"  Loaded {loaded} formulary records")


def _load_beneficiary_cost(conn, filepath):
    log("Loading beneficiary cost (copays)...")
    loaded = 0
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        header = f.readline().strip().upper()
        cols = header.split("|")
        idx = {c: i for i, c in enumerate(cols)}

        for line in f:
            parts = line.strip().split("|")
            if len(parts) < 5:
                continue
            cid = parts[idx.get("CONTRACT_ID", 0)].strip()
            pid = parts[idx.get("PLAN_ID", 1)].strip().zfill(3)
            if (cid, pid) not in TARGET_NORMALIZED:
                continue

            def safe_float(col_name, default=0.0):
                if col_name not in idx:
                    return default
                val = parts[idx[col_name]].strip().replace("$", "").replace(",", "")
                try:
                    return float(val) if val else default
                except ValueError:
                    return default

            conn.execute("INSERT INTO beneficiary_cost VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                cid, pid,
                int(parts[idx.get("TIER", 2)].strip() or 0) if "TIER" in idx else 0,
                parts[idx.get("COST_TYPE", 3)].strip() if "COST_TYPE" in idx else "",
                safe_float("PREF_RETAIL_30_AMT"),
                safe_float("PREF_RETAIL_60_AMT"),
                safe_float("PREF_RETAIL_90_AMT"),
                safe_float("STD_RETAIL_30_AMT"),
                safe_float("STD_RETAIL_60_AMT"),
                safe_float("STD_RETAIL_90_AMT"),
                safe_float("MAIL_90_AMT"),
            ))
            loaded += 1

    conn.commit()
    log(f"  Loaded {loaded} beneficiary cost records")


def _load_pricing(conn, filepath):
    log("Loading pricing (monthly drug costs)...")
    loaded = 0
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        header = f.readline().strip().upper()
        cols = header.split("|")
        idx = {c: i for i, c in enumerate(cols)}

        for line in f:
            parts = line.strip().split("|")
            if len(parts) < 5:
                continue
            cid = parts[idx.get("CONTRACT_ID", 0)].strip()
            pid = parts[idx.get("PLAN_ID", 1)].strip().zfill(3)
            if (cid, pid) not in TARGET_NORMALIZED:
                continue

            try:
                avg_cost_str = parts[idx.get("AVG_MO_COST_AMT", 5)].strip() if "AVG_MO_COST_AMT" in idx else "0"
                avg_cost = float(avg_cost_str.replace("$", "").replace(",", "") or 0)
                conn.execute("INSERT INTO pricing VALUES (?,?,?,?,?,?,?)", (
                    cid, pid,
                    parts[idx.get("FORMULARY_ID", 2)].strip() if "FORMULARY_ID" in idx else "",
                    parts[idx.get("RXCUI", 3)].strip() if "RXCUI" in idx else "",
                    parts[idx.get("LABEL_NAME", 4)].strip() if "LABEL_NAME" in idx else "",
                    int(parts[idx.get("DAYS_SUPPLY", 6)].strip() or 0) if "DAYS_SUPPLY" in idx else 0,
                    avg_cost,
                ))
                loaded += 1
            except (ValueError, IndexError):
                continue

    conn.execute("CREATE INDEX IF NOT EXISTS idx_pricing_plan ON pricing(contract_id, plan_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pricing_rxcui ON pricing(rxcui)")
    conn.commit()
    log(f"  Loaded {loaded} pricing records")


def _extract_mn_pharmacies(conn, filepath):
    log("Extracting MN pharmacy network (filtering to MN zips and target plans)...")
    loaded = 0
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        header = f.readline().strip().upper()
        cols = header.split("|")
        idx = {c: i for i, c in enumerate(cols)}

        for line in f:
            parts = line.strip().split("|")
            if len(parts) < 5:
                continue
            cid = parts[idx.get("CONTRACT_ID", 0)].strip()
            pid = parts[idx.get("PLAN_ID", 1)].strip().zfill(3)
            if (cid, pid) not in TARGET_NORMALIZED:
                continue
            pharm_zip = parts[idx.get("PHARMACY_ZIPCODE", 2)].strip().zfill(5) if "PHARMACY_ZIPCODE" in idx else ""
            if pharm_zip not in MN_ZIPS:
                continue

            npi = parts[idx.get("NPI", 3)].strip() if "NPI" in idx else ""
            preferred = int(parts[idx.get("PREFERRED_RETAIL", 4)].strip() or 0) if "PREFERRED_RETAIL" in idx else 0
            mail = int(parts[idx.get("MAIL_ORDER", 5)].strip() or 0) if "MAIL_ORDER" in idx else 0

            conn.execute("INSERT INTO pharmacy_network VALUES (?,?,?,?,?,?)",
                         (cid, pid, npi, pharm_zip, preferred, mail))
            loaded += 1

    conn.execute("CREATE INDEX IF NOT EXISTS idx_pn_plan ON pharmacy_network(contract_id, plan_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pn_npi ON pharmacy_network(npi)")
    conn.commit()
    log(f"  Loaded {loaded} MN pharmacy network records")


def _build_service_area(conn, landscape_csv):
    log("Building service area from Landscape CSV...")
    loaded = 0
    COUNTY_FIXES = {"St. Louis": "St. Louis", "Lac qui Parle": "Lac qui Parle",
                    "Lake of the Woods": "Lake of the Woods"}

    with open(landscape_csv, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("State Territory Abbreviation", "").strip() != "MN":
                continue

            cid = row.get("Contract ID", "").strip()
            pid = row.get("Plan ID", "").strip().zfill(3)
            county = COUNTY_FIXES.get(row.get("County Name", "").strip(),
                                      row.get("County Name", "").strip())

            def clean_amt(val):
                val = str(val).strip().replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
                try:
                    return float(val) if val not in ("", "Not Applicable") else 0.0
                except Exception:
                    return 0.0

            premium_c     = clean_amt(row.get("Part C Premium", "0"))
            premium_d     = clean_amt(row.get("Part D Total Premium", "0"))
            premium_total = clean_amt(row.get("Monthly Consolidated Premium (Part C + D)", "0"))
            deductible    = clean_amt(row.get("Annual Part D Deductible Amount", "0"))
            if premium_total == 0:
                premium_total = premium_c + premium_d

            conn.execute("INSERT OR IGNORE INTO service_area VALUES (?,?,?,?,?,?,?,?,?,?)", (
                cid, pid, county,
                row.get("Plan Name", "").strip(),
                premium_c, premium_d, premium_total, deductible,
                row.get("Plan Type", "").strip(),
                row.get("Organization Marketing Name", "").strip(),
            ))

            # Update premium/deductible in plans table
            conn.execute("""
                UPDATE plans SET premium=?, deductible=?, plan_name=COALESCE(NULLIF(plan_name,''),?)
                WHERE contract_id=? AND plan_id=?
            """, (premium_total, deductible, row.get("Plan Name", "").strip(), cid, pid))

            loaded += 1

    conn.execute("CREATE INDEX IF NOT EXISTS idx_sa_county ON service_area(county_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sa_plan ON service_area(contract_id, plan_id)")
    conn.commit()
    log(f"  Loaded {loaded} service area rows")


# ============================================================
# STEP 3: PHARMACY NAMES (NPI REGISTRY)
# ============================================================

def build_pharmacy_names(skip_existing=True):
    log("=== STEP 3: Looking up pharmacy names from NPI Registry ===")
    conn = sqlite3.connect(str(NEW_DB))

    # Get unique NPI codes from pharmacy_network
    all_npis = set(r[0] for r in conn.execute("SELECT DISTINCT npi FROM pharmacy_network WHERE npi != ''").fetchall())
    log(f"  Total unique NPIs in network: {len(all_npis)}")

    if skip_existing:
        existing_npis = set(r[0] for r in conn.execute("SELECT npi FROM pharmacy_names").fetchall())
        npis_to_lookup = all_npis - existing_npis
        log(f"  Already have {len(existing_npis)} — looking up {len(npis_to_lookup)} new ones")
    else:
        npis_to_lookup = all_npis
        log(f"  Looking up all {len(npis_to_lookup)} NPIs")

    # Get unique zip codes for batch lookup
    zip_codes = set(r[0] for r in conn.execute("SELECT DISTINCT pharmacy_zip FROM pharmacy_network WHERE pharmacy_zip != ''").fetchall())
    log(f"  Searching {len(zip_codes)} zip codes via NPI Registry...")

    found = 0
    not_found = 0
    all_pharmacies = {}

    for i, zip_code in enumerate(sorted(zip_codes)):
        try:
            params = urlparse.urlencode({
                "version": "2.1",
                "taxonomy_description": "pharmacy",
                "postal_code": zip_code,
                "state": "MN",
                "limit": "200"
            })
            url = "https://npiregistry.cms.hhs.gov/api/?" + params
            req = urlreq.Request(url, headers={"User-Agent": "MedicareDrugEngine/1.0"})
            with urlreq.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            for result in data.get("results", []):
                npi = result.get("number", "")
                if npi not in npis_to_lookup or npi in all_pharmacies:
                    continue
                basic = result.get("basic", {})
                name = basic.get("organization_name", "") or \
                       (basic.get("first_name", "") + " " + basic.get("last_name", "")).strip()
                addresses = result.get("addresses", [])
                practice = next((a for a in addresses if a.get("address_purpose") == "LOCATION"), None)
                if not practice and addresses:
                    practice = addresses[0]
                if npi and name:
                    all_pharmacies[npi] = {
                        "npi": npi, "name": name,
                        "address": practice.get("address_1", "") if practice else "",
                        "city": practice.get("city", "") if practice else "",
                        "state": practice.get("state", "MN") if practice else "MN",
                        "zip": practice.get("postal_code", zip_code)[:5] if practice else zip_code,
                    }
                    found += 1

        except Exception:
            not_found += 1

        if (i + 1) % 20 == 0:
            log(f"  {i+1}/{len(zip_codes)} zips done — {found} pharmacies found")

        time.sleep(0.2)

    # Insert into DB
    for p in all_pharmacies.values():
        conn.execute("""
            INSERT OR IGNORE INTO pharmacy_names (npi, name, address, city, state, zip)
            VALUES (?,?,?,?,?,?)
        """, (p["npi"], p["name"], p["address"], p["city"], p["state"], p["zip"]))

    conn.commit()
    total_in_db = conn.execute("SELECT COUNT(*) FROM pharmacy_names").fetchone()[0]
    conn.close()
    log(f"  Done. {found} new pharmacies added. Total in DB: {total_in_db}")


# ============================================================
# STEP 4: GEOCODE PHARMACIES
# ============================================================

def geocode_pharmacies(skip_existing=True):
    log("=== STEP 4: Geocoding pharmacies (Census API) ===")
    conn = sqlite3.connect(str(NEW_DB))

    if skip_existing:
        rows = conn.execute("""
            SELECT npi, name, address, city, state, zip FROM pharmacy_names
            WHERE (lat IS NULL OR lat = 0) AND address != '' AND address IS NOT NULL
        """).fetchall()
    else:
        rows = conn.execute(
            "SELECT npi, name, address, city, state, zip FROM pharmacy_names WHERE address != ''"
        ).fetchall()

    total = len(rows)
    log(f"  {total} pharmacies to geocode. Estimated time: ~{total * 0.4 / 60:.0f} minutes")

    found = 0
    not_found = 0

    for i, (npi, name, address, city, state, zip_code) in enumerate(rows):
        try:
            params = urlparse.urlencode({
                "street": address, "city": city, "state": state or "MN",
                "zip": zip_code, "benchmark": "2020", "format": "json"
            })
            url = "https://geocoding.geo.census.gov/geocoder/locations/address?" + params
            req = urlreq.Request(url, headers={"User-Agent": "MedicareDrugEngine/1.0"})
            with urlreq.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            matches = data.get("result", {}).get("addressMatches", [])
            if matches:
                coords = matches[0]["coordinates"]
                lat, lon = float(coords["y"]), float(coords["x"])
                conn.execute("UPDATE pharmacy_names SET lat=?, lon=? WHERE npi=?", (lat, lon, npi))
                found += 1
            else:
                not_found += 1
        except Exception:
            not_found += 1

        if (i + 1) % 100 == 0:
            conn.commit()
            pct = (i + 1) / total * 100
            log(f"  {i+1}/{total} ({pct:.0f}%) — {found} geocoded, {not_found} not found")

        time.sleep(0.35)

    conn.commit()
    geocoded_total = conn.execute(
        "SELECT COUNT(*) FROM pharmacy_names WHERE lat IS NOT NULL AND lat != 0"
    ).fetchone()[0]
    all_total = conn.execute("SELECT COUNT(*) FROM pharmacy_names").fetchone()[0]
    conn.close()
    log(f"  Done. {geocoded_total}/{all_total} pharmacies have coordinates ({geocoded_total/max(all_total,1)*100:.1f}%)")


# ============================================================
# STEP 5: ZIP TABLES
# ============================================================

def build_zip_county():
    log("=== STEP 5a: Building zip→county from Census ===")
    conn = sqlite3.connect(str(NEW_DB))

    MN_COUNTY_FIPS = {
        "001": "Aitkin", "003": "Anoka", "005": "Becker", "007": "Beltrami",
        "009": "Benton", "011": "Big Stone", "013": "Blue Earth", "015": "Brown",
        "017": "Carlton", "019": "Carver", "021": "Cass", "023": "Chippewa",
        "025": "Chisago", "027": "Clay", "029": "Clearwater", "031": "Cook",
        "033": "Cottonwood", "035": "Crow Wing", "037": "Dakota", "039": "Dodge",
        "041": "Douglas", "043": "Faribault", "045": "Fillmore", "047": "Freeborn",
        "049": "Goodhue", "051": "Grant", "053": "Hennepin", "055": "Houston",
        "057": "Hubbard", "059": "Isanti", "061": "Itasca", "063": "Jackson",
        "065": "Kanabec", "067": "Kandiyohi", "069": "Kittson", "071": "Koochiching",
        "073": "Lac qui Parle", "075": "Lake", "077": "Lake of the Woods",
        "079": "Le Sueur", "081": "Lincoln", "083": "Lyon", "085": "McLeod",
        "087": "Mahnomen", "089": "Marshall", "091": "Martin", "093": "Meeker",
        "095": "Mille Lacs", "097": "Morrison", "099": "Mower", "101": "Murray",
        "103": "Nicollet", "105": "Nobles", "107": "Norman", "109": "Olmsted",
        "111": "Otter Tail", "113": "Pennington", "115": "Pine", "117": "Pipestone",
        "119": "Polk", "121": "Pope", "123": "Ramsey", "125": "Red Lake",
        "127": "Redwood", "129": "Renville", "131": "Rice", "133": "Rock",
        "135": "Roseau", "137": "St. Louis", "139": "Scott", "141": "Sherburne",
        "143": "Sibley", "145": "Stearns", "147": "Steele", "149": "Stevens",
        "151": "Swift", "153": "Todd", "155": "Traverse", "157": "Wabasha",
        "159": "Wadena", "161": "Waseca", "163": "Washington", "165": "Watonwan",
        "167": "Wilkin", "169": "Winona", "171": "Wright", "173": "Yellow Medicine",
    }

    url = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"
    try:
        req = urlreq.Request(url, headers={"User-Agent": "MedicareDrugEngine/1.0"})
        with urlreq.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        reader = csv.DictReader(raw.splitlines(), delimiter="|")
        loaded = 0
        for row in reader:
            zip_code = row.get("GEOID_ZCTA5_20", "").strip().zfill(5)
            county_geoid = row.get("GEOID_COUNTY_20", "").strip()
            if len(county_geoid) < 5 or county_geoid[:2] != "27":
                continue
            county_name = MN_COUNTY_FIPS.get(county_geoid[2:])
            if not county_name:
                continue
            conn.execute("INSERT OR REPLACE INTO zip_county VALUES (?,?,?)", (zip_code, county_name, "MN"))
            loaded += 1
        conn.commit()
        log(f"  Loaded {loaded} zip→county mappings")
    except Exception as e:
        log(f"  Census download failed: {e}. Zip→county may be incomplete.", "WARN")

    conn.close()


def build_zip_coords():
    log("=== STEP 5b: Building zip coordinates ===")
    conn = sqlite3.connect(str(NEW_DB))

    all_mn_zips = set(str(z).zfill(5) for z in range(55001, 56764))
    existing = set(r[0] for r in conn.execute("SELECT zip FROM zip_coords").fetchall())
    remaining = sorted(all_mn_zips - existing)
    log(f"  {len(existing)} already have coords. Fetching {len(remaining)} more...")

    found = 0
    for i, zip_code in enumerate(remaining):
        try:
            url = f"https://api.zippopotam.us/us/{zip_code}"
            req = urlreq.Request(url, headers={"User-Agent": "MedicareDrugEngine/1.0"})
            with urlreq.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            places = data.get("places", [])
            if places:
                conn.execute("INSERT OR REPLACE INTO zip_coords VALUES (?,?,?,?,?)", (
                    zip_code,
                    float(places[0]["latitude"]),
                    float(places[0]["longitude"]),
                    places[0]["place name"],
                    places[0]["state abbreviation"],
                ))
                found += 1
        except Exception:
            pass

        if (i + 1) % 200 == 0:
            conn.commit()
            log(f"  {i+1}/{len(remaining)} done ({found} found)")

        time.sleep(0.1)

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM zip_coords").fetchone()[0]
    conn.close()
    log(f"  Done. Total zip coords: {total}")


# ============================================================
# STEP 6: VALIDATE
# ============================================================

def validate_new_db():
    log("=== STEP 6: Validating new database ===")
    # Import and run validation
    sys.path.insert(0, str(BASE_DIR))
    os.environ["DB_PATH"] = str(NEW_DB)

    try:
        import validate_db
        passed, results = validate_db.run_validation(db_path=str(NEW_DB), previous_db_path=str(OUTPUT_DB) if OUTPUT_DB.exists() else None)
        for line in results:
            if line.strip():
                log(f"  {line}")
        if not passed:
            abort("Database validation failed. See errors above. DB has NOT been deployed.")
        log("Validation passed.")
    except ImportError:
        log("validate_db.py not found — skipping detailed validation", "WARN")
        # Basic check
        conn = sqlite3.connect(str(NEW_DB))
        plan_count = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        conn.close()
        if plan_count < 50:
            abort(f"Only {plan_count} plans in new DB — aborting.")


# ============================================================
# STEP 7: UPLOAD TO R2
# ============================================================

def upload_to_r2():
    log("=== STEP 7: Uploading to Cloudflare R2 ===")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    bucket = os.environ["R2_BUCKET_NAME"]
    size_mb = NEW_DB.stat().st_size / 1024 / 1024
    log(f"  Uploading {size_mb:.1f} MB to R2...")

    start = time.time()
    client.upload_file(
        str(NEW_DB), bucket, "medicare_mn.db",
        ExtraArgs={"ContentType": "application/octet-stream"}
    )
    elapsed = time.time() - start
    log(f"  Upload complete in {elapsed:.1f}s")

    # Also upload a timestamped backup
    quarter = datetime.now().strftime("%Y_Q") + str((datetime.now().month - 1) // 3 + 1)
    backup_key = f"backups/medicare_mn_{quarter}.db"
    try:
        client.upload_file(str(NEW_DB), bucket, backup_key)
        log(f"  Backup saved to R2: {backup_key}")
    except Exception as e:
        log(f"  Backup upload failed (non-fatal): {e}", "WARN")


# ============================================================
# STEP 8: SWAP DB AND GIT PUSH
# ============================================================

def swap_and_push():
    log("=== STEP 8: Swapping DB and pushing to GitHub ===")

    # Backup current DB
    if OUTPUT_DB.exists():
        shutil.copy2(str(OUTPUT_DB), str(BACKUP_DB))
        log(f"  Backed up current DB to {BACKUP_DB.name}")

    # Swap
    shutil.copy2(str(NEW_DB), str(OUTPUT_DB))
    log(f"  New DB in place ({OUTPUT_DB.stat().st_size / 1024 / 1024:.1f} MB)")

    # Git push
    quarter = datetime.now().strftime("%Y Q") + str((datetime.now().month - 1) // 3 + 1)
    try:
        subprocess.run(["git", "-C", str(BASE_DIR), "add", "medicare_mn.db"], check=True)
        subprocess.run(["git", "-C", str(BASE_DIR), "commit", "-m",
                        f"DB refresh: {quarter} quarterly update"], check=True)
        subprocess.run(["git", "-C", str(BASE_DIR), "push"], check=True)
        log("  Git push successful. Railway will redeploy automatically.")
    except subprocess.CalledProcessError as e:
        abort(f"Git push failed: {e}\nDB has been updated locally and uploaded to R2, but Railway was not triggered.")


# ============================================================
# STEP 9: VERIFY RAILWAY DEPLOYMENT
# ============================================================

def verify_railway():
    log("=== STEP 9: Waiting for Railway to redeploy ===")
    max_wait = 300  # 5 minutes
    interval = 20
    elapsed = 0

    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval
        try:
            req = urlreq.Request(f"{RAILWAY_SERVICE}/health",
                                 headers={"User-Agent": "MedicareDrugEngine/1.0"})
            with urlreq.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if data.get("status") == "ok":
                plan_count = data.get("plans", 0)
                zip_county_rows = data.get("zip_county_rows", 0)
                zip_county = data.get("zip_55309_county", "")

                log(f"  Railway up: {plan_count} plans, {zip_county_rows} zip-county rows, 55309→{zip_county}")

                if plan_count >= 60 and zip_county_rows >= 800 and zip_county == "Sherburne":
                    log("  Railway verification passed.")
                    return True
                else:
                    log("  Railway responding but data looks stale — waiting for redeploy...", "WARN")
        except Exception as e:
            log(f"  Railway not ready yet ({elapsed}s elapsed): {e}")

    log("Railway did not redeploy within 5 minutes.", "WARN")
    return False


# ============================================================
# STEP 10: SMOKE TEST
# ============================================================

def smoke_test():
    log("=== STEP 10: Running smoke test (Jack Reacher SOA) ===")
    try:
        body = json.dumps({
            "client_name": "Jack Reacher",
            "dob": "08/16/1944",
            "zip_code": "55309",
            "soa_date": "02/05/2026",
            "confidence": "0.95",
            "client_address": "500 Shannon Dr",
            "client_city": "Big Lake",
            "client_state": "MN",
            "drug_names": "Xarelto,Metformin,Atorvastatin",
            "drug_dosages": "20mg,500mg,20mg",
        }).encode("utf-8")

        req = urlreq.Request(
            f"{RAILWAY_SERVICE}/process-soa",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "MedicareDrugEngine/1.0"},
            method="POST"
        )
        with urlreq.urlopen(req, timeout=60) as resp:
            content_type = resp.headers.get("Content-Type", "")
            content = resp.read()

        if "application/pdf" in content_type and len(content) > 10000:
            log(f"  Smoke test passed: PDF returned ({len(content) / 1024:.0f} KB)")
            return True
        else:
            log(f"  Smoke test failed: unexpected response ({content_type}, {len(content)} bytes)", "WARN")
            return False

    except Exception as e:
        log(f"  Smoke test failed: {e}", "WARN")
        return False


# ============================================================
# STEP 11: CLEANUP
# ============================================================

def cleanup():
    log("=== STEP 11: Cleaning up temp files ===")
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
        log(f"  Removed {TEMP_DIR.name}/")
    if NEW_DB.exists():
        NEW_DB.unlink()
        log(f"  Removed {NEW_DB.name}")
    log("Cleanup complete.")


# ============================================================
# MAIN
# ============================================================

def main():
    start_time = time.time()
    quarter = datetime.now().strftime("%Y Q") + str((datetime.now().month - 1) // 3 + 1)

    print("\n" + "="*60)
    print(f"  Medicare DB Quarterly Refresh — {quarter}")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60 + "\n")

    notify(f"🔄 Quarterly refresh started ({quarter})", title="Medicare DB Refresh")

    try:
        # Run all steps
        spuf_zip, landscape_csv = preflight()
        file_map = extract_spuf(spuf_zip)
        build_database(file_map, landscape_csv)
        build_pharmacy_names(skip_existing=True)
        geocode_pharmacies(skip_existing=True)
        build_zip_county()
        build_zip_coords()
        validate_new_db()
        upload_to_r2()
        swap_and_push()
        railway_ok = verify_railway()
        smoke_ok = smoke_test() if railway_ok else False
        cleanup()

        elapsed = time.time() - start_time
        minutes = elapsed / 60

        if railway_ok and smoke_ok:
            summary = (f"✅ Refresh complete ({quarter})\n"
                       f"Time: {minutes:.0f} min\n"
                       f"Railway: live\nSmoke test: passed")
            log(f"\n{'='*60}")
            log(f"  REFRESH COMPLETE in {minutes:.0f} minutes")
            log(f"  Railway: live | Smoke test: passed")
            log(f"{'='*60}\n")
        else:
            summary = (f"⚠️ Refresh done but verify Railway ({quarter})\n"
                       f"Railway OK: {railway_ok} | Smoke test: {smoke_ok}")
            log(f"\nRefresh done but Railway verification incomplete. Check manually.", "WARN")

        notify(summary, title="Medicare DB Refresh")

    except SystemExit:
        raise
    except Exception as e:
        abort(f"Unexpected error: {e}")


if __name__ == "__main__":
    # Load .env file if present
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())

    main()
