"""Manage the files Railway downloads from Cloudflare R2 at startup (2026-10-06).

    python r2_files.py list                          what's in the bucket (name, size, date)
    python r2_files.py upload providers_2027.db      upload a local file under the same name
    python r2_files.py upload medicare_mn_2027.db --as medicare_mn.db
    python r2_files.py delete medica_providers.db    delete a file (asks you to type DELETE first)

Reads the R2 credentials from .env in this folder (same as the other upload scripts).
"""
import os
import sys

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))


def env():
    with open(os.path.join(HERE, ".env")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    return boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
                        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto"), \
        os.environ["R2_BUCKET_NAME"]


def main(argv):
    if not argv or argv[0] not in ("list", "upload", "delete"):
        sys.exit(__doc__)
    client, bucket = env()
    if argv[0] == "list":
        for o in client.list_objects_v2(Bucket=bucket).get("Contents", []):
            print(f"{o['Key']:45} {o['Size'] / 1024 / 1024:8.1f} MB   {o['LastModified']:%Y-%m-%d %H:%M}")
    elif argv[0] == "upload":
        local = os.path.join(HERE, argv[1])
        key = argv[argv.index("--as") + 1] if "--as" in argv else os.path.basename(argv[1])
        if not os.path.exists(local):
            sys.exit(f"Not found: {local}")
        print(f"Uploading {argv[1]} ({os.path.getsize(local) / 1024 / 1024:.1f} MB) as {key} ...")
        client.upload_file(local, bucket, key)
        head = client.head_object(Bucket=bucket, Key=key)
        if head["ContentLength"] != os.path.getsize(local):
            sys.exit("ERROR: size in R2 doesn't match the local file - try again.")
        print(f"Done. Verified in R2: {key} {head['ContentLength'] / 1024 / 1024:.1f} MB")
    else:
        key = argv[1]
        if input(f"Permanently delete {key} from R2? Type DELETE to confirm: ").strip() != "DELETE":
            sys.exit("Nothing deleted.")
        client.delete_object(Bucket=bucket, Key=key)
        print(f"Deleted {key}.")


if __name__ == "__main__":
    main(sys.argv[1:])
