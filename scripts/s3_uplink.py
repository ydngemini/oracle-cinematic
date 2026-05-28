"""
S3 Uplink — uploads swarm_textbook.jsonl to the nexum-swarm-training-data bucket.

Connects to AWS us-east-2, creates the bucket if it doesn't exist,
uploads the training data, and prints the final s3:// URI.
"""

import os
import sys

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

REGION = "us-east-2"
BUCKET = "nexum-swarm-training-data"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_FILE = os.path.join(BASE_DIR, "training_data", "swarm_textbook.jsonl")
S3_KEY = "training_data/swarm_textbook.jsonl"


def main() -> int:
    if not os.getenv("AWS_PROFILE"):
        load_dotenv(os.path.join(BASE_DIR, ".env"))

    if not os.path.isfile(LOCAL_FILE):
        print(f"ERROR: Training data not found: {LOCAL_FILE}")
        return 1

    file_size = os.path.getsize(LOCAL_FILE)
    print(f"File: {LOCAL_FILE} ({file_size:,} bytes)")
    print(f"Target: s3://{BUCKET}/{S3_KEY}")
    print(f"Region: {REGION}")

    # Use AWS_PROFILE if set, otherwise fall back to explicit .env creds
    if os.getenv("AWS_PROFILE"):
        print(f"Auth: AWS_PROFILE={os.getenv('AWS_PROFILE')}\n")
        session = boto3.Session(region_name=REGION)
    else:
        aws_key = os.getenv("AWS_ACCESS_KEY_ID")
        aws_secret = os.getenv("AWS_SECRET_ACCESS_KEY")
        if not aws_key or not aws_secret:
            print("ERROR: Set AWS_PROFILE or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in .env")
            return 1
        print(f"Auth: explicit credentials\n")
        session = boto3.Session(
            region_name=REGION,
            aws_access_key_id=aws_key,
            aws_secret_access_key=aws_secret,
        )

    s3 = session.client("s3")

    # Check if bucket exists, create if not
    try:
        s3.head_bucket(Bucket=BUCKET)
        print(f"Bucket '{BUCKET}' exists.")
    except ClientError as e:
        error_code = int(e.response["Error"]["Code"])
        if error_code == 404:
            print(f"Bucket '{BUCKET}' not found. Creating...")
            s3.create_bucket(
                Bucket=BUCKET,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
            print(f"Bucket '{BUCKET}' created in {REGION}.")
        else:
            print(f"ERROR: Cannot access bucket: {e}")
            return 1

    # Upload
    print(f"Uploading {file_size:,} bytes...")
    s3.upload_file(LOCAL_FILE, BUCKET, S3_KEY)

    uri = f"s3://{BUCKET}/{S3_KEY}"
    print(f"\nUpload complete.")
    print(uri)
    return 0


if __name__ == "__main__":
    sys.exit(main())
