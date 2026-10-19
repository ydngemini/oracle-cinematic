#!/usr/bin/env python3
"""Bounded, local Neoh software-development agent. Not a production service."""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "docs" / "claude-prompts"
READ = ["Read", "Glob", "Grep"]
BLOCKED = ["Bash", "WebFetch", "WebSearch", "NotebookEdit", "mcp__*"]


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=True, capture_output=True, text=True, timeout=20,
    )
    return result.stdout.strip()


def prompt_path(category: str) -> Path:
    if category not in {f"{number:02d}" for number in range(1, 21)}:
        raise ValueError("Choose a Neoh category from 01 to 20.")
    matches = sorted(PROMPTS.glob(f"{category}-*.md"))
    if len(matches) != 1:
        raise ValueError(f"Category {category} prompt missing or ambiguous.")
    return matches[0]


def preflight(mode: str, category: str) -> str:
    for path in [
        ROOT / "CLAUDE.md", PROMPTS / "README.md",
        PROMPTS / "CONTRACT.md", PROMPTS / "LEDGER.md",
        prompt_path(category),
    ]:
        if not path.is_file():
            raise ValueError(f"Required instruction file missing: {path}")
    sha = git("rev-parse", "HEAD")
    if mode == "build":
        branch = git("branch", "--show-current")
        if not branch or branch in {"main", "master"}:
            raise ValueError("Build requires a named feature branch, not main.")
        if git("status", "--porcelain", "--untracked-files=all"):
            raise ValueError("Build requires a clean working tree.")
    return sha


def source_path_allowed(raw: str) -> bool:
    """Defense-in-depth for Read/Write/Edit, not a filesystem sandbox."""
    if not isinstance(raw, str) or not raw:
        return False
    path = (ROOT / raw).resolve()
    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        return False
    parts = relative.parts
    forbidden = {".git", ".venv", "venv", "node_modules", "training_data"}
    if not parts or any(p in forbidden or p.startswith(".env") for p in parts):
        return False
    if parts[0] in {".github", "infra"}:
        return False
    if path.name in {".mcp.json", "settings.local.json"}:
        return False
    return True


async def file_guard(data, _tool_use_id, _context):
    if data.get("tool_name") in {"Read", "Write", "Edit"}:
        target = data.get("tool_input", {}).get("file_path")
        if not source_path_allowed(target):
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Protected or out-of-repository path",
            }}
    return {}


def engineering_prompt(args, sha: str, path: Path) -> str:
    task = args.task.strip() or "Find the highest-impact small safe vertical slice."
    prompt = (
        f"Base SHA: {sha}; category {args.category}; read {path.relative_to(ROOT)}. "
        "FIRST read root CLAUDE.md, docs/claude-prompts/README.md, CONTRACT.md, "
        "the category prompt, related LEDGER.md rows and current source/tests. "
        "Follow the binding Neoh contract and owner decisions; one category "
        "per session. Use read-only specialists for bounded reviews. "
        "Never access real customer data or credentials, contact people, "
        "spend with providers, execute commands, deploy, or push Git commits. "
        "Bash is unavailable, so report all tests as NOT RUN. "
        "Report in Neoh CONTRACT C11 order with source evidence. "
    )
    if args.mode == "audit":
        prompt += "READ-ONLY inspection. Do not edit anything. "
    else:
        prompt += (
            "Implement ONE narrowly scoped change and add regression tests. "
            "Update that category's ledger rows with an honest "
            "implemented_untested state, since you cannot run tests. "
            "Never modify production deployment or CI configuration. "
        )
    return prompt + "\n\nTask: " + task


async def run(args) -> int:
    from claude_agent_sdk import (
        AgentDefinition, AssistantMessage, ClaudeAgentOptions, HookMatcher,
        ResultMessage, TextBlock, ToolUseBlock, query,
    )
    sha = preflight(args.mode, args.category)
    agents = {
        name: AgentDefinition(
            description=desc,
            prompt=(
                "You are a READ-ONLY Neoh expert. Follow CLAUDE.md and the "
                "engineering contract. Inspect and report; do not edit, "
                "execute shell commands, access customer data, or call APIs. "
                + focus
            ),
            tools=READ,
            maxTurns=5,
        )
        for name, desc, focus in [
            ("architect", "Architecture and system dependencies", "Focus on boundaries and safe migrations."),
            ("frontend", "React browser UI specialist", "Focus on oracle-app, responsive design and accessibility."),
            ("backend", "FastAPI and Postgres specialist", "Focus on RLS, API contracts and data safety."),
            ("ai", "Neoh AI and voice specialist", "Focus on model gateway, voice and budgets."),
            ("security", "Security and privacy specialist", "Focus on tenancy, PII and authorization."),
            ("qa", "Test planning specialist", "Propose tests without claiming to execute them."),
        ]
    }
    tools = READ + ["Agent"]
    if args.mode == "build":
        tools += ["Write", "Edit"]
    options = ClaudeAgentOptions(
        cwd=str(ROOT),
        tools=tools,
        allowed_tools=tools,
        disallowed_tools=BLOCKED,
        setting_sources=["project"],
        strict_mcp_config=True,
        mcp_servers={},
        permission_mode="acceptEdits" if args.mode == "build" else "default",
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "You are Neoh's local development orchestrator. Respect all repository engineering instructions. Do not act outside the authorized development task.",
        },
        agents=agents,
        hooks={"PreToolUse": [HookMatcher(
            matcher="Read|Write|Edit", hooks=[file_guard],
        )]},
        max_turns=args.max_turns,
        max_budget_usd=args.budget_usd,
        model=args.model,
    )
    print(f"Neoh {args.mode}: category {args.category}, base {sha[:12]}")
    completed = False
    async for message in query(
        prompt=engineering_prompt(args, sha, prompt_path(args.category)),
        options=options,
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)
                elif isinstance(block, ToolUseBlock):
                    print(f"[tool] {block.name}")
        elif isinstance(message, ResultMessage):
            completed = True
            print(f"[session] {message.subtype}")
            if message.total_cost_usd is not None:
                print(f"[estimated cost] USD {message.total_cost_usd:.4f}")
            if message.is_error:
                return 1
    return 0 if completed else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Neoh Claude Agent SDK development runner")
    parser.add_argument("--mode", choices=["audit", "build"], default="audit")
    parser.add_argument("--category", default="02")
    parser.add_argument("--task", default="")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--max-turns", type=int, default=14)
    parser.add_argument("--budget-usd", type=float, default=3.0)
    args = parser.parse_args()
    if args.max_turns < 1 or args.budget_usd <= 0:
        parser.error("max-turns and budget-usd must be positive")
    try:
        raise SystemExit(asyncio.run(run(args)))
    except (ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"Preflight failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
