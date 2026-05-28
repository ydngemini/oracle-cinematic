"""
Forge Model — submits a Bedrock fine-tuning job for the Oracle underwriter.

Uses the swarm_textbook.jsonl in S3 to fine-tune Meta Llama 3.3 70B Instruct
into a Delaware wholesale real estate analyst that enforces the 70% MAO rule.
"""

import json
import os
import sys
from datetime import datetime

import boto3
from dotenv import load_dotenv

REGION = "us-west-2"
BASE_MODEL = "meta.llama3-3-70b-instruct-v1:0:128k"
S3_BUCKET = "nexum-oracle-forge-404870839825"
S3_KEY = "training_data/swarm_textbook_bedrock.jsonl"
S3_URI = f"s3://{S3_BUCKET}/{S3_KEY}"
OUTPUT_S3_URI = f"s3://{S3_BUCKET}/output/"

JOB_NAME = f"oracle-underwriter-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
CUSTOM_MODEL_NAME = "oracle-underwriter-70b"

ROLE_ARN = os.getenv("BEDROCK_ROLE_ARN", "")


def main() -> int:
    if not os.getenv("AWS_PROFILE"):
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

    print(f"{'='*70}")
    print(f"  FORGE MODEL — Bedrock Fine-Tuning Job Submission")
    print(f"  Base model: {BASE_MODEL}")
    print(f"  Training data: {S3_URI}")
    print(f"  Output: {OUTPUT_S3_URI}")
    print(f"  Job name: {JOB_NAME}")
    print(f"  Custom model: {CUSTOM_MODEL_NAME}")
    print(f"{'='*70}\n")

    if os.getenv("AWS_PROFILE"):
        session = boto3.Session(region_name=REGION)
        print(f"Auth: AWS_PROFILE={os.getenv('AWS_PROFILE')}")
    else:
        aws_key = os.getenv("AWS_ACCESS_KEY_ID")
        aws_secret = os.getenv("AWS_SECRET_ACCESS_KEY")
        if not aws_key or not aws_secret:
            print("ERROR: Set AWS_PROFILE or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY")
            return 1
        session = boto3.Session(
            region_name=REGION,
            aws_access_key_id=aws_key,
            aws_secret_access_key=aws_secret,
        )
        print("Auth: explicit credentials")

    bedrock = session.client("bedrock")

    # Check if training data exists in S3
    s3 = session.client("s3")
    try:
        resp = s3.head_object(Bucket=S3_BUCKET, Key=S3_KEY)
        size = resp["ContentLength"]
        print(f"Training data confirmed: {size:,} bytes\n")
    except Exception as e:
        print(f"ERROR: Training data not found at {S3_URI}: {e}")
        return 1

    # Resolve or create the Bedrock service role
    role_arn = ROLE_ARN
    if not role_arn:
        iam = session.client("iam")
        role_name = "BedrockFineTuningRole"
        try:
            role_resp = iam.get_role(RoleName=role_name)
            role_arn = role_resp["Role"]["Arn"]
            print(f"Using existing role: {role_arn}")
        except iam.exceptions.NoSuchEntityException:
            print(f"Creating IAM role: {role_name}...")
            trust_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "bedrock.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
            role_resp = iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description="Allows Bedrock to access S3 for fine-tuning jobs",
            )
            role_arn = role_resp["Role"]["Arn"]

            # Attach S3 read policy
            s3_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:ListBucket", "s3:PutObject"],
                        "Resource": [
                            f"arn:aws:s3:::{S3_BUCKET}",
                            f"arn:aws:s3:::{S3_BUCKET}/*",
                        ],
                    }
                ],
            }
            iam.put_role_policy(
                RoleName=role_name,
                PolicyName="BedrockS3Access",
                PolicyDocument=json.dumps(s3_policy),
            )
            print(f"Created role: {role_arn}")
            print("Waiting 10s for IAM propagation...")
            import time
            time.sleep(10)

    print(f"\nSubmitting fine-tuning job...")
    print(f"  Role: {role_arn}")

    try:
        response = bedrock.create_model_customization_job(
            jobName=JOB_NAME,
            customModelName=CUSTOM_MODEL_NAME,
            roleArn=role_arn,
            baseModelIdentifier=BASE_MODEL,
            customizationType="FINE_TUNING",
            trainingDataConfig={"s3Uri": S3_URI},
            outputDataConfig={"s3Uri": OUTPUT_S3_URI},
            hyperParameters={
                "epochCount": "3",
                "batchSize": "1",
                "learningRate": "0.00001",
            },
        )

        job_arn = response["jobArn"]
        print(f"\nJob submitted successfully!")
        print(f"\njobArn: {job_arn}")
        print(f"\nMonitor with:")
        print(f"  aws bedrock get-model-customization-job --job-identifier {job_arn} --region {REGION}")
        return 0

    except Exception as e:
        print(f"\nERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
