# Legal Sentinel — Delaware Property Law RAG

This directory holds the source documents for the Legal Sentinel agent.
The Underwriter model handles MAO math; the Legal Sentinel handles statutory compliance.

## Data Sources (place PDFs in `data/`)

Priority documents:
- Delaware Code Title 25 (Property) — Chapter 27: Landlord-Tenant Code
- Delaware Code Title 25 — Chapter 61: Conveyances
- Delaware Code Title 6 — Chapter 25: Assignment of Contract Rights (UCC Article 9 analogues)
- Delaware Real Estate Commission regulations
- New Castle County recording requirements

## How it works

1. PDFs are chunked and embedded (bge-small-en, 384-dim)
2. At query time, the Legal Sentinel retrieves top-k chunks relevant to the deal
3. The Underwriter's verdict is cross-checked against retrieved law
4. If a legal red flag is found, the deal is flagged regardless of MAO math

## Integration

The Legal Sentinel does NOT override the Underwriter's MAO math.
It adds a second gate: a deal must pass BOTH the 70% rule AND the legal check.
