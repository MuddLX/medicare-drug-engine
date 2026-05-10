"""
validate_db.py — Medicare DB Validator
Runs after quarterly_refresh.py builds a new DB, and again after Railway deploys.
Can also be run standalone: python validate_db.py

Exit code 0 = all checks passed
Exit code 1 = one or more checks failed
"""

import sqlite3
import sys
import os

DB_PATH = os.environ.get("DB_PATH", "medicare_mn.db")

# Minimum acceptable row counts
MIN_COUNTS = {
    "plans":            60,
    "formulary":        10000,
    "beneficiary_cost": 1000,
    "pricing":          5000,
    "pharmacy_network": 50000,
    "pharmacy_names":   1000,
    "zip_coords":       1000,
    "service_area":     1000,
    "zip_county":       800,
}

# Plans that must exist with a valid formulary_id
REQUIRED_PLANS = [
    ("H4882", "009"),  # HealthPartners Pace
    ("H4882", "011"),  # HealthPartners Stride
    ("H5959", "013"),  # Blue Cross Core
    ("H5959", "015"),  # Blue Cross Comfort
    ("H6154", "001"),  # Medica Advantage
    ("H5216", "275"),  # Humana Choice
    ("H3219", "001"),  # Aetna Signature
    ("S5884", "190"),  # Humana Value Rx
    ("S4802", "146"),  # WellCare Value Script
]

# Drugs that must be in the formulary
REQUIRED_DRUGS = ["rivaroxaban", "metformin", "atorvastatin", "insulin glargine",
                  "metoprolol", "levothyroxine", "warfarin", "amlodipine"]

# Key zip codes that must resolve to correct counties
ZIP_COUNTY_CHECKS = {
    "55309": "Sherburne",
    "55441": "Hennepin",
    "55901": "Olmsted",
    "55803": "St. Louis",
}


def run_validation(db_path=None, previous_db_path=None):
    """
    Run all validation checks.
    Returns (passed: bool, results: list of str)
    """
    path = db_path or DB_PATH
    results = []
    failed = []

    if not os.path.exists(path):
        return False, [f"FAIL: DB file not found at {path}"]

    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error as e:
        return False, [f"FAIL: Cannot open DB: {e}"]

    # 1. Table presence
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for table in MIN_COUNTS:
        if table not in tables:
            failed.append(f"FAIL: Table '{table}' missing")
        else:
            results.append(f"  OK: Table '{table}' exists")

    if failed:
        conn.close()
        return False, failed + results

    # 2. Row count checks
    results.append("\nRow counts:")
    for table, minimum in MIN_COUNTS.items():
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count < minimum:
            failed.append(f"FAIL: {table} has {count} rows (minimum {minimum})")
        else:
            results.append(f"  OK: {table}: {count:,} rows")

    # 3. Required plans with formulary_id
    results.append("\nRequired plans:")
    for contract_id, plan_id in REQUIRED_PLANS:
        row = conn.execute(
            "SELECT plan_name, formulary_id FROM plans WHERE contract_id=? AND plan_id=?",
            (contract_id, plan_id.zfill(3))
        ).fetchone()
        if not row:
            failed.append(f"FAIL: Plan {contract_id}/{plan_id} not in plans table")
        elif not row[1]:
            failed.append(f"FAIL: Plan {contract_id}/{plan_id} ({row[0]}) has no formulary_id")
        else:
            results.append(f"  OK: {contract_id}/{plan_id} — {row[0][:35]} (formulary {row[1]})")

    # 4. Required drugs in formulary
    results.append("\nRequired drugs:")
    for drug in REQUIRED_DRUGS:
        row = conn.execute(
            "SELECT COUNT(*) FROM formulary WHERE LOWER(name) LIKE ?",
            (f"%{drug}%",)
        ).fetchone()
        if not row or row[0] == 0:
            failed.append(f"FAIL: Drug '{drug}' not found in formulary")
        else:
            results.append(f"  OK: '{drug}' — {row[0]} formulary entries")

    # 5. Zip → county checks
    results.append("\nZip→County lookups:")
    for zip_code, expected_county in ZIP_COUNTY_CHECKS.items():
        row = conn.execute("SELECT county_name FROM zip_county WHERE zip=?", (zip_code,)).fetchone()
        if not row:
            failed.append(f"FAIL: zip {zip_code} not found in zip_county")
        elif row[0] != expected_county:
            failed.append(f"FAIL: zip {zip_code} → '{row[0]}' (expected '{expected_county}')")
        else:
            results.append(f"  OK: {zip_code} → {row[0]}")

    # 6. Pharmacy coordinate coverage
    results.append("\nPharmacy coordinates:")
    total = conn.execute("SELECT COUNT(*) FROM pharmacy_names").fetchone()[0]
    geocoded = conn.execute(
        "SELECT COUNT(*) FROM pharmacy_names WHERE lat IS NOT NULL AND lat != 0"
    ).fetchone()[0]
    if total > 0:
        pct = geocoded / total * 100
        if pct < 90:
            failed.append(f"FAIL: Only {pct:.1f}% of pharmacies geocoded ({geocoded}/{total})")
        else:
            results.append(f"  OK: {pct:.1f}% geocoded ({geocoded}/{total})")

    # 7. DB file size
    results.append("\nDB size:")
    size_mb = os.path.getsize(path) / 1024 / 1024
    if size_mb > 95:
        failed.append(f"WARN: DB is {size_mb:.1f} MB — approaching GitHub 100MB limit. Consider Git LFS or R2.")
    else:
        results.append(f"  OK: {size_mb:.1f} MB")

    # 8. Compare against previous DB if provided
    if previous_db_path and os.path.exists(previous_db_path):
        results.append("\nComparison with previous DB:")
        try:
            prev_conn = sqlite3.connect(previous_db_path)
            for table in ["formulary", "beneficiary_cost", "pricing"]:
                new_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                old_count = prev_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                change_pct = abs(new_count - old_count) / max(old_count, 1) * 100
                if change_pct > 20:
                    failed.append(f"WARN: {table} changed by {change_pct:.1f}% ({old_count:,} → {new_count:,}). Verify this is expected.")
                else:
                    results.append(f"  OK: {table}: {old_count:,} → {new_count:,} ({change_pct:.1f}% change)")
            prev_conn.close()
        except Exception as e:
            results.append(f"  SKIP: Could not compare with previous DB: {e}")

    conn.close()

    all_results = results + (["", "=== FAILURES ==="] + failed if failed else [])
    passed = len(failed) == 0
    return passed, all_results


if __name__ == "__main__":
    print(f"\n=== Medicare DB Validation: {DB_PATH} ===\n")
    passed, results = run_validation()
    for line in results:
        print(line)
    if passed:
        print("\n✓ All checks passed.")
        sys.exit(0)
    else:
        print("\n✗ Validation failed. Do not deploy until issues are resolved.")
        sys.exit(1)
