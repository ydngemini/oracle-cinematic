# Neoh — Release Checklist

Short on purpose. A checklist nobody finishes is a checklist nobody uses.

Most of this is enforced by CI and will stop the deploy on its own. The boxes
exist for the parts a machine cannot check, and so that skipping one is a
decision somebody made rather than a step that quietly did not happen.

---

## Once, before the first production deploy

- [ ] **Create the `production` environment with a required reviewer.** The
      workflow only *names* it. On 2026-09-25 this repository had no
      environments at all, and GitHub silently auto-creates a missing one with
      **no protection rules** — so until this is done, the approval gate is not
      a gate. Settings → Environments → New environment `production` →
      Required reviewers, and restrict deployment branches to `main`. Or:
      ```sh
      gh api -X PUT repos/ydngemini/oracle-cinematic/environments/production \
        -F 'reviewers[][type]=User' -F 'reviewers[][id]=<your user id>' \
        -F 'deployment_branch_policy[protected_branches]=false' \
        -F 'deployment_branch_policy[custom_branch_policies]=true'
      ```
- [ ] **Put the deploy secrets on that environment**, not on the repository —
      `DIGITALOCEAN_ACCESS_TOKEN`, `DIGITALOCEAN_REGISTRY`,
      `DIGITALOCEAN_APP_ID`, the `ORACLE_DB_*` admin credentials, the
      `NEOH_PUBLIC_*` / `VITE_*` values. Environment secrets are only released
      to a job after its reviewer approves; repository secrets are released to
      any job that asks.

## Before

- [ ] **CI green on the exact commit being deployed** — not "on main", on *this* SHA.
- [ ] **Migration risk reviewed.** Run it and read the answer:
      ```sh
      cd backend && python -c "
      import pathlib; from migration_safety import classify_files
      new = [p for p in sorted(pathlib.Path('db/migrations').glob('*.sql')) if p.name > '<last deployed head>']
      for v in classify_files(new): print(v.classification, v.filename, v.reasons)"
      ```
      Anything not `additive` means **this release cannot be rolled back by
      redeploying the previous one.** That is allowed — it is sometimes the
      right call — but decide it now, in daylight, not during an incident.
- [ ] **A current backup exists.** `scripts/backup-postgres.sh`. DigitalOcean's
      own PITR window is 7 days and its backups cannot be downloaded, so the
      logical backup is the only copy Neoh actually holds.
- [ ] **The encryption master key is escrowed somewhere other than the
      database.** A restore without it succeeds and returns *blank* data for 26
      columns across 18 tables — no error. See the DR state map.
- [ ] **Secrets present in the `production` GitHub Environment** — the deploy
      fails late and confusingly if one is missing.
- [ ] **Someone is available to watch it.** Not a formality: the observation
      window below is the point.

## Deploy

- [ ] **Production approval** — Actions → CI → Run workflow, `confirm: deploy`.
      There is no automatic deploy on merge, deliberately.
- [ ] CI captures the rollback target, resolves digests, writes the release
      manifest, and runs the migration precheck **before** touching the
      database. If the precheck fails, stop — do not override it. It is
      telling you the database is not the one this release expects.
- [ ] Migrations run once, from the built image, before the new image serves
      traffic.
- [ ] The spec is applied with both digests substituted.
- [ ] **Smoke test passes** — `GET /version` must report *this* release's
      `git_sha`. If it reports something else, the deploy did not land.

## After

- [ ] **Download and keep the release manifest artifact.** It is the rollback
      target for whatever ships next, and the only record of which digest is
      live.
- [ ] **Observation window — 30 minutes minimum.** Watch error rate, latency,
      and the worker actually picking up jobs. Most bad releases are obvious
      inside ten minutes and invisible in the deploy log.
- [ ] **Re-run the smoke test at the end of the window**, not just at the
      start. A release that boots and then degrades passes the first one.
- [ ] Record the release: git SHA, both digests, migration head, who deployed,
      and the result. The GitHub run plus its artifacts is that record — no
      separate database table is needed for it.

## If it goes wrong

1. **Do not restore the database.** Not as a first move, possibly not at all.
2. `scripts/rollback.sh --to <previous-manifest.json>` — dry run first. It
   tells you whether an application-only rollback is even safe.
3. If it prints **`APPLICATION ROLLBACK UNSAFE`**, believe it. A corrective
   forward migration is usually faster and always less destructive than going
   backwards. See the Rollback section of `docs/deploy-digitalocean.md`.
4. Only then consider PITR, and read `docs/disaster-recovery-state-map.md`
   before starting: a restore creates a **new** cluster that must be
   repointed, and it loses every write since the restore point.

---

**The one thing worth remembering:** the checks in this pipeline are not
ceremony. Every one of them exists because something slipped past its absence
— the app spec that rebuilt from source while claiming to pin a digest, the
version endpoint that reported intent instead of fact, the migration recorded
as applied whose file was never in the repository. When a check fails, it has
usually noticed something real.
