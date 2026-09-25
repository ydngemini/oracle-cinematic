# Neoh — Release Path Audit

**2026-09-25.** What the release process actually does, as opposed to what it
says it does.

---

## The finding: production would not deploy what CI built

The brief asked whether the current deployment is genuinely immutable, and said
not to assume it is. It is not. The gap is real, and it is wider than suspected.

### What CI does

`.github/workflows/ci.yml`, job `deploy`:

1. Builds `neoh-backend:<git-sha>` and `neoh-frontend:<git-sha>`.
2. Pushes both to the DigitalOcean Container Registry.
3. Runs the migrations **using the backend image it just built**.
4. Deploys with `doctl apps update --spec infra/digitalocean/app.yaml`.

### What the app spec says

All three deployable components — the `api` service, the `worker`, and the
`web` static site — are declared with:

```yaml
dockerfile_path: backend/Dockerfile
source_dir: /
```

There is **no image reference anywhere in the spec**. `grep` for `image`,
`registry`, `digest` or `sha256` finds only comments.

### Therefore

App Platform **builds its own image from source** at deploy time. The two
images CI pushed are built, used once for the migration step, and then
orphaned in the registry. Production runs an artifact that nothing tested.

This is precisely the *"CI tested image A, production rebuilt image B"* gap.

### What makes it worse than an oversight

A comment sits directly above the deploy step:

> Updates the App Platform app in place from the spec, **pinning both the api
> service and the worker to this exact image digest** — not a moving tag — so
> what App Platform deploys is provably what was just built and migrated
> against, not whatever `:latest` resolves to by the time the rollout actually
> happens.

Every clause of that is false of the spec it applies. Prose that contradicts
the code is worse than no prose, because it stops the next person checking.

---

## The second divergence: the frontend bakes a different API base

`oracle-app/Dockerfile` is a Vite build, so `VITE_*` values are **compiled into
the bundle** and cannot change afterwards.

| Built by | `VITE_API_BASE` comes from |
|---|---|
| CI | `secrets.NEOH_PUBLIC_API_BASE` |
| App Platform | `${api.PUBLIC_URL}` in the app spec |

So the two builds are not merely different images of the same code — they are
configured differently. Whichever one ships, the other was never what was
tested.

---

## The smoke test would NOT have caught it — correcting my own finding

My first pass through this concluded that the deploy would fail its own smoke
test with `git_sha=unknown`, because App Platform's rebuild passes no
`--build-arg GIT_SHA`. That was wrong, and the truth is worse.

The app spec also carried a **runtime** environment variable:

```yaml
- key: ORACLE_GIT_SHA
  value: ${_self.GIT_COMMIT_HASH}
```

A runtime env var overrides whatever the image baked in. So `/version` reported
the commit **App Platform thought it had deployed**, not the build that was
actually running — and `smoke-test.sh`, which compares that against the release
SHA, would have agreed every time regardless of which image was serving.

`GET /version` exists to answer *"which build is actually running."* That
override made it answer *"which commit we intended"* — a claim asserted rather
than derived. The one check capable of catching the rebuild was defeated by the
same spec that caused it.

It is the failure shape this codebase keeps producing: the MLS gates that were
built but never fed, the DR roles that were captured but never restored, and
now a version endpoint that reports intent instead of fact.

**Fixed by deletion.** The override is gone; `ORACLE_GIT_SHA`,
`ORACLE_APP_VERSION` and `ORACLE_BUILD_TIMESTAMP` now come only from the
image's own `ENV`, set by `--build-arg` in the build that produced it. An image
can then only ever report what it is.

## Why nobody has hit it

The `deploy` job is gated:

```yaml
if: github.event_name == 'workflow_dispatch' && github.event.inputs.confirm == 'deploy'
environment: production
```

Manual dispatch only, behind a `production` environment. It has almost
certainly never run against DigitalOcean.

`infra/digitalocean/smoke-test.sh` is strict and correct in itself — it fails
hard when `/version` reports `git_sha=unknown`. It simply never got the chance,
because the spec fed it the right answer from the wrong source. See the section
above.

---

## Inventory

| Asset | State |
|---|---|
| `.github/workflows/ci.yml` | present — builds and pushes, then deploys from source |
| `infra/digitalocean/app.yaml` | present — **no image references** |
| `infra/digitalocean/smoke-test.sh` | present, strict, correct |
| `infra/scripts/prod-smoke.sh` | present |
| `infra/scripts/run-migrations.sh` | present |
| `docs/deploy-digitalocean.md` | present |
| `GET /version` | present and well built — git_sha, app_version, built_at, live migration_head, process_role |
| staging app spec | **absent** |
| release manifest | **absent** |
| `.github/workflows/deploy.yml` | **absent** — referenced by a comment in `app.yaml` |

---

## Pinned by tests

`backend/tests/test_release_immutability.py` asserts the invariant rather than
the instance: if CI builds and pushes an image, the app spec may not declare
`dockerfile_path`. It also fails on a moving tag in the spec, on a deploy that
never resolves a digest, and — specifically — on a comment claiming digest
pinning above a spec that has no image reference.

All five failed against the state described above — which is the point of
writing them first. They were then marked `xfail(strict=True)` rather than
skipped, so that closing the gap would make them XPASS and report a failure
telling whoever fixed it to delete the marker. That is exactly what happened.

## Fixed

| Was | Now |
|---|---|
| `dockerfile_path` on all three components | `image:` with `registry_type: DOCR` and a `digest` |
| No digest anywhere | CI resolves both digests from the registry and substitutes them |
| Spec asserted `ORACLE_GIT_SHA` at runtime | removed — the image reports itself |
| Frontend baked a different `VITE_API_BASE` | one artifact, built once, with CI's values |
| No release manifest | `scripts/build-release-manifest.sh`, digests + migration hash |
| No migration precheck | `scripts/migration-precheck.sh`, aborts on drift |
| `health_check` only | readiness + `liveness_health_check`, and a `termination` drain/grace contract |

Two DigitalOcean constraints shaped this, both verified against current docs:
`deploy_on_push` **cannot coexist with `digest`**, so CI must drive deploys —
which is the intent, since nothing should reach production merely because an
image appeared in a registry. And `doctl apps update` does **not** re-pull when
the tag is unchanged; only a changed digest makes it deterministic.
