"""The deployed artifact must be the artifact that was built and tested.

The gap these tests exist to close, found by auditing the release path:

  CI builds `neoh-backend:<sha>` and `neoh-frontend:<sha>`, pushes both to the
  DigitalOcean Container Registry, runs the migrations using the backend image
  — and then deploys with `doctl apps update --spec infra/digitalocean/app.yaml`.

  That spec defines all three components with `dockerfile_path` + `source_dir`
  and contains no image reference of any kind. So App Platform BUILDS ITS OWN
  IMAGE from source at deploy time. The two images CI pushed are never
  deployed; they are built, used once for migrations, and orphaned.

  A comment directly above the deploy step asserts the opposite — "pinning both
  the api service and the worker to this exact image digest — not a moving tag
  — so what App Platform deploys is provably what was just built". That comment
  is false, and a false comment is worse than no comment, because it stops the
  next person checking.

The consequence is not theoretical. `/version` reports `ORACLE_GIT_SHA`, which
`backend/Dockerfile` defaults to `unknown` and CI supplies via `--build-arg`.
App Platform's own rebuild passes no build args, so a deploy that way serves
`git_sha=unknown` — and `smoke-test.sh` fails on exactly that, with exactly the
right diagnosis. The only reason nobody has seen it is that the deploy job is
gated behind a manual `workflow_dispatch` with `confirm=deploy`.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
APP_SPEC = REPO / "infra" / "digitalocean" / "app.yaml"
CI = REPO / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def spec() -> str:
    return APP_SPEC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ci() -> str:
    return CI.read_text(encoding="utf-8")


def _uncommented(text: str) -> str:
    """App-spec and workflow comments discuss images at length. Only the
    actual YAML keys count — that discrepancy is the whole bug."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_the_app_spec_and_ci_agree_on_where_the_image_comes_from(spec, ci):
    """The core invariant: if CI builds and pushes an image, the platform must
    deploy THAT image. Building in both places means production runs an
    artifact that nothing tested."""
    ci_builds_images = "docker push" in ci
    spec_body = _uncommented(spec)
    spec_builds_from_source = "dockerfile_path:" in spec_body

    assert not (ci_builds_images and spec_builds_from_source), (
        "CI builds and pushes container images, but the App Platform spec still "
        "declares `dockerfile_path`, so the platform rebuilds from source at "
        "deploy time. Production would run an image that never went through CI, "
        "and the images CI pushed would be orphaned in the registry."
    )


def test_every_deployable_component_pins_a_registry_image(spec):
    """Each service / worker / static site must name a registry image."""
    body = _uncommented(spec)
    assert "image:" in body, "no component references a registry image"
    # One `image:` block per deployable component: api, worker, web.
    assert body.count("registry_type:") >= 3, (
        "every deployable component (api, worker, web) must pin an image; "
        f"found {body.count('registry_type:')}"
    )


def test_nothing_deployable_is_pinned_to_a_moving_tag(spec):
    """`:latest` may exist for developer convenience. It may never be what a
    production decision resolves against — the whole point of an immutable
    release is that the answer cannot change between the decision and the
    rollout."""
    body = _uncommented(spec)
    assert "latest" not in body, "the app spec resolves a moving tag"


def test_the_deploy_step_substitutes_the_release_identity(ci):
    """The spec is a template; the deploy must stamp this release's digest into
    it. A spec committed with a hardcoded digest would deploy that digest
    forever."""
    body = _uncommented(ci)
    assert "DIGEST" in body or "digest" in body, (
        "the deploy step never resolves an image digest"
    )


def test_ci_verifies_the_digest_it_pushed(ci):
    """A tag can be moved after the push. The digest is what makes the artifact
    immutable, so the deploy has to read it back from the registry rather than
    assume it."""
    body = _uncommented(ci)
    assert re.search(r"docker\s+(image\s+)?inspect|RepoDigests|manifest inspect", body), (
        "CI never reads back the digest of the image it pushed"
    )


def test_the_version_endpoint_can_actually_report_the_release(ci):
    """`/version` reads ORACLE_GIT_SHA, which the Dockerfile defaults to
    `unknown`. It is only correct if whoever BUILDS the image passes the build
    arg — so the build that produces the deployed artifact must pass it."""
    assert "--build-arg GIT_SHA=" in ci or "GIT_SHA=" in ci, (
        "no build passes GIT_SHA, so /version would report 'unknown' and the "
        "smoke test would fail with no way to tell which release is live"
    )


def test_no_comment_claims_an_immutability_the_spec_does_not_provide(ci):
    """The audit's sharpest finding was a comment asserting digest pinning
    above a step that did no such thing. Prose that contradicts the code is how
    a gap survives review."""
    claims_digest_pinning = "exact image digest" in ci
    if claims_digest_pinning:
        spec_body = _uncommented(APP_SPEC.read_text(encoding="utf-8"))
        assert "digest" in spec_body or "image:" in spec_body, (
            "a CI comment claims the deploy pins an exact image digest, but the "
            "app spec it applies contains no image reference at all"
        )


# ── Rollback ───────────────────────────────────────────────────────────────

ROLLBACK = REPO / "scripts" / "rollback.sh"


@pytest.fixture(scope="module")
def rollback() -> str:
    return ROLLBACK.read_text(encoding="utf-8")


def test_rollback_script_exists_and_is_executable():
    assert ROLLBACK.exists() and ROLLBACK.stat().st_mode & 0o111


def test_rollback_never_touches_the_database(rollback):
    """It redeploys an artifact. Restoring a database is a different decision
    with a different blast radius, and it belongs to a human holding the
    disaster-recovery runbook."""
    code = _uncommented(rollback)
    for forbidden in ("pg_restore", "psql", "DROP ", "restore-postgres"):
        assert forbidden not in code, f"rollback.sh references {forbidden}"


def test_rollback_refuses_across_a_destructive_migration(rollback):
    """The whole point. Redeploying the old app onto an incompatible schema
    turns one broken release into a broken release AND a broken database."""
    assert "APPLICATION ROLLBACK UNSAFE" in rollback
    assert "rollback_is_safe" in rollback or "migration_safety" in rollback
    assert "exit 3" in rollback, "an unsafe rollback must exit non-zero"


def test_rollback_leaves_additive_migrations_in_place(rollback):
    """Down-migrations are not attempted. Rolling the app back and leaving an
    additive migration is the preferred pattern."""
    assert "no down-migration is attempted" in rollback
    code = _uncommented(rollback)
    # No attempt to run a reversal: no `*_down.sql`, no `migrate down`.
    assert "_down.sql" not in code
    assert "migrate down" not in code.lower()


def test_rollback_uses_a_digest_not_a_rebuild(rollback):
    """Rebuilding old source produces a different artifact, which is not a
    rollback — it is a new release that happens to have old code in it."""
    assert "backend_digest" in rollback and "frontend_digest" in rollback
    code = _uncommented(rollback)
    assert "docker build" not in code


def test_rollback_is_dry_run_by_default(rollback):
    """The default has to be the safe one: an operator reaching for this is
    under pressure and may not have read the flags."""
    assert "--apply" in rollback
    assert "Nothing has been changed" in rollback


def test_rollback_avoids_the_pinning_rollback_api(rollback):
    """DigitalOcean's rollback endpoint PINS the app, blocking every later
    deploy until someone commits or reverts — a second incident waiting for the
    moment the fix-forward release is ready and will not deploy."""
    code = _uncommented(rollback)
    # The DigitalOcean rollback ENDPOINT, not this script's own filename —
    # `scripts/rollback.sh` in the usage text contains the substring "/rollback".
    assert "apps/{app_id}/rollback" not in code
    assert "/v2/apps/" not in code
    assert "doctl apps update" in code


def test_ci_captures_the_rollback_target_before_deploying(ci):
    """Decided before the incident, not discovered during it."""
    assert "Capture the rollback target" in ci
    capture = ci.index("Capture the rollback target")
    deploy = ci.index("Deploy to App Platform")
    assert capture < deploy, "the target must be captured before it is replaced"
