# upload_pbp_db.py
# Uploads pbp_benefits.db to Cloudflare R2 (bucket 'medicare-db').
# Credentials are read from .env (never hardcoded — .env is gitignored).
# Delete-then-upload avoids R2 silently serving a stale cached copy on re-runs,
# so this is also the script to re-run after each PBP rebuild (e.g. the 2027 data).
# Usage: python upload_pbp_db.py

import boto3
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "pbp_benefits.db")
R2_KEY = "pbp_benefits.db"

# --- load R2 credentials from .env ---
env = {}
with open(os.path.join(HERE, ".env")) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()


def main():
    if not os.path.exists(DB_PATH):
        print("ERROR: pbp_benefits.db not found. Run build_pbp_db.py first.")
        sys.exit(1)

    size_kb = os.path.getsize(DB_PATH) / 1024
    bucket = env["R2_BUCKET_NAME"]
    print(f"Uploading pbp_benefits.db ({size_kb:.1f} KB) to r2://{bucket}/{R2_KEY} ...")

    s3 = boto3.client(
        "s3",
        endpoint_url=env["R2_ENDPOINT_URL"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    # delete-then-upload (avoids silent cache failures on re-runs)
    try:
        s3.delete_object(Bucket=bucket, Key=R2_KEY)
        print("  removed existing copy (if any)")
    except Exception as e:
        print(f"  no existing copy to remove ({e.__class__.__name__})")

    s3.upload_file(DB_PATH, bucket, R2_KEY)

    # verify it landed
    obj = s3.head_object(Bucket=bucket, Key=R2_KEY)
    print(f"Upload complete. R2 now reports {obj['ContentLength'] / 1024:.1f} KB, "
          f"last modified {obj['LastModified']}.")
    print("\nNext step: add pbp_benefits.db to startup.py so Railway downloads it on deploy.")


if __name__ == "__main__":
    main()
