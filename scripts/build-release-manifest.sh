#!/usr/bin/env bash
#
# Neoh — emit the release manifest for one immutable release.
#
# One release, one identity, written down before anything is deployed. The
# manifest is what makes "promote the artifact that passed staging" a checkable
# claim rather than an intention: staging records a digest, production asserts
# the same digest, and a mismatch is a hard stop instead of a shrug.
#
# It records the DIGEST, not just the tag. A tag is a mutable pointer — it can
# be moved between the moment CI decides to deploy and the moment the platform
# pulls. The digest cannot; it IS the content. `:latest` appears nowhere in a
# production decision, and the tag is recorded only so a human can find the
# image in a registry UI.
#
# Contains no secrets, by construction and by test: only identities, digests
# and hashes.
#
# Usage:
#   REGISTRY=registry.digitalocean.com/<reg> GIT_SHA=$(git rev-parse HEAD) \
#     scripts/build-release-manifest.sh > release-manifest.json

set -euo pipefail

die() { printf '\n  ERROR: %s\n\n' "$*" >&2; exit 1; }

: "${REGISTRY:?REGISTRY is required (registry.digitalocean.com/<registry-name>)}"
: "${GIT_SHA:?GIT_SHA is required — a release has no identity without it}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GIT_REF="${GIT_REF:-$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)}"
BUILT_AT="${BUILT_AT:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
RUN_ID="${GITHUB_RUN_ID:-local}"
RUN_ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"

BACKEND_TAG="${REGISTRY}/neoh-backend:${GIT_SHA}"
FRONTEND_TAG="${REGISTRY}/neoh-frontend:${GIT_SHA}"

# ── Digests, read back from the registry ────────────────────────────────────
#
# Read back rather than assumed. The point of a digest is that it is the
# content's own name; taking CI's word for it would reintroduce exactly the
# trust gap the digest exists to close.
#
# `docker buildx imagetools inspect` queries the REGISTRY, so it reports what
# was actually stored. `docker inspect .RepoDigests` reads the local daemon's
# record of the push and is the fallback when buildx is unavailable.
#
# The repository is taken with `${ref%:*}` (strip from the LAST colon), not
# `${ref%%:*}` (the first). A registry with a port — localhost:5000/team/img:tag
# — turns the first-colon form into "localhost", which then matches nothing and
# the digest silently comes back empty.

digest_of() {
  local ref="$1" digest=""

  if command -v docker >/dev/null 2>&1; then
    digest="$(docker buildx imagetools inspect "$ref" --format '{{.Manifest.Digest}}' 2>/dev/null || true)"
    if [ -z "$digest" ]; then
      digest="$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$ref" 2>/dev/null \
                | grep -F "${ref%:*}@" | head -1 | cut -d'@' -f2 || true)"
    fi
  fi

  if [ -z "$digest" ]; then
    die "could not resolve a digest for $ref.

  Without it this release has no immutable identity, and 'promote the artifact
  that passed staging' becomes an assertion nobody can check. Push the image
  first, and make sure this runner can reach the registry."
  fi
  case "$digest" in
    sha256:*) ;;
    *) die "resolved '$digest' for $ref, which is not a sha256 digest" ;;
  esac
  printf '%s' "$digest"
}

BACKEND_DIGEST="$(digest_of "$BACKEND_TAG")"
FRONTEND_DIGEST="$(digest_of "$FRONTEND_TAG")"

# ── Migration identity ──────────────────────────────────────────────────────
#
# The migration SET is part of the release, not a detail of it. Two builds of
# the same git SHA always carry the same migrations; recording the head and an
# aggregate hash lets a deploy assert that the database it is about to touch
# is the one this release was built against.
#
# Sorted by filename so the hash is stable across filesystems — `find` order is
# not, and an aggregate hash that changes for no reason is an aggregate hash
# nobody trusts.
MIGRATIONS_DIR="$REPO_ROOT/backend/db/migrations"
[ -d "$MIGRATIONS_DIR" ] || die "no migrations directory at $MIGRATIONS_DIR"

MIGRATION_HEAD="$(ls "$MIGRATIONS_DIR"/*.sql 2>/dev/null | xargs -n1 basename | sort | tail -1)"
MIGRATION_COUNT="$(ls "$MIGRATIONS_DIR"/*.sql 2>/dev/null | wc -l | tr -d ' ')"
MIGRATIONS_SHA256="$(ls "$MIGRATIONS_DIR"/*.sql | sort | xargs cat | sha256sum | cut -d' ' -f1)"

[ -n "$MIGRATION_HEAD" ] || die "found no migrations to hash"

cat <<JSON
{
  "release_id": "$GIT_SHA",
  "git_sha": "$GIT_SHA",
  "git_ref": "$GIT_REF",
  "built_at": "$BUILT_AT",

  "backend_tag": "$BACKEND_TAG",
  "backend_digest": "$BACKEND_DIGEST",
  "backend_pinned": "${REGISTRY}/neoh-backend@${BACKEND_DIGEST}",

  "frontend_tag": "$FRONTEND_TAG",
  "frontend_digest": "$FRONTEND_DIGEST",
  "frontend_pinned": "${REGISTRY}/neoh-frontend@${FRONTEND_DIGEST}",

  "migration_head": "$MIGRATION_HEAD",
  "migration_count": $MIGRATION_COUNT,
  "migrations_sha256": "$MIGRATIONS_SHA256",

  "expected_api_version": "$GIT_SHA",
  "expected_frontend_version": "$GIT_SHA",

  "build_run_id": "$RUN_ID",
  "build_run_attempt": "$RUN_ATTEMPT",

  "__note": "Digests, not tags, are what deployment pins. A tag can be moved between the decision to deploy and the pull that follows it; a digest is the content's own name. No secret appears in this file by construction — only identities, digests and hashes."
}
JSON
