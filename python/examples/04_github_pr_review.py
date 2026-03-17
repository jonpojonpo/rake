"""
GitHub PR auto-review with rake.

Fetches changed files from a GitHub Pull Request, runs a code review,
and posts a summary comment back to the PR.

This pattern enables:
  - Automatic code review on every PR
  - Security scanning as a merge gate
  - AI-generated review comments with finding details

Prerequisites:
  pip install PyGithub
  export GITHUB_TOKEN="ghp_..."
  export ANTHROPIC_API_KEY="sk-ant-..."

Usage:
  python examples/04_github_pr_review.py --repo owner/repo --pr 42
  python examples/04_github_pr_review.py --repo owner/repo --pr 42 --post-comment
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from rake_sdk import RakeClient, RakeConfig, FindingSeverity


def parse_args():
    p = argparse.ArgumentParser(description="Review a GitHub PR with rake")
    p.add_argument("--repo", required=True, help="owner/repo")
    p.add_argument("--pr", type=int, required=True, help="PR number")
    p.add_argument("--post-comment", action="store_true", help="Post review as PR comment")
    p.add_argument("--llm", default="claude", help="LLM backend")
    p.add_argument("--fail-on-critical", action="store_true", help="Exit 1 if critical issues found")
    return p.parse_args()


async def review_pr(repo_name: str, pr_number: int, llm: str, post_comment: bool, fail_on_critical: bool):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    try:
        from github import Github, GithubException
    except ImportError:
        print("Install PyGithub: pip install PyGithub", file=sys.stderr)
        sys.exit(1)

    g = Github(token)
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    print(f"PR #{pr_number}: {pr.title}")
    print(f"Files changed: {pr.changed_files}")

    # Collect changed files (skip binary and very large files)
    named_files: dict[str, bytes] = {}
    for pf in pr.get_files():
        if pf.status == "removed":
            continue
        ext = Path(pf.filename).suffix.lower()
        if ext not in (".py", ".js", ".ts", ".go", ".rs", ".java", ".cs", ".rb", ".php",
                        ".yaml", ".yml", ".json", ".tf", ".sh", ".sql"):
            continue
        if (pf.additions + pf.deletions) > 2000:
            print(f"  Skipping large file: {pf.filename}")
            continue

        # Fetch raw content
        try:
            content = repo.get_contents(pf.filename, ref=pr.head.sha)
            named_files[pf.filename] = content.decoded_content
            print(f"  Added: {pf.filename} (+{pf.additions}/-{pf.deletions})")
        except GithubException as e:
            print(f"  Skipping {pf.filename}: {e}")

    if not named_files:
        print("No reviewable files found.")
        return

    config = RakeConfig(
        llm=llm,
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        timeout=240,
    )

    print(f"\nRunning code review on {len(named_files)} files with {llm}…")
    async with RakeClient(config) as client:
        result = await client.analyze_bytes(
            named_files=named_files,
            goal=(
                "Review these code changes for a pull request. Focus on: "
                "bugs and correctness issues, security vulnerabilities, "
                "missing error handling, performance problems, and code clarity. "
                "For each issue output: '- [SEVERITY] Title: description (file:line)' "
                "where SEVERITY is CRITICAL, HIGH, MEDIUM, LOW, or INFO."
            ),
        )

    # Format comment
    severity_icons = {
        FindingSeverity.CRITICAL: "🔴",
        FindingSeverity.HIGH: "🟠",
        FindingSeverity.MEDIUM: "🟡",
        FindingSeverity.LOW: "🔵",
        FindingSeverity.INFO: "⚪",
    }

    comment_lines = [
        "## 🔍 rake AI Code Review",
        "",
        result.summary,
        "",
    ]

    if result.findings:
        comment_lines.append("### Findings")
        comment_lines.append("")
        for f in sorted(result.findings, key=lambda x: [FindingSeverity.CRITICAL, FindingSeverity.HIGH, FindingSeverity.MEDIUM, FindingSeverity.LOW, FindingSeverity.INFO].index(x.severity)):
            icon = severity_icons.get(f.severity, "•")
            loc = f" `{f.file}:{f.line}`" if f.file else ""
            comment_lines.append(f"- {icon} **[{f.severity.value.upper()}]**{loc} **{f.title}**: {f.description}")
        comment_lines.append("")

    comment_lines += [
        "---",
        f"*Analysed {len(named_files)} files · {result.tool_calls} tool calls · "
        f"{result.total_input_tokens + result.total_output_tokens} tokens · "
        f"Powered by [rake](https://github.com/jonpojonpo/rake)*",
    ]

    comment = "\n".join(comment_lines)
    print("\n" + comment)

    if post_comment:
        pr.create_issue_comment(comment)
        print(f"\nPosted review comment to PR #{pr_number}")

    if fail_on_critical and result.has_critical_issues:
        print(f"\nFAILED: {len(result.critical_findings)} critical finding(s)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(review_pr(
        repo_name=args.repo,
        pr_number=args.pr,
        llm=args.llm,
        post_comment=args.post_comment,
        fail_on_critical=args.fail_on_critical,
    ))
