# training_data/ — legacy, heuristic, synthetic

These six files are kept as they were committed; they have not been regenerated
or edited. Read this before using any of them (audit A08; ledger L11-05, L11-06).

| File | Rows | Shape |
|---|---|---|
| `swarm_textbook.jsonl` | 100 | `instruction` / `input` / `output` / `reasoning` / `metrics` / `verdict` / `_meta` |
| `swarm_textbook_bedrock.jsonl` | 100 | `prompt` / `completion` |
| `swarm_textbook_bedrock_v2.jsonl` | 100 | `messages` |
| `swarm_textbook_bedrock_v3.jsonl` | 100 | `prompt` / `completion` |
| `swarm_textbook_bedrock_v4.jsonl` | 100 | `system` / `messages` |
| `swarm_textbook_bedrock_v5.jsonl` | 100 | `schemaVersion` / `system` / `messages` |

What they are:

- **Inputs are synthetic.** Every row is built from an LLM-generated assignment
  contract in `data/raw_scrapes/` (written by `backend/ml_forge/synthetic_lawyer.py`).
  The directory name notwithstanding, none of it was scraped from a real source.
- **Targets are heuristic.** The ARV, rehab, MAO and verdict in every row were
  computed by `backend/ml_forge/cot_synthesizer.py` from fixed, unsourced constants
  (a county $/sqft table, year-built rehab brackets, condition multipliers and the
  70% rule). No row was checked against a real sale.
- **Two statements inside the rows are wrong.** Every row says "County median
  ARV/sqft …", but the figure is an unsourced placeholder, not a median. In
  `swarm_textbook.jsonl`, `_meta.model` names `meta.llama3-3-70b-instruct-v1:0`,
  but these rows were computed by Python rules; no model wrote them. The generator
  has been corrected (L11-05, L11-06), but these files predate the fix.

Do not use these files to train, evaluate or promote a valuation model, and do not
present anything learned from them as an appraisal, an automated valuation, a market
value or a verified deal price. `backend/models_api.activate_model` and
`backend/ml_forge/edge_forge/local_vllm_adapter.EdgeUnderwriter` refuse an underwriter
whose model card does not declare observed-transaction targets.

`scripts/forge_model.py` (the legacy AWS Bedrock experiment) still points at
`training_data/swarm_textbook_bedrock.jsonl` in S3. Deleting or regenerating these
files is an owner decision.
