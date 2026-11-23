# Noah: 20 additional control layers

These are controller checks, bounded context, and evaluation improvements—not
20 paid model agents. They do not raise the model budget, grant new tools,
merge changes, deploy, or make owner decisions automatically.

## Implemented layers

| # | Layer | Enforcement / evidence |
|---|---|---|
| 1 | Preflight task discovery | Fetch and select actionable work before creating a worktree, installing dependencies, or reserving model budget. |
| 2 | No-work standby | No eligible task is an idle condition, not a failed model cycle. |
| 3 | Pinned task identity | Hash category, task, and main SHA; reject a changed main before the model starts. |
| 4 | Risk-aware review | Sensitive categories/keywords receive a security-focused review checklist and must change regression tests when changing source. |
| 5 | Source-evidence plan | Require `.neoh-agent-state/plan.json`; evidence files and line numbers must exist within the checkout. |
| 6 | Acceptance and recovery plan | Require bounded acceptance criteria, rollback instructions, and a self-review record. These declarations are not proof of correctness. |
| 7 | Declared change scope | Every changed file must appear in the plan; limit a slice to 12 files. |
| 8 | Change-size review gate | Maximum 1500 added/deleted lines and 1 MiB per changed file; binary changes require manual review. |
| 9 | Syntax fast-fail | Parse changed Python files without executing them before expensive checks. |
| 10 | Focused regression first | Run changed/adjacent tests first; after success still require the complete existing approval suite. |
| 11 | Per-check evidence | Persist command, working directory, exit status, duration, and completion status as private JSON. |
| 12 | Infrastructure triage | Disk exhaustion, tracked-file readability failures, and command timeouts stop model repair attempts. Timeout classification is operational, not proof that the model is blameless. |
| 13 | Timeout process cleanup | Timed-out commands run in their own process group; kill and reap that group rather than leaving ordinary child test processes behind. |
| 14 | Stable failure fingerprints | Normalize runtime durations and cycle identifiers before comparing failures. |
| 15 | Stagnation breaker | Stop when both the failure fingerprint and changed-file content are unchanged across repair rounds. |
| 16 | Bounded failure memory | Carry only the last three failed-cycle metadata records for the selected category. No raw inbox or transcript import. |
| 17 | Durable cycle receipts | Copy bounded check and handoff artifacts into supervisor state before successful worktree cleanup. |
| 18 | Repair-context redaction | Mask common credential formats and authorization/password assignments in repair feedback. This is heuristic, not a comprehensive secret scanner. |
| 19 | Fresh owner context | Include at most five category-matching email answers from the last 30 days. Reject future/malformed timestamps; answers remain untrusted context, not authority to override the ledger. |
| 20 | Explicit idle telemetry | Persist distinct budget, disk, no-work, and preflight-unavailable reasons; email status explains these rather than implying it is waiting for a reply. |

## Plan example

```json
{
  "category": "02",
  "goal": "Reject malformed route inputs without changing valid routes",
  "evidence": [{"path": "oracle-app/src/routes.js", "line": 1}],
  "acceptance": ["Existing valid routes remain unchanged", "Malformed input is rejected"],
  "files": ["oracle-app/src/routes.js", "oracle-app/src/routes.test.js", "docs/claude-prompts/LEDGER.md"],
  "rollback": "Revert the isolated review-branch change",
  "self_review": "Checked empty input, invalid types, and backwards compatibility"
}
```

The model creates the plan before source changes and updates its self-review
afterward; the controller validates it before running approval tests.
This is a publication gate, not a guarantee the model never writes an unplanned
file. Existing filesystem and file-tool boundaries remain in force.

Receipts are local records, not cryptographic attestations. Passing a focused
test does not bypass full tests, and passing local tests does not establish
staging or production correctness. Historical failed runs are not rewritten.

The 0.5 GiB free-space guard, $12 per-cycle reservation, $48 UTC-day reservation,
and existing model remain unchanged. The free-space guard runs before a cycle;
it cannot guarantee enough space for an entire build. No old worktrees are
deleted by these improvements.

## Validation

`python -m unittest discover -s tools/neoh-agent -p 'test_*.py' -v`

`test_intelligence.py` exercises plans, evidence, scope, risk gates, syntax,
targeted/full test ordering, failure fingerprints, process cleanup, receipts,
redaction, and fresh owner context. Supervisor tests cover free discovery,
no-work idling, and task identity. Autobuild tests cover stale main,
infrastructure stop, and unchanged-repair termination.

These are offline regression tests, including a real local subprocess timeout
test. They do not establish improved end-to-end task success rates; that requires
observing future budget-authorized cycles. No paid model run is needed to test
the control logic.

## Research basis (reviewed 2026-10-20)

This design is our application of the following primary sources, not a vendor
claim that this exact implementation is optimal.

- [Anthropic: effective agents](https://www.anthropic.com/engineering/building-effective-agents):
  use small composable workflows and explicit evaluation criteria rather than
  adding autonomous model calls indiscriminately.
- [Anthropic: long-running harnesses](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents):
  work incrementally and leave inspectable progress artifacts between sessions.
- [Anthropic: context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents):
  keep relevant context compact and retrieve source evidence when needed.
- [Anthropic: agent evaluations](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents):
  evaluate observable behavior with reproducible tests rather than relying on
  the model's completion claim.
- [Anthropic: infrastructure noise](https://www.anthropic.com/engineering/infrastructure-noise):
  distinguish environment limitations from model behavior when diagnosing runs.
- [Claude Agent SDK hooks](https://code.claude.com/docs/en/agent-sdk/hooks):
  retain external controls around tool access; prompt instructions alone are
  not a security boundary.
- [Python 3.12 subprocess documentation](https://docs.python.org/3.12/library/subprocess.html):
  bound child execution and explicitly clean up processes after timeouts.
- [Git worktree documentation](https://git-scm.com/docs/git-worktree.html):
  keep task checkouts independent of the supervisor and active development tree.

## Slack communication (not connected)

A separate owner-facing bot can use
[Slack Socket Mode](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/)
without opening a public HTTP listener. It needs a Slack app installation,
an app-level token, a bot token, the selected workspace/channel, and an owner
user-ID allowlist. Start with mention-only events and posting permissions;
do not import all channel history or give the coding model Slack credentials.
Slack replies should use the same task-context boundary as email.
