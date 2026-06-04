# Oracle — Security Hardening Map (Decision 007)

Enterprise hardening for the Neolithic backend. Split by enforcement layer —
**what lives where matters**, because none of the cluster/perimeter controls are
SQL. SQL-applicable items ship as migrations; the rest are config/IaC.

| Control | Layer | Artifact |
|---|---|---|
| FORCE RLS on all tenant tables | SQL | `backend/db/migrations/0001_init_tenancy.sql` |
| REVOKE PUBLIC, IAM login role, pgaudit | SQL | `backend/db/migrations/0003_hardening.sql` |
| Passwordless IAM token + TLS 1.3 + RLS ctx | App | `backend/db/connection.py` |
| `rds-db:connect` permission | IAM | `infra/iam/rds-connect-policy.json` |
| force_ssl / scram / pgaudit preload | Param group | this file, §1 |
| VPC endpoints, WAF, throttling | IaC | this file, §2–3 (Terraform TODO) |

---

## 1. Aurora cluster parameter group (NOT SQL)

`CREATE EXTENSION pgaudit` and TLS enforcement only work once these are set on
the Aurora PostgreSQL **cluster parameter group**, then the cluster rebooted:

```
shared_preload_libraries = pgaudit      # required before CREATE EXTENSION pgaudit
pgaudit.role             = pgaudit_reader  # object-audit role (created in 0003)
pgaudit.log              = ddl,role,write  # cluster-wide floor; per-role adds more
rds.force_ssl            = 1             # reject any non-TLS connection
password_encryption      = scram-sha-256 # kill MD5; new passwords use SCRAM
ssl_min_protocol_version = TLSv1.3       # no downgrade below 1.3
log_connections          = 1
log_disconnections       = 1
```

Also on the **cluster** itself (not the param group):
- Enable **IAM database authentication**.
- Enable **storage encryption** (KMS) + automated backups.
- Ship pgaudit output to **CloudWatch Logs** with a retention + immutability
  (log-group resource policy denying delete) so the audit trail is tamper-evident.

Apply order: param group → reboot → run migrations `0001` → `0002` → `0003`.

## 2. Zero-Trust VPC

- Aurora in **private subnets only**; no public IP, `PubliclyAccessible = false`.
- Fargate workers + Lambda reach Aurora over the internal AWS backbone via the
  RDS Data API / standard endpoint inside the VPC — never the public internet.
- **VPC interface endpoints** (PrivateLink) for the AWS APIs the backend calls
  (`rds`, `bedrock-runtime`, `secretsmanager`, `logs`, `sts`) so token minting
  and inference traffic stay on the backbone.
- Security group: Aurora ingress allows **only** the Fargate/Lambda SG on 5432.
  No `0.0.0.0/0`. No bastion with a public IP (use SSM Session Manager).

## 3. Edge defense

- **AWS WAF** on the API Gateway / ALB in front of the API: managed rule sets
  for SQLi + XSS + known bad bots (AWSManagedRulesCommonRuleSet,
  AWSManagedRulesSQLiRuleSet, AmazonIpReputationList).
- **Per-tenant throttling** at API Gateway via usage plans keyed on the JWT
  `tenant_id` (rate + burst), so one compromised agent can't fan out 5k
  valuations and bleed the Bedrock token budget. Pair with a per-tenant
  concurrency cap on the valuation endpoint.

## 4. Passwordless auth flow (how `connection.py` uses §1 + IAM)

1. Backend assumes its task role (the role holding `rds-connect-policy.json`).
2. `connection.py._iam_auth_token()` calls `rds.generate_db_auth_token(...)` →
   a signed, ~15-min token scoped to db user `oracle_app_login`.
3. asyncpg connects with that token as the password over TLS 1.3 (cert verified
   against the RDS CA bundle).
4. Aurora validates the token via IAM (the `rds_iam` grant from `0003`) — no
   static secret anywhere. Stolen source code yields zero usable credentials.

**Not yet done:** Terraform for §2–3. The migrations + `connection.py` are
runtime-untested (no Aurora instance wired). Smoke-test order in §1.
