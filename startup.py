"""\nstartup.py — Railway startup script
Downloads the latest medicare_mn.db, medica_providers.db, bcbs_providers.db,
hp_providers.db, humana_providers.db, and uhc_providers.db from Cloudflare R2
before the app starts.
Runs automatically via the Railway start command:
    python startup.py && gunicorn app.main:app

If R2 credentials are missing or download fails, the app will not start.
This is intentional — a missing DB means the app would return wrong data.
"""

import os
import sys
import time
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

DB_PATH           = "medicare_mn.db"
PROVIDERS_DB_PATH = "medica_providers.db"
BCBS_DB_PATH      = "bcbs_providers.db"
HP_DB_PATH        = "hp_providers.db"
HUMANA_DB_PATH    = "humana_providers.db"
UHC_DB_PATH       = "uhc_providers.db"
REQUIRED_ENV_VARS = ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME"]


def check_env():
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
        print("Set these in Railway -> Service -> Variables before deploying.")
        sys.exit(1)


def get_r2_client():
    endpoint = os.environ["R2_ENDPOINT_URL"]
    key_id = os.environ["R2_ACCESS_KEY_ID"]
    print(f"  R2 endpoint: {endpoint}")
    print(f"  R2 key ID: {key_id[:8]}...{key_id[-4:]} (length: {len(key_id)})")
    print(f"  R2 bucket: {os.environ['R2_BUCKET_NAME']}")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def download_file_from_r2(client, bucket, r2_key, local_path, label):
    print(f"Checking R2 for latest {label}...")
    try:
        head = client.head_object(Bucket=bucket, Key=r2_key)
        remote_size     = head["ContentLength"]
        remote_modified = head["LastModified"]
        print(f"  Remote {label}: {remote_size / 1024 / 1024:.1f} MB, modified {remote_modified}")

        if os.path.exists(local_path):
            local_size = os.path.getsize(local_path)
            local_mtime = os.path.getmtime(local_path)
            import datetime
            remote_dt = remote_modified.timestamp() if hasattr(remote_modified, 'timestamp') else 0
            if local_size == remote_size and local_mtime >= remote_dt:
                print(f"  Local {label} already up to date ({local_size / 1024 / 1024:.1f} MB). Skipping download.")
                return
            else:
                print(f"  Local {label} differs or outdated. Downloading {remote_size / 1024 / 1024:.1f} MB...")
        else:
            print(f"  No local {label} found. Downloading {remote_size / 1024 / 1024:.1f} MB...")

        start = time.time()
        client.download_file(bucket, r2_key, local_path)
        elapsed = time.time() - start

        downloaded_size = os.path.getsize(local_path)
        if downloaded_size != remote_size:
            print(f"ERROR: {label} download size mismatch. Expected {remote_size}, got {downloaded_size}.")
            sys.exit(1)

        print(f"  {label} downloaded successfully in {elapsed:.1f}s ({downloaded_size / 1024 / 1024:.1f} MB)")

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("404", "NoSuchKey"):
            print(f"ERROR: {r2_key} not found in R2 bucket.")
            if r2_key == "medicare_mn.db":
                print("Run quarterly_refresh.py locally first to build and upload the DB.")
            else:
                print(f"Upload {r2_key} to R2 bucket before deploying.")
            sys.exit(1)
        else:
            print(f"ERROR: R2 error downloading {label}: {e}")
            sys.exit(1)
    except NoCredentialsError:
        print("ERROR: Invalid R2 credentials. Check Railway environment variables.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Unexpected error downloading {label}: {e}")
        sys.exit(1)


def download_db():
    bucket = os.environ["R2_BUCKET_NAME"]
    client = get_r2_client()
    download_file_from_r2(client, bucket, "medicare_mn.db",       DB_PATH,           "medicare_mn.db")
    download_file_from_r2(client, bucket, "medica_providers.db",  PROVIDERS_DB_PATH, "medica_providers.db")
    download_file_from_r2(client, bucket, "bcbs_providers.db",    BCBS_DB_PATH,      "bcbs_providers.db")
    download_file_from_r2(client, bucket, "hp_providers.db",      HP_DB_PATH,        "hp_providers.db")
    download_file_from_r2(client, bucket, "humana_providers.db",  HUMANA_DB_PATH,    "humana_providers.db")
    download_file_from_r2(client, bucket, "uhc_providers.db",     UHC_DB_PATH,       "uhc_providers.db")


def validate_db():
    import sqlite3

    # -- Validate medicare_mn.db -----------------------------------------------
    try:
        conn = sqlite3.connect(DB_PATH)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        required_tables = ["plans", "formulary", "beneficiary_cost", "pricing",
                           "pharmacy_names", "pharmacy_network", "zip_coords",
                           "service_area", "zip_county"]
        missing = [t for t in required_tables if t not in tables]
        if missing:
            print(f"ERROR: medicare_mn.db is missing tables: {', '.join(missing)}")
            conn.close()
            sys.exit(1)
        plan_count = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        zip_county = conn.execute("SELECT county_name FROM zip_county WHERE zip='55309'").fetchone()
        conn.close()
        if plan_count < 50:
            print(f"ERROR: medicare_mn.db has only {plan_count} plans. Expected 65. DB may be corrupt.")
            sys.exit(1)
        if not zip_county:
            print("WARNING: zip 55309 not found in zip_county. County lookups may fail.")
        print(f"  medicare_mn.db validation passed: {plan_count} plans, all tables present.")
    except sqlite3.Error as e:
        print(f"ERROR: medicare_mn.db validation failed: {e}")
        sys.exit(1)

    # -- Validate medica_providers.db ------------------------------------------
    try:
        conn = sqlite3.connect(PROVIDERS_DB_PATH)
        if "providers" not in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
            print("ERROR: medica_providers.db is missing the providers table.")
            conn.close(); sys.exit(1)
        provider_count = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
        conn.close()
        if provider_count < 20000:
            print(f"ERROR: medica_providers.db has only {provider_count} providers. Expected ~25,000.")
            sys.exit(1)
        print(f"  medica_providers.db validation passed: {provider_count:,} providers.")
    except sqlite3.Error as e:
        print(f"ERROR: medica_providers.db validation failed: {e}"); sys.exit(1)

    # -- Validate bcbs_providers.db --------------------------------------------
    try:
        conn = sqlite3.connect(BCBS_DB_PATH)
        if "providers" not in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
            print("ERROR: bcbs_providers.db is missing the providers table.")
            conn.close(); sys.exit(1)
        provider_count = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
        dental_count   = conn.execute("SELECT COUNT(*) FROM providers WHERE source='dental'").fetchone()[0]
        conn.close()
        if provider_count < 20000:
            print(f"ERROR: bcbs_providers.db has only {provider_count} providers. Expected ~24,000.")
            sys.exit(1)
        print(f"  bcbs_providers.db validation passed: {provider_count:,} providers ({dental_count:,} dental).")
    except sqlite3.Error as e:
        print(f"ERROR: bcbs_providers.db validation failed: {e}"); sys.exit(1)

    # -- Validate hp_providers.db ----------------------------------------------
    try:
        conn = sqlite3.connect(HP_DB_PATH)
        if "providers" not in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
            print("ERROR: hp_providers.db is missing the providers table.")
            conn.close(); sys.exit(1)
        provider_count = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
        dental_count   = conn.execute("SELECT COUNT(*) FROM providers WHERE source='dental'").fetchone()[0]
        conn.close()
        if provider_count < 30000:
            print(f"ERROR: hp_providers.db has only {provider_count} providers. Expected ~43,000.")
            sys.exit(1)
        print(f"  hp_providers.db validation passed: {provider_count:,} providers ({dental_count:,} dental).")
    except sqlite3.Error as e:
        print(f"ERROR: hp_providers.db validation failed: {e}"); sys.exit(1)

    # -- Validate humana_providers.db ------------------------------------------
    try:
        conn = sqlite3.connect(HUMANA_DB_PATH)
        if "providers" not in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
            print("ERROR: humana_providers.db is missing the providers table.")
            conn.close(); sys.exit(1)
        provider_count = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
        dental_count   = conn.execute("SELECT COUNT(*) FROM providers WHERE source='dental'").fetchone()[0]
        conn.close()
        if provider_count < 15000:
            print(f"ERROR: humana_providers.db has only {provider_count} providers. Expected ~23,000.")
            sys.exit(1)
        print(f"  humana_providers.db validation passed: {provider_count:,} providers ({dental_count:,} dental).")
    except sqlite3.Error as e:
        print(f"ERROR: humana_providers.db validation failed: {e}"); sys.exit(1)

    # -- Validate uhc_providers.db ---------------------------------------------
    try:
        conn = sqlite3.connect(UHC_DB_PATH)
        if "providers" not in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
            print("ERROR: uhc_providers.db is missing the providers table.")
            conn.close(); sys.exit(1)
        provider_count = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
        conn.close()
        if provider_count < 400:
            print(f"ERROR: uhc_providers.db has only {provider_count} providers. Expected ~600+.")
            sys.exit(1)
        print(f"  uhc_providers.db validation passed: {provider_count:,} providers.")
    except sqlite3.Error as e:
        print(f"ERROR: uhc_providers.db validation failed: {e}"); sys.exit(1)


if __name__ == "__main__":
    print("=== Medicare DB Startup ===")
    check_env()
    download_db()
    validate_db()
    print("=== Startup complete. Starting app... ===\n")
