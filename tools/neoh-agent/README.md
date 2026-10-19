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

## Read-only first

    python tools/neoh-agent/runner.py --mode audit --category 02 \
      --task "Audit mobile navigation with evidence from current code"

## Implement one safe slice

Start on a clean feature branch. Build mode refuses main or dirty worktrees.

    git switch -c feat/neoh-mobile-navigation
    python tools/neoh-agent/runner.py --mode build --category 02 \
      --task "Fix one documented mobile navigation issue, add regression tests" \
      --budget-usd 3 --max-turns 14

Check changes and run tests manually:

    git diff --check
    cd oracle-app
    npm ci && npm test && npm run lint && npm run build

For backend work, use the backend pytest instructions in the root README.
Run the runner's unit checks:

    python -m unittest discover -s tools/neoh-agent -p 'test_*.py'

After actual test execution, update the correct category row in
docs/claude-prompts/LEDGER.md with the commands and evidence.
Review the diff and submit a PR through normal Git processes. Pushing to
main can deploy Neoh staging, so do not merge without owner approval.

## Permissions and limitations

* The main agent has two modes: audit (read-only) and build (edit source).
* Six specialists are read-only: architecture, frontend, backend, AI,
  security, and QA. Project CLAUDE.md and its binding category rules load.
* Bash, web access, external MCP servers, and deployment tools are disabled.
  The agent cannot run tests itself or push changes.
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

Future: use containerized ephemeral development worktrees, a supervised
test-command allowlist, GitHub PR automation, and per-agent execution budgets
before permitting unattended engineering work.
