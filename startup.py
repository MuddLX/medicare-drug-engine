"""
startup.py — Railway startup script
Downloads the latest medicare_mn.db from Cloudflare R2 before the app starts.
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

DB_PATH = "medicare_mn.db"
REQUIRED_ENV_VARS = ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME"]


def check_env():
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
        print("Set these in Railway → Service → Variables before deploying.")
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


def download_db():
    bucket = os.environ["R2_BUCKET_NAME"]
    client = get_r2_client()

    print(f"Checking R2 for latest medicare_mn.db...")

    try:
        # Get remote file metadata
        head = client.head_object(Bucket=bucket, Key="medicare_mn.db")
        remote_size = head["ContentLength"]
        remote_modified = head["LastModified"]
        print(f"  Remote DB: {remote_size / 1024 / 1024:.1f} MB, modified {remote_modified}")

        # Check if local DB exists and is current
        if os.path.exists(DB_PATH):
            local_size = os.path.getsize(DB_PATH)
            if local_size == remote_size:
                print(f"  Local DB already up to date ({local_size / 1024 / 1024:.1f} MB). Skipping download.")
                return
            else:
                print(f"  Local DB differs ({local_size / 1024 / 1024:.1f} MB local vs {remote_size / 1024 / 1024:.1f} MB remote). Downloading...")
        else:
            print(f"  No local DB found. Downloading {remote_size / 1024 / 1024:.1f} MB...")

        # Download
        start = time.time()
        client.download_file(bucket, "medicare_mn.db", DB_PATH)
        elapsed = time.time() - start

        # Verify download
        downloaded_size = os.path.getsize(DB_PATH)
        if downloaded_size != remote_size:
            print(f"ERROR: Download size mismatch. Expected {remote_size}, got {downloaded_size}.")
            sys.exit(1)

        print(f"  Downloaded successfully in {elapsed:.1f}s ({downloaded_size / 1024 / 1024:.1f} MB)")

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "404" or error_code == "NoSuchKey":
            print("ERROR: medicare_mn.db not found in R2 bucket.")
            print("Run quarterly_refresh.py locally first to build and upload the DB.")
            sys.exit(1)
        else:
            print(f"ERROR: R2 error: {e}")
            sys.exit(1)
    except NoCredentialsError:
        print("ERROR: Invalid R2 credentials. Check Railway environment variables.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Unexpected error downloading DB: {e}")
        sys.exit(1)


def validate_db():
    """Quick sanity check that the DB is usable before starting the app."""
    import sqlite3
    try:
        conn = sqlite3.connect(DB_PATH)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        required_tables = ["plans", "formulary", "beneficiary_cost", "pricing", "pharmacy_names",
                           "pharmacy_network", "zip_coords", "service_area", "zip_county"]
        missing = [t for t in required_tables if t not in tables]
        if missing:
            print(f"ERROR: DB is missing tables: {', '.join(missing)}")
            conn.close()
            sys.exit(1)

        # Quick row count checks
        plan_count = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        zip_county = conn.execute("SELECT county_name FROM zip_county WHERE zip='55309'").fetchone()
        conn.close()

        if plan_count < 50:
            print(f"ERROR: DB has only {plan_count} plans. Expected 65. DB may be corrupt.")
            sys.exit(1)

        if not zip_county:
            print("WARNING: zip 55309 not found in zip_county. County lookups may fail.")

        print(f"  DB validation passed: {plan_count} plans, all tables present.")

    except sqlite3.Error as e:
        print(f"ERROR: DB validation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    print("=== Medicare DB Startup ===")
    check_env()
    download_db()
    validate_db()
    print("=== Startup complete. Starting app... ===\n")
