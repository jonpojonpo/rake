"""
Unit tests for the rake Python SDK.

Run:
  RAKE_LLM=noop pytest python/tests/test_sdk.py -v
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from rake_sdk.models import (
    RakeResult,
    FindingSeverity,
    extract_findings,
    parse_trajectory,
    ThinkStep,
    LlmTurnStep,
    ToolCallStep,
    ToolResultStep,
    DoneStep,
)
from rake_sdk.exceptions import RakeError, RakeBinaryNotFoundError


# ── Trajectory parsing ────────────────────────────────────────────────────────

def test_parse_trajectory_think():
    raw = [{"type": "think", "text": "I will analyse the files."}]
    steps = parse_trajectory(raw)
    assert len(steps) == 1
    assert isinstance(steps[0], ThinkStep)
    assert steps[0].text == "I will analyse the files."


def test_parse_trajectory_llm_turn():
    raw = [{"type": "llm_turn", "ms": 1234, "input_tokens": 100, "output_tokens": 50}]
    steps = parse_trajectory(raw)
    assert isinstance(steps[0], LlmTurnStep)
    assert steps[0].ms == 1234
    assert steps[0].input_tokens == 100
    assert steps[0].output_tokens == 50


def test_parse_trajectory_tool_call():
    raw = [{"type": "call", "tool": "grep_files", "input": {"pattern": "TODO"}}]
    steps = parse_trajectory(raw)
    assert isinstance(steps[0], ToolCallStep)
    assert steps[0].tool == "grep_files"
    assert steps[0].input == {"pattern": "TODO"}


def test_parse_trajectory_done():
    raw = [{"type": "done", "summary": "Analysis complete."}]
    steps = parse_trajectory(raw)
    assert isinstance(steps[0], DoneStep)
    assert steps[0].summary == "Analysis complete."


def test_parse_trajectory_mixed():
    raw = [
        {"type": "think", "text": "Let me look at the files."},
        {"type": "llm_turn", "ms": 500, "input_tokens": 200, "output_tokens": 80},
        {"type": "call", "tool": "read_file", "input": {"path": "app.py"}},
        {"type": "result", "tool": "read_file", "tool_ms": 5, "output": "print('hi')"},
        {"type": "done", "summary": "No issues found."},
    ]
    steps = parse_trajectory(raw)
    assert len(steps) == 5
    assert isinstance(steps[0], ThinkStep)
    assert isinstance(steps[4], DoneStep)


# ── Finding extraction ────────────────────────────────────────────────────────

def test_extract_findings_bullet_format():
    summary = """
## Security Audit

- [CRITICAL] SQL Injection: User input passed directly to query at line 42
- [HIGH] Hardcoded Secret: API key found in config.py
- [MEDIUM] Missing Validation: No input sanitisation on username field
- [LOW] TODO: Refactor auth module
- [INFO] Consider using parameterised queries
"""
    findings = extract_findings(summary)
    severities = {f.severity for f in findings}
    assert FindingSeverity.CRITICAL in severities
    assert FindingSeverity.HIGH in severities
    assert len(findings) >= 3


def test_extract_findings_empty():
    findings = extract_findings("")
    assert findings == []


def test_extract_findings_no_structured_items():
    summary = "The code looks fine. No issues were found."
    findings = extract_findings(summary)
    assert isinstance(findings, list)


def test_extract_findings_deduplication():
    summary = "\n".join([
        "- [HIGH] Hardcoded Secret: found in app.py",
        "- [HIGH] Hardcoded Secret: found in app.py",  # duplicate
    ])
    findings = extract_findings(summary)
    assert len(findings) == 1


# ── RakeResult ────────────────────────────────────────────────────────────────

def test_rake_result_from_trajectory():
    raw = [
        {"type": "think", "text": "analysing"},
        {"type": "llm_turn", "ms": 1000, "input_tokens": 300, "output_tokens": 100},
        {"type": "call", "tool": "read_file", "input": {}},
        {"type": "result", "tool": "read_file", "tool_ms": 10, "output": "..."},
        {"type": "done", "summary": "- [HIGH] Hardcoded password found in config.py"},
    ]
    result = RakeResult.from_trajectory(raw, files=["config.py"])
    assert result.summary == "- [HIGH] Hardcoded password found in config.py"
    assert result.total_input_tokens == 300
    assert result.total_output_tokens == 100
    assert result.total_llm_ms == 1000
    assert result.tool_calls == 1
    assert result.files_analyzed == ["config.py"]


def test_rake_result_has_critical_issues():
    raw = [{"type": "done", "summary": "- [CRITICAL] RCE vulnerability found"}]
    result = RakeResult.from_trajectory(raw, files=["app.py"])
    assert result.has_critical_issues


def test_rake_result_no_critical():
    raw = [{"type": "done", "summary": "- [LOW] TODO comment found"}]
    result = RakeResult.from_trajectory(raw, files=["app.py"])
    assert not result.has_critical_issues


def test_rake_result_to_dict():
    raw = [{"type": "done", "summary": "All clear."}]
    result = RakeResult.from_trajectory(raw, files=["test.py"])
    d = result.to_dict()
    assert "summary" in d
    assert "findings" in d
    assert "stats" in d
    assert d["stats"]["files_analyzed"] == ["test.py"]


# ── RakeClient (noop backend integration test) ────────────────────────────────

@pytest.mark.asyncio
async def test_client_noop_analyze_bytes():
    """Integration test using noop LLM backend — doesn't require API key."""
    import os
    from rake_sdk import RakeClient, RakeConfig

    # Skip if rake binary not available
    import shutil
    if not shutil.which("rake") and not os.environ.get("RAKE_BINARY"):
        pytest.skip("rake binary not found")

    config = RakeConfig(llm="noop", timeout=30)
    async with RakeClient(config) as client:
        result = await client.analyze_bytes(
            named_files={"hello.py": b"print('hello world')"},
            goal="Find issues.",
        )

    assert isinstance(result, RakeResult)
    assert isinstance(result.findings, list)
    assert isinstance(result.trajectory, list)
