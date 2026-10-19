# Neoh's Claude Agent SDK development runner

Local, opt-in developer tooling. It is NOT part of Neoh's shipping FastAPI
backend, React/Vite frontend, or DigitalOcean production stack.

## Setup

Requires Python 3.10+, Git, a clean checkout with synthetic test data, and
an Anthropic API key. Anthropic's Agent SDK quickstart documents API-key authentication; do not rely on a Claude subscription login for an application.

Run these commands on your own development machine. Until this branch is merged, switch to it after cloning:

    git clone https://github.com/ydngemini/oracle-cinematic.git
    cd oracle-cinematic
    git switch feat/neoh-agent-sdk-dev-runner-20261019
    python3 -m venv "$HOME/.cache/neoh-agent-venv"
    source "$HOME/.cache/neoh-agent-venv/bin/activate"
    python -m pip install -r tools/neoh-agent/requirements.txt

Set ANTHROPIC_API_KEY securely in your shell using a key from https://platform.claude.com/settings/keys.
A Claude Pro/Max subscription is not equivalent to API billing.
Never commit your key or store real customer data in the checkout.

## Recommended: automatic build, test, repair and push

The autonomous controller uses **build mode by default**. It starts from a
clean checkout, creates a **fresh** GitHub side branch for each task, edits
one Neoh category, runs fixed local test commands, retries failures (up to
two repair rounds by default), commits and pushes successful work to GitHub.

The main branch is NEVER pushed, merged or deployed by this controller.
The resulting remote branch is named like:

    auto/neoh-02-20261019T120000Z-abcdef

GitHub authentication must already be configured for this checkout's
origin, such as via SSH or GitHub CLI. The runner will only push when both
fetch and push remotes match ydngemini/oracle-cinematic on GitHub.

Start this from the repository root after installing the Python SDK and
Neoh's test dependencies:

    python tools/neoh-agent/autobuild.py \
      --category 02 \
      --task "Fix the next documented mobile UI defect with regression tests" \
      --budget-usd 12 \
      --max-repairs 2

You may launch from main or another clean branch. The controller creates
a NEW auto/neoh-* branch before the agent starts. To avoid including
unrelated commits in the resulting side branch, launch from an up-to-date
main after this runner's draft PR has been merged.

Outputs will include the new side-branch GitHub URL after a successful push.
No PR is opened automatically. You can then review the side branch, compare
it with main, and open a PR separately.

For an offline run without remote publication:

    python tools/neoh-agent/autobuild.py --category 02 --no-push

To opt out of the automatic local commit too, pass both --no-commit and
--no-push.

If tests or required ledger updates fail, the builder attempts targeted
repairs. After exhaustion, the changes remain local and are NOT pushed.
CI and provider-dependent tests, migrations, integrations and real-device
evidence remain separate release requirements. Local tests are not production
certification.

Important: this tool is not a filesystem or network sandbox. Run it only
in an isolated development environment with synthetic data and no production
credentials, cloud tokens, customer records or production network access.

## Optional: inspect without building

    python tools/neoh-agent/runner.py --mode audit --category 02 \
      --task "Audit mobile navigation with evidence from current code"

## Developer runner (edits without automatic tests/push)

    git switch -c feat/neoh-manual-work
    python tools/neoh-agent/runner.py --mode build --category 02 \
      --task "Fix one small issue"

This lower-level command can edit files but does not itself run tests or
push. Prefer autobuild.py for automatic engineering.

## Offline unit tests

    python -m unittest discover -s tools/neoh-agent -p 'test_*.py'

## Permissions and limitations

* The main agent has two modes: audit (read-only) and build (edit source).
* Six specialists are read-only: architecture, frontend, backend, AI,
  security, and QA. Project CLAUDE.md and its binding category rules load.
* Bash, web access, external MCP servers, and deployment tools are disabled
  for the model. The separate controller runs only fixed local test commands
  and pushes a dedicated side branch after checks pass.
* File hooks protect some sensitive directories and environment files. These
  are NOT a full filesystem sandbox: Glob/Grep and dependencies may still
  expose local files. Use an isolated checkout without secrets or PII.
* Each run has a bounded turn count and estimated USD budget; the API cost
  limit is not a guaranteed hard spending ceiling.
* The existing external oncompute MCP server is deliberately not enabled.
* All real sends, calls, billing operations, provider logins, production
  migrations, and deployments require explicit human authorization.
* The runner is source tooling only. No user-facing Neoh feature is marked
  tested or production ready by installing it.

Future: run this inside ephemeral locked-down development containers, add
independent CI gates and code review, and optionally open a draft PR after
successful checks. Never automatically merge or release to production.
