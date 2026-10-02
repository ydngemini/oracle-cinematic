# Runbook: GPU provider (RunPod) failing

**What Neoh does:**
- Reconstruction jobs fail honestly or wait; there is **no automatic resubmission**, so there is no duplicate GPU spend.
- **Original photos and video are always kept.**
- A job stuck `running` with no progress for `ORACLE_RECON_STALL_HOURS` (6 h) becomes "timed out — needs attention".
- Pods left behind by a lost response are reaped by name and age.

**Do:**
1. Check RunPod status and the balance.
2. Resubmit failed jobs deliberately from the property's 3D tab once the provider is back.
3. Check the RunPod console for pods older than 4 h with a `neoh-` name, and terminate them if the reaper has not.
