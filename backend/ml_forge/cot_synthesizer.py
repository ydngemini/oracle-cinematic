"""
CoT Synthesizer — transforms raw property scrapes into Chain-of-Thought training samples.

Reads all JSON files from Oracle/data/raw_scrapes/, generates multi-step reasoning
blocks for each property record with ARV estimation, rehab cost analysis, and MAO
calculation. Outputs to Oracle/training_data/swarm_textbook.jsonl.

Verdict logic: if asking price > MAO → REJECT. Period.
"""

import json
import os
import sys
import traceback
from datetime import datetime
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW_SCRAPES_DIR = os.path.join(BASE_DIR, "data", "raw_scrapes")
OUTPUT_PATH = os.path.join(BASE_DIR, "training_data", "swarm_textbook.jsonl")
ERROR_LOG_PATH = os.path.join(BASE_DIR, "training_data", "error_log.txt")

# Delaware county-level ARV multipliers ($/sqft for retail-ready SFR)
# Based on median sale prices for renovated properties in each county.
COUNTY_ARV_PSF = {
    "New Castle": 195.0,
    "Kent": 155.0,
    "Sussex": 210.0,
}

# Year-built brackets for rehab cost estimation ($/sqft)
REHAB_COST_BRACKETS = [
    (1900, 65.0),   # Pre-1920: gut rehab territory
    (1920, 55.0),   # 1920-1949: heavy rehab
    (1950, 45.0),   # 1950-1969: moderate-heavy
    (1970, 35.0),   # 1970-1989: moderate
    (1990, 22.0),   # 1990-2004: light-moderate
    (2005, 12.0),   # 2005+: cosmetic only
]

# Condition modifiers based on property age relative to last likely reno
CONDITION_LABELS = {
    "gut_rehab": {"label": "Gut Rehab", "multiplier": 1.3},
    "heavy": {"label": "Heavy Rehab", "multiplier": 1.1},
    "moderate": {"label": "Moderate Rehab", "multiplier": 1.0},
    "light": {"label": "Light Cosmetic", "multiplier": 0.75},
    "turnkey": {"label": "Near-Turnkey", "multiplier": 0.5},
}


def estimate_arv(prop: dict) -> tuple[float, str]:
    county = prop.get("county", "Kent")
    sqft = prop.get("sqft", 1200)
    bedrooms = prop.get("bedrooms", 3)
    bathrooms = prop.get("bathrooms", 1)

    base_psf = COUNTY_ARV_PSF.get(county, 155.0)

    # Adjust for bed/bath count (market premium for 4+ BR or 2.5+ BA)
    if bedrooms >= 4:
        base_psf *= 1.08
    if bathrooms >= 2.5:
        base_psf *= 1.05
    if bedrooms <= 2:
        base_psf *= 0.90

    arv = round(base_psf * sqft, -3)  # Round to nearest $1000

    rationale = (
        f"County median ARV/sqft for renovated SFR in {county} County: "
        f"${base_psf:.0f}/sqft. "
        f"{sqft} sqft × ${base_psf:.0f} = ${arv:,.0f} estimated ARV."
    )
    return arv, rationale


def estimate_rehab_cost(prop: dict) -> tuple[float, str, str]:
    year_built = prop.get("year_built", 1990)
    sqft = prop.get("sqft", 1200)

    # Find rehab bracket
    rehab_psf = 22.0
    for cutoff, cost in REHAB_COST_BRACKETS:
        if year_built < cutoff:
            rehab_psf = cost
            break
    else:
        # Year >= 2005
        rehab_psf = 12.0

    # Determine condition category
    age = 2026 - year_built
    if age > 80:
        cond_key = "gut_rehab"
    elif age > 50:
        cond_key = "heavy"
    elif age > 30:
        cond_key = "moderate"
    elif age > 15:
        cond_key = "light"
    else:
        cond_key = "turnkey"

    cond = CONDITION_LABELS[cond_key]
    adjusted_psf = rehab_psf * cond["multiplier"]
    rehab_total = round(adjusted_psf * sqft, -2)  # Round to nearest $100

    rationale = (
        f"Built {year_built} ({age} years old) → condition: {cond['label']}. "
        f"Base rehab: ${rehab_psf:.0f}/sqft × {cond['multiplier']:.2f} condition modifier = "
        f"${adjusted_psf:.0f}/sqft. "
        f"{sqft} sqft × ${adjusted_psf:.0f} = ${rehab_total:,.0f} estimated rehab."
    )
    return rehab_total, rationale, cond["label"]


def calculate_mao(arv: float, rehab_cost: float) -> tuple[float, str]:
    mao = round((arv * 0.70) - rehab_cost, -2)
    rationale = (
        f"MAO = (ARV × 0.70) - Rehab Costs\n"
        f"MAO = (${arv:,.0f} × 0.70) - ${rehab_cost:,.0f}\n"
        f"MAO = ${arv * 0.70:,.0f} - ${rehab_cost:,.0f}\n"
        f"MAO = ${mao:,.0f}"
    )
    return mao, rationale


def compute_deal_metrics(record: dict) -> dict:
    prop = record.get("property", {})
    orig = record.get("original_contract", {})
    asg = record.get("assignment", {})

    purchase_price = orig.get("purchase_price", 0)
    assignment_fee = asg.get("assignment_fee", 0)
    total_to_assignee = asg.get("total_price_to_assignee", 0)
    earnest_money = orig.get("earnest_money", 0)
    sqft = prop.get("sqft", 0)

    roi_on_earnest = (assignment_fee / earnest_money * 100) if earnest_money > 0 else 0
    price_per_sqft = (purchase_price / sqft) if sqft > 0 else 0
    spread_pct = (assignment_fee / purchase_price * 100) if purchase_price > 0 else 0
    markup_to_assignee = total_to_assignee - purchase_price

    arv, arv_rationale = estimate_arv(prop)
    rehab_cost, rehab_rationale, condition = estimate_rehab_cost(prop)
    mao, mao_rationale = calculate_mao(arv, rehab_cost)

    asking_price = total_to_assignee  # What the assignee actually pays
    mao_delta = asking_price - mao
    mao_delta_pct = (mao_delta / mao * 100) if mao > 0 else 999

    return {
        "purchase_price": purchase_price,
        "assignment_fee": assignment_fee,
        "total_to_assignee": total_to_assignee,
        "earnest_money": earnest_money,
        "roi_on_earnest": round(roi_on_earnest, 1),
        "price_per_sqft": round(price_per_sqft, 2),
        "spread_pct": round(spread_pct, 1),
        "markup_to_assignee": markup_to_assignee,
        "sqft": sqft,
        "bedrooms": prop.get("bedrooms", 0),
        "bathrooms": prop.get("bathrooms", 0),
        "year_built": prop.get("year_built", 0),
        "property_type": prop.get("property_type", "unknown"),
        "county": prop.get("county", "unknown"),
        "city": prop.get("city", "unknown"),
        "arv": arv,
        "arv_rationale": arv_rationale,
        "rehab_cost": rehab_cost,
        "rehab_rationale": rehab_rationale,
        "condition": condition,
        "mao": mao,
        "mao_rationale": mao_rationale,
        "mao_delta": mao_delta,
        "mao_delta_pct": round(mao_delta_pct, 1),
        "asking_price": asking_price,
    }


def assess_risk_factors(record: dict, metrics: dict) -> list[str]:
    risks = []
    orig = record.get("original_contract", {})
    contingencies = orig.get("contingencies", [])

    if metrics["asking_price"] > metrics["mao"]:
        risks.append(
            f"CRITICAL: Asking price (${metrics['asking_price']:,}) exceeds MAO "
            f"(${metrics['mao']:,}) by ${metrics['mao_delta']:,} "
            f"({metrics['mao_delta_pct']:.1f}% over limit) — NO MARGIN FOR PROFIT"
        )
    if metrics["spread_pct"] > 15:
        risks.append("High assignment spread (>15%) — assignor extracting excessive fee")
    if metrics["spread_pct"] < 5:
        risks.append("Thin margin (<5%) leaves no room for closing cost overruns")
    if "financing" in contingencies:
        risks.append("Financing contingency — deal depends on assignee loan approval")
    if "inspection" in contingencies:
        risks.append("Inspection contingency — potential renegotiation risk post-inspection")
    if metrics["year_built"] and metrics["year_built"] < 1970:
        risks.append(
            f"Property built {metrics['year_built']} — likely deferred maintenance, "
            f"lead paint, asbestos risk. Rehab costs may exceed estimate."
        )
    if metrics["rehab_cost"] > 50000:
        risks.append(f"Heavy rehab budget (${metrics['rehab_cost']:,}) — execution risk is high")
    if metrics["roi_on_earnest"] > 500:
        risks.append("Extremely high ROI on earnest may indicate inflated assignment fee")

    return risks if risks else ["Standard risk profile — no red flags identified"]


def generate_cot_reasoning(record: dict) -> dict:
    metrics = compute_deal_metrics(record)
    risks = assess_risk_factors(record, metrics)
    prop = record.get("property", {})
    orig = record.get("original_contract", {})
    legal = record.get("legal_clauses", {})
    notar = record.get("notarization", {})

    address = prop.get("address", "Unknown")
    city = prop.get("city", "")
    county = prop.get("county", "")

    # Verdict: MAO is the hard line
    if metrics["asking_price"] > metrics["mao"]:
        verdict = "REJECT"
    elif metrics["spread_pct"] < 3:
        verdict = "REJECT"
    elif metrics["asking_price"] > metrics["mao"] * 0.95:
        verdict = "CAUTION"
    else:
        verdict = "PROCEED"

    reasoning_steps = [
        {
            "step": 1,
            "title": "Property Identification & Market Context",
            "reasoning": (
                f"Subject property: {address}, {city}, {county} County, DE. "
                f"Type: {metrics['property_type']} | {metrics['bedrooms']}BR/{metrics['bathrooms']}BA | "
                f"{metrics['sqft']} sqft | Built {metrics['year_built']}. "
                f"Located in {county} County — "
                f"{'economic hub, highest median values in DE' if county == 'New Castle' else 'secondary market, moderate values' if county == 'Kent' else 'coastal/rural, seasonal demand premium'}."
            ),
        },
        {
            "step": 2,
            "title": "After Repair Value (ARV) Estimation",
            "reasoning": (
                f"{metrics['arv_rationale']}\n"
                f"Estimated ARV: ${metrics['arv']:,}"
            ),
        },
        {
            "step": 3,
            "title": "Rehab Cost Estimation",
            "reasoning": (
                f"{metrics['rehab_rationale']}\n"
                f"Condition assessment: {metrics['condition']}. "
                f"Total estimated rehab: ${metrics['rehab_cost']:,}"
            ),
        },
        {
            "step": 4,
            "title": "Maximum Allowable Offer (MAO) Calculation",
            "reasoning": (
                f"{metrics['mao_rationale']}\n\n"
                f"The 70% rule ensures 30% gross margin after rehab to cover "
                f"holding costs, closing costs, and profit."
            ),
        },
        {
            "step": 5,
            "title": "MAO vs. Asking Price — The Brutal Comparison",
            "reasoning": (
                f"Assignor's total price to assignee: ${metrics['asking_price']:,}\n"
                f"Maximum Allowable Offer (MAO): ${metrics['mao']:,}\n"
                f"Delta: ${metrics['mao_delta']:,} "
                f"({'OVER' if metrics['mao_delta'] > 0 else 'UNDER'} MAO by "
                f"{abs(metrics['mao_delta_pct']):.1f}%)\n\n"
                + (
                    f"FAIL: Asking price EXCEEDS MAO by ${metrics['mao_delta']:,}. "
                    f"At this price, the deal has NEGATIVE margin after rehab. "
                    f"The assignor's fee of ${metrics['assignment_fee']:,} is eating "
                    f"into what should be the investor's profit. Walk away."
                    if metrics["asking_price"] > metrics["mao"]
                    else
                    f"PASS: Asking price is ${abs(metrics['mao_delta']):,} BELOW MAO. "
                    f"Sufficient margin exists for rehab + holding costs + profit. "
                    f"Built-in equity at close: ${abs(metrics['mao_delta']):,}."
                )
            ),
        },
        {
            "step": 6,
            "title": "Deal Structure Breakdown",
            "reasoning": (
                f"Original contract: ${metrics['purchase_price']:,} with "
                f"${metrics['earnest_money']:,} earnest deposit.\n"
                f"Assignment fee: ${metrics['assignment_fee']:,} "
                f"(spread: {metrics['spread_pct']:.1f}% of purchase price).\n"
                f"Total price to assignee: ${metrics['total_to_assignee']:,}.\n"
                f"Price per sqft: ${metrics['price_per_sqft']:.2f}.\n"
                f"ROI on assignor's earnest: {metrics['roi_on_earnest']:.1f}%."
            ),
        },
        {
            "step": 7,
            "title": "Risk Assessment",
            "reasoning": "\n".join(f"• {r}" for r in risks),
        },
        {
            "step": 8,
            "title": "Legal Compliance Check",
            "reasoning": (
                f"Assignability clause: {'Present and permissive' if legal.get('assignability_clause') else 'MISSING — critical gap'}. "
                f"Governing law: {'Delaware confirmed' if 'Delaware' in legal.get('governing_law', '') else 'Non-DE or missing'}. "
                f"Notarization: {'Recorded' if notar.get('recording_ref') else 'Not recorded'}. "
                f"Dispute resolution: {'Binding arbitration' if 'arbitration' in legal.get('dispute_resolution', '').lower() else 'Litigation or unspecified'}. "
                f"Indemnification: {'In place' if legal.get('indemnification') else 'Missing — assignor exposed'}."
            ),
        },
        {
            "step": 9,
            "title": "Final Verdict",
            "reasoning": (
                f"{'REJECT' if verdict == 'REJECT' else 'CAUTION' if verdict == 'CAUTION' else 'PROCEED'}: "
                + (
                    f"Asking price of ${metrics['asking_price']:,} EXCEEDS the MAO of "
                    f"${metrics['mao']:,}. This deal destroys capital. "
                    f"The assignor is pricing in ${metrics['assignment_fee']:,} of fee "
                    f"that pushes the total above what the numbers support. "
                    f"Renegotiate to below ${metrics['mao']:,} or WALK."
                    if verdict == "REJECT"
                    else
                    f"Asking price ${metrics['asking_price']:,} is within MAO "
                    f"(${metrics['mao']:,}) with ${abs(metrics['mao_delta']):,} margin. "
                    + (
                        "Margin is thin — one rehab surprise kills the deal. Proceed only with locked contractor bids."
                        if verdict == "CAUTION"
                        else
                        "Solid margin for rehab + profit. Recommend proceeding to close with full due diligence."
                    )
                )
            ),
        },
    ]

    return {
        "reasoning_chain": reasoning_steps,
        "metrics": {k: v for k, v in metrics.items() if not k.endswith("_rationale")},
        "risk_factors": risks,
        "verdict": verdict,
    }


def build_training_sample(record: dict, filename: str) -> dict:
    cot = generate_cot_reasoning(record)
    prop = record.get("property", {})
    address = prop.get("address", "Unknown")
    city = prop.get("city", "")
    county = prop.get("county", "")

    instruction = (
        f"Analyze this Delaware wholesale real estate assignment contract for "
        f"the property at {address}, {city}, {county} County. "
        f"Estimate the ARV, calculate rehab costs, determine the Maximum Allowable Offer "
        f"using (ARV × 0.70) - Rehab Costs, compare against the asking price, "
        f"and provide a PROCEED/REJECT verdict with full mathematical reasoning."
    )

    input_context = json.dumps(record, indent=2)

    reasoning_text = "\n\n".join(
        f"## Step {step['step']}: {step['title']}\n{step['reasoning']}"
        for step in cot["reasoning_chain"]
    )

    output = (
        f"<reasoning>\n{reasoning_text}\n</reasoning>\n\n"
        f"<verdict>{cot['verdict']}</verdict>\n"
        f"<metrics>{json.dumps(cot['metrics'])}</metrics>"
    )

    return {
        "id": record.get("record_id", filename.replace(".json", "")),
        "instruction": instruction,
        "input": input_context,
        "output": output,
        "reasoning": cot["reasoning_chain"],
        "metrics": cot["metrics"],
        "risk_factors": cot["risk_factors"],
        "verdict": cot["verdict"],
        "_meta": {
            "source_file": filename,
            "synthesized_at": datetime.now(tz=None).astimezone().isoformat(),
            "model": "meta.llama3-3-70b-instruct-v1:0",
            "format": "chain-of-thought-v2-mao",
        },
    }


def validate_record(record: dict, filename: str) -> Optional[str]:
    required_keys = ["property", "original_contract", "assignment"]
    for key in required_keys:
        if key not in record or not isinstance(record[key], dict):
            return f"Missing or invalid required key: '{key}'"

    prop = record["property"]
    if not prop.get("address"):
        return "property.address is empty or missing"

    orig = record["original_contract"]
    if not orig.get("purchase_price") or orig["purchase_price"] <= 0:
        return "original_contract.purchase_price is missing or <= 0"

    asg = record["assignment"]
    if not asg.get("assignment_fee") or asg["assignment_fee"] <= 0:
        return "assignment.assignment_fee is missing or <= 0"

    return None


def main():
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    if not os.path.isdir(RAW_SCRAPES_DIR):
        print(f"ERROR: raw_scrapes directory not found: {RAW_SCRAPES_DIR}")
        sys.exit(1)

    files = sorted(f for f in os.listdir(RAW_SCRAPES_DIR) if f.endswith(".json"))

    if not files:
        print(f"ERROR: No JSON files found in {RAW_SCRAPES_DIR}")
        sys.exit(1)

    print(f"{'='*70}")
    print(f"  COT SYNTHESIZER v2 — MAO-Based Valuation Training Data")
    print(f"  Source: {RAW_SCRAPES_DIR} ({len(files)} files)")
    print(f"  Output: {OUTPUT_PATH}")
    print(f"  Verdict Logic: asking_price > MAO → REJECT")
    print(f"{'='*70}\n")

    success = 0
    errors = 0
    rejects = 0
    error_entries = []

    with open(OUTPUT_PATH, "w") as out_f:
        for filename in files:
            filepath = os.path.join(RAW_SCRAPES_DIR, filename)

            try:
                with open(filepath, "r") as f:
                    record = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                error_msg = f"[PARSE ERROR] {filename}: {e}"
                error_entries.append(error_msg)
                errors += 1
                print(f"  SKIP  {filename} — malformed JSON")
                continue
            except Exception as e:
                error_msg = f"[READ ERROR] {filename}: {e}"
                error_entries.append(error_msg)
                errors += 1
                print(f"  SKIP  {filename} — read error")
                continue

            validation_error = validate_record(record, filename)
            if validation_error:
                error_msg = f"[VALIDATION] {filename}: {validation_error}"
                error_entries.append(error_msg)
                errors += 1
                print(f"  SKIP  {filename} — {validation_error}")
                continue

            try:
                sample = build_training_sample(record, filename)
                out_f.write(json.dumps(sample) + "\n")
                success += 1

                verdict = sample["verdict"]
                if verdict == "REJECT":
                    rejects += 1
                m = sample["metrics"]
                print(
                    f"  [{success:03d}] {sample['id']:<16} "
                    f"{verdict:<8} "
                    f"ARV=${m['arv']:>9,} "
                    f"Rehab=${m['rehab_cost']:>7,} "
                    f"MAO=${m['mao']:>9,} "
                    f"Ask=${m['asking_price']:>9,} "
                    f"Δ=${m['mao_delta']:>+8,}"
                )
            except Exception as e:
                error_msg = f"[SYNTHESIS ERROR] {filename}: {traceback.format_exc()}"
                error_entries.append(error_msg)
                errors += 1
                print(f"  SKIP  {filename} — synthesis error: {e}")

    if error_entries:
        with open(ERROR_LOG_PATH, "w") as err_f:
            err_f.write(f"CoT Synthesizer Error Log — {datetime.now(tz=None).astimezone().isoformat()}\n")
            err_f.write(f"{'='*70}\n\n")
            for entry in error_entries:
                err_f.write(entry + "\n")

    print(f"\n{'='*70}")
    print(f"  COMPLETE: {success} samples | {rejects} REJECT | {success - rejects} PROCEED/CAUTION | {errors} errors")
    if errors:
        print(f"  Error log: {ERROR_LOG_PATH}")
    print(f"  Output: {OUTPUT_PATH}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
