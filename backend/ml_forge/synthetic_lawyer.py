"""
Synthetic Lawyer — generates Delaware real estate assignment contract records as JSONL.

Uses Bedrock (Llama 70B primary, DeepSeek v3.2 fallback) to produce
legally-formatted wholesale assignment contracts for training data.
"""

import json
import os
import sys
import time
import random
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.ml_forge.bedrock_client import (
    invoke_bedrock_model,
    PRIMARY_MODEL,
    SECONDARY_MODEL,
)

TOTAL_RECORDS = 100
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "output",
    "de_assignment_contracts.jsonl",
)

DE_COUNTIES = ["New Castle", "Kent", "Sussex"]
DE_CITIES = {
    "New Castle": ["Wilmington", "Newark", "Bear", "Hockessin", "Middletown", "New Castle", "Pike Creek"],
    "Kent": ["Dover", "Smyrna", "Milford", "Camden", "Harrington"],
    "Sussex": ["Rehoboth Beach", "Lewes", "Georgetown", "Seaford", "Millsboro"],
}

PROMPT_TEMPLATE = """Generate a realistic Delaware real estate wholesale assignment contract record as valid JSON.

Requirements:
- This is record {record_num} of a training dataset
- County: {county}
- City: {city}
- The contract must include all legally required fields for a DE assignment of contract
- Assignment fee should be between $5,000 and $35,000
- Original purchase price between $80,000 and $450,000
- Property must be residential (SFR, duplex, or townhome)
- Include realistic Delaware-specific legal language in the clauses
- All dates should be in 2025-2026
- Generate unique names, addresses, and parcel IDs

Output ONLY valid JSON with these exact keys:
{{
  "record_id": "DE-ASG-XXXXX",
  "assignor": {{"name": "", "address": "", "entity_type": "individual|llc"}},
  "assignee": {{"name": "", "address": "", "entity_type": "individual|llc"}},
  "seller": {{"name": "", "address": ""}},
  "property": {{
    "address": "",
    "city": "",
    "county": "",
    "state": "DE",
    "zip": "",
    "parcel_id": "",
    "property_type": "SFR|duplex|townhome",
    "bedrooms": 0,
    "bathrooms": 0,
    "sqft": 0,
    "year_built": 0
  }},
  "original_contract": {{
    "purchase_price": 0,
    "earnest_money": 0,
    "execution_date": "",
    "closing_deadline": "",
    "contingencies": []
  }},
  "assignment": {{
    "assignment_fee": 0,
    "total_price_to_assignee": 0,
    "assignment_date": "",
    "consideration_paid": 0,
    "deposit_held_by": ""
  }},
  "legal_clauses": {{
    "assignability_clause": "",
    "indemnification": "",
    "governing_law": "",
    "dispute_resolution": "",
    "closing_obligations": ""
  }},
  "notarization": {{
    "notary_name": "",
    "commission_expiry": "",
    "county_recorded": "",
    "recording_ref": ""
  }}
}}

Output ONLY the JSON object, no markdown, no explanation."""


def generate_record(record_num: int) -> dict | None:
    county = random.choice(DE_COUNTIES)
    city = random.choice(DE_CITIES[county])

    prompt = PROMPT_TEMPLATE.format(
        record_num=record_num,
        county=county,
        city=city,
    )

    result = invoke_bedrock_model(PRIMARY_MODEL, prompt, max_tokens=2048)

    if not result:
        result = invoke_bedrock_model(SECONDARY_MODEL, prompt, max_tokens=2048)

    if not result:
        return None

    try:
        json_start = result.find("{")
        json_end = result.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            record = json.loads(result[json_start:json_end])
            record["_meta"] = {
                "generated_at": datetime.utcnow().isoformat(),
                "model": PRIMARY_MODEL,
                "record_num": record_num,
            }
            return record
    except json.JSONDecodeError:
        pass

    return None


def main():
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    print(f"{'='*70}")
    print(f"  SYNTHETIC LAWYER — Delaware Assignment Contract Generator")
    print(f"  Models: {PRIMARY_MODEL} (primary) | {SECONDARY_MODEL} (fallback)")
    print(f"  Target: {TOTAL_RECORDS} JSONL records")
    print(f"  Output: {OUTPUT_PATH}")
    print(f"{'='*70}\n")

    success = 0
    failed = 0
    t_start = time.time()

    with open(OUTPUT_PATH, "w") as f:
        for i in range(1, TOTAL_RECORDS + 1):
            record = generate_record(i)

            if record:
                line = json.dumps(record)
                f.write(line + "\n")
                f.flush()
                success += 1

                prop = record.get("property", {})
                asg = record.get("assignment", {})
                print(
                    f"[{i:03d}/{TOTAL_RECORDS}] "
                    f"{record.get('record_id', 'N/A'):<16} "
                    f"{prop.get('address', ''):<35} "
                    f"{prop.get('city', ''):<15} "
                    f"${asg.get('assignment_fee', 0):>8,} fee | "
                    f"${asg.get('total_price_to_assignee', 0):>10,} total"
                )
            else:
                failed += 1
                print(f"[{i:03d}/{TOTAL_RECORDS}] FAILED — retrying next record")

            time.sleep(0.2)

    elapsed = time.time() - t_start
    print(f"\n{'='*70}")
    print(f"  COMPLETE: {success} records generated, {failed} failed")
    print(f"  Time: {elapsed:.1f}s ({elapsed/TOTAL_RECORDS:.2f}s/record)")
    print(f"  Output: {OUTPUT_PATH}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
