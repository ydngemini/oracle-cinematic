import json
import os
import sys

import boto3
from dotenv import load_dotenv


MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "meta.llama3-3-70b-instruct-v1:0")
PROMPT = "Respond with exactly three words: Swarm uplink established."


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    load_dotenv()

    region = os.getenv("AWS_REGION") or os.getenv("BEDROCK_REGION") or "us-east-2"

    print("Initiating AWS Bedrock uplink...")
    print(f"Region: {region}")
    print(f"Model: {MODEL_ID}")

    try:
        client = boto3.client(
            service_name="bedrock-runtime",
            region_name=region,
            aws_access_key_id=required_env("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=required_env("AWS_SECRET_ACCESS_KEY"),
        )

        if "anthropic" in MODEL_ID:
            payload = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 50,
                "messages": [{"role": "user", "content": PROMPT}],
            }
        else:
            payload = {
                "prompt": f"<|begin_of_text|><|start_header_id|>user<|end_header_id|>\n\n{PROMPT}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n",
                "max_gen_len": 50,
                "temperature": 0.6,
                "top_p": 0.9,
            }

        response = client.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps(payload),
            contentType="application/json",
            accept="application/json",
        )

        response_body = json.loads(response["body"].read())
        if "anthropic" in MODEL_ID:
            text = response_body["content"][0]["text"].strip()
        else:
            text = response_body.get("generation", "").strip()
        print(f"\n[AWS RESPONSE]: {text}")
        print("\nStatus: Bedrock call completed.")
        return 0

    except Exception as exc:
        print(f"\n[UPLINK FAILED]: {exc}")
        print("Check .env formatting and confirm the key, region, and model access are valid.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
