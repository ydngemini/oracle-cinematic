"""
Oracle Query — invokes the fine-tuned oracle-underwriter-70b on Bedrock.

Usage:
    python3 Oracle/scripts/oracle_query.py Oracle/data/test_deals/newark_sfr.json
    python3 Oracle/scripts/oracle_query.py '{"property": {...}}'

Accepts either a path to a JSON file or a raw JSON string as the first argument.
Formats the input per bedrock-conversation-2024 schema and prints the model's
structured underwriting response.
"""

import json
import os
import sys

import boto3
from dotenv import load_dotenv

REGION = "us-west-2"
MODEL_ID = "oracle-underwriter-70b"

SYSTEM_PROMPT = (
    "You are Oracle Underwriter, a Delaware wholesale real estate analyst. "
    "For every property, you MUST:\n"
    "1. Estimate the After Repair Value (ARV) using county-level comps\n"
    "2. Calculate rehab costs based on property age and condition\n"
    "3. Compute Maximum Allowable Offer: MAO = (ARV × 0.70) - Rehab Costs\n"
    "4. Compare asking price against MAO\n"
    "5. REJECT any deal where asking price exceeds MAO\n\n"
    "Output your full reasoning in <reasoning> tags, then a <verdict> tag "
    "(PROCEED or REJECT), then <metrics> with JSON numbers."
)

USER_INSTRUCTION = (
    "Analyze this Delaware wholesale real estate assignment contract. "
    "Estimate the ARV, calculate rehab costs, determine the Maximum Allowable "
    "Offer using (ARV × 0.70) - Rehab Costs, compare against the asking price, "
    "and provide a PROCEED/REJECT verdict with full mathematical reasoning."
)


def load_deal(arg: str) -> dict:
    if os.path.isfile(arg):
        with open(arg) as f:
            return json.load(f)
    return json.loads(arg)


def build_payload(deal: dict) -> dict:
    user_content = f"{USER_INSTRUCTION}\n\n{json.dumps(deal, indent=2)}"
    return {
        "schemaVersion": "bedrock-conversation-2024",
        "system": [{"text": SYSTEM_PROMPT}],
        "messages": [
            {"role": "user", "content": [{"text": user_content}]}
        ],
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: oracle_query.py <deal.json | '{...}'>")
        return 1

    if not os.getenv("AWS_PROFILE"):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        load_dotenv(os.path.join(base, ".env"))

    deal = load_deal(sys.argv[1])

    if os.getenv("AWS_PROFILE"):
        session = boto3.Session(region_name=REGION)
    else:
        session = boto3.Session(
            region_name=REGION,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )

    bedrock_runtime = session.client("bedrock-runtime")

    payload = build_payload(deal)

    print(f"Invoking model: {MODEL_ID}")
    print(f"Property: {deal.get('property', {}).get('address', 'unknown')}")
    print(f"{'='*70}\n")

    try:
        response = bedrock_runtime.converse(
            modelId=MODEL_ID,
            messages=payload["messages"],
            system=payload["system"],
        )

        output = response["output"]["message"]["content"][0]["text"]
        print(output)

        stop_reason = response.get("stopReason", "unknown")
        usage = response.get("usage", {})
        print(f"\n{'='*70}")
        print(f"Stop: {stop_reason} | "
              f"In: {usage.get('inputTokens', '?')} | "
              f"Out: {usage.get('outputTokens', '?')}")
        return 0

    except bedrock_runtime.exceptions.ModelNotReadyException:
        print("ERROR: Model not yet ready. Fine-tuning job may still be in progress.")
        print(f"Check: aws bedrock get-model-customization-job --job-identifier "
              f"arn:aws:bedrock:{REGION}:404870839825:model-customization-job/"
              f"meta.llama3-3-70b-instruct-v1:0:128k/wbtis0vmhc54")
        return 1
    except Exception as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
