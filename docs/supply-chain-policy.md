# Neoh — Supply-Chain Policy

What ships, how it is scanned, and what blocks a release.

## What actually ships

| Artifact | Scanned as | Why |
|---|---|---|
| Backend image (`neoh-backend`) | the built image — OS packages + Python deps | this is the running process |
| Frontend | the npm **lockfile** | production serves only the static bundle: DigitalOcean copies `output_dir` to its CDN, so the image's nginx and node layers never run there |

The frontend's **build-stage** `node` image is deliberately *not* the scan
target. On 2026-09-25 it carried 1 CRITICAL (`tar`, CVE-2026-59873) and 10
HIGH — none of which reach production, because that stage is discarded.
Scanning it would block every release on code that never ships.

## Baseline, measured 2026-09-25 (Trivy 0.58.1, fixable only)

| Target | CRITICAL | HIGH |
|---|---:|---:|
| backend Python deps (35 pinned packages) | 0 | 0 |
| `python:3.12-slim` base | 0 | 0 |
| frontend npm lockfile | 0 | 0 |
| `node:24.18.0-alpine` (build stage, not shipped) | 1 | 10 |
| `nginx:1.30.4-alpine` (not executed in production) | 0 | 8 |

## What blocks a release

- **A fixable CRITICAL in anything that ships blocks the `release` job.** Before
  staging, so it can never reach production.
- **HIGH** is printed in the job log and not blocking. Fix within the next
  release cycle.
- **Unfixed** vulnerabilities (no patched version exists) are not counted.
  There is nothing to upgrade to; they are tracked through the SBOM.

## Pinning

- **Base images are pinned by digest** (`python:3.12-slim@sha256:…`). A tag is
  repointed upstream whenever the image is rebuilt, so unpinned builds picked
  up whatever the tag meant that day and were not reproducible. Upstream
  patches therefore no longer arrive by accident. **The scan is the refresh
  signal**: when it reports a fixable CRITICAL, resolve the tag's current
  digest and update the `FROM` line.
- **Trivy itself is pinned by digest.** An unpinned scanner is a supply-chain
  hole of its own.
- **npm:** `package-lock.json` + `npm ci`. Exact.
- **Python:** 35 of 37 requirements are exact pins. `numpy>=1.26,<3`
  (deliberately major-open for opencv) and `eth-account>=0.13` are ranges, and
  a scanner cannot match a range to a version, so **those two are invisible to
  the scan.** Recommended next step: a compiled lock (`pip-compile`) so every
  transitive version is pinned and scanned.
- **Promotion never builds**, so nothing unpredictable is pulled at
  production-promotion time. Production deploys the digests staging verified.

## SBOMs

Every release uploads `sbom-<sha>` (CycloneDX: `sbom-backend.cdx.json`,
`sbom-frontend.cdx.json`), kept 400 days. When a new CVE is announced, "are we
exposed?" becomes a lookup in the SBOM of the running release, not a rebuild.

## Refreshing a base image digest

```sh
repo=python tag=3.12-slim
tok=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/$repo:pull" | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
curl -sI -H "Authorization: Bearer $tok" \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
  "https://registry-1.docker.io/v2/library/$repo/manifests/$tag" | grep -i docker-content-digest
```

## DigitalOcean API token

Use the narrowest token DigitalOcean offers for each environment: App
Platform update/read and Container Registry read/write. Keep a **separate
token per GitHub environment**, so the staging token cannot touch the
production app even if both apps live in one account.
