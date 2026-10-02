# Runbook: object storage (Spaces) failing

**What Neoh does:**
- Uploads and downloads fail with a bounded error. CRM records are unaffected.
- A media row records its object key only after a successful put, so nothing claims an object that does not exist.

**Do:**
1. Check DigitalOcean Spaces status and the access key.
2. After an outage, run `python scripts/privacy-orphan-audit.py` to find objects uploaded without a row (dry run), and decide per prefix.
