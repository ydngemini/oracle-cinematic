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

The autonomous controller uses **build mode by default**. On every run it
verifies the GitHub origin, fetches the newest `main`, and merges/fast-forwards
that commit into the clean development checkout. It compares the entire
`docs/claude-prompts/` directory with the authoritative GitHub version and
refuses to continue if instruction files diverge or a merge conflicts.

It then reads the **current** main-branch README, CONTRACT, all 20 category
prompts and LEDGER. Without a supplied task, it chooses an actionable next
slice based on the category build order, dependency prerequisites and ledger
next-task entries. It skips explicit owner-gated work, categories already
blocked, and categories with existing `auto/neoh-*` branches on GitHub.

For each selected task it creates a **fresh** GitHub side branch, edits one
category, runs fixed local test commands, retries failures (up to two repair
rounds by default), commits and pushes successful work to GitHub.

The main branch is NEVER pushed, merged or deployed by this controller.
The resulting remote branch is named like:

    auto/neoh-02-20261019T120000Z-abcdef

GitHub authentication must already be configured for this checkout's
origin, such as via SSH or GitHub CLI. The runner will only push when both
fetch and push remotes match ydngemini/oracle-cinematic on GitHub.

Start this from the repository root after installing the Python SDK and
Neoh's test dependencies:

    # Completely self-directed: picks one open category from current main
    python tools/neoh-agent/autobuild.py --budget-usd 12 --max-repairs 2

    # Or explicitly assign the category and work
    python tools/neoh-agent/autobuild.py \
      --category 02 \
      --task "Fix the next documented mobile UI defect with regression tests" \
      --budget-usd 12 \
      --max-repairs 2

Launch from a clean main or integration branch that already includes this
runner. The controller merges in the newest origin/main and creates a new
`auto/neoh-*` branch before editing. Before PR #34 is merged, the setup's
development branch includes the runner; its history will also be present
in any child side branch. After merging the reviewed setup PR, launch from
updated main for clean, isolated build branches.

**One task per invocation:** this honors Neoh's one-category-per-session
contract and prevents starting new work while previous branches await review.
To start another self-directed task, run autobuild again after reviewing or
removing the completed side branch. This is not a daemon or background job.

Outputs will include the new side-branch GitHub URL after a successful push.
No PR is opened automatically. Review the side branch, compare it with
main, and open a PR separately. Pushing it triggers non-deploy CI checks
for side branches; staging and production deployment workflows remain
main-gated. The developer agent guardrail tests are also in GitHub CI.

For an offline run without remote publication:

    python tools/neoh-agent/autobuild.py --category 02 --no-push

To opt out of the automatic local commit too, pass both --no-commit and
--no-push.

If tests or required ledger updates fail, the builder attempts targeted
repairs. After exhaustion, the changes remain local and are NOT pushed.
If main changes during the preflight fetch or the prompt format becomes
unrecognizable, the builder stops rather than using stale instructions.
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
