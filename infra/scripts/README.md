# infra/scripts — LEGACY AWS

**Nothing in this directory deploys Neoh production.** Production runs on
DigitalOcean App Platform; see `docs/deploy-digitalocean.md`.

Every script here targets the retired AWS ECS stack and refuses to run unless
`NEOH_LEGACY_AWS=1` is set. Several still describe themselves as deploying "to
prod" — that was true once. The guard exists because a script whose own header
says "prod" will eventually be run by someone who believes it.

| Production task | Use this instead |
|---|---|
| deploy | GitHub Actions → CI → Run workflow on `main`, `confirm: deploy` |
| migrations | run by the deploy job, after `scripts/migration-precheck.sh` |
| smoke test | `infra/digitalocean/smoke-test.sh` |
| rollback | `scripts/rollback.sh --to <release-manifest.json>` |
| backup / restore | `scripts/backup-postgres.sh`, `scripts/restore-postgres.sh` |

Kept rather than deleted: the AWS account and its Terraform state may still
exist, and deleting the only tooling that operates them would make that harder
to clean up, not easier.
