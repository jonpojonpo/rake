"""Typed models for rake trajectory output and analysis results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import base64 as _b64


# ── Trajectory step models ──────────────────────────────────────────────────

@dataclass
class ThinkStep:
    text: str


@dataclass
class LlmTurnStep:
    ms: int
    input_tokens: int
    output_tokens: int


@dataclass
class ToolCallStep:
    tool: str
    input: dict[str, Any]


@dataclass
class ToolResultStep:
    tool: str
    tool_ms: int
    output: str


@dataclass
class DoneStep:
    summary: str


TrajectoryStep = ThinkStep | LlmTurnStep | ToolCallStep | ToolResultStep | DoneStep


def parse_trajectory(raw: list[dict]) -> list[TrajectoryStep]:
    """Parse raw JSON trajectory into typed step objects."""
    steps: list[TrajectoryStep] = []
    for item in raw:
        t = item.get("type")
        if t == "think":
            steps.append(ThinkStep(text=item["text"]))
        elif t == "llm_turn":
            steps.append(LlmTurnStep(
                ms=item["ms"],
                input_tokens=item["input_tokens"],
                output_tokens=item["output_tokens"],
            ))
        elif t == "call":
            steps.append(ToolCallStep(tool=item["tool"], input=item.get("input", {})))
        elif t == "result":
            steps.append(ToolResultStep(
                tool=item["tool"],
                tool_ms=item.get("tool_ms", 0),
                output=item.get("output", ""),
            ))
        elif t == "done":
            steps.append(DoneStep(summary=item["summary"]))
    return steps


# ── Finding extraction ───────────────────────────────────────────────────────

class FindingSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass
class Finding:
    title: str
    description: str
    severity: FindingSeverity
    file: Optional[str] = None
    line: Optional[int] = None
    category: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "severity": self.severity.value,
            "file": self.file,
            "line": self.line,
            "category": self.category,
        }


_SEV_KEYWORDS: dict[FindingSeverity, list[str]] = {
    FindingSeverity.CRITICAL: ["critical", "rce", "remote code execution", "sql injection", "command injection"],
    FindingSeverity.HIGH: ["high", "vulnerability", "hardcoded secret", "hardcoded password", "hardcoded credential",
                           "injection", "xss", "path traversal", "authentication bypass"],
    FindingSeverity.MEDIUM: ["medium", "missing validation", "unhandled exception", "insecure", "deprecated"],
    FindingSeverity.LOW: ["low", "todo", "fixme", "style", "naming"],
    FindingSeverity.INFO: ["note", "info", "suggestion", "consider"],
}


def _infer_severity(text: str) -> FindingSeverity:
    lower = text.lower()
    for sev, keywords in _SEV_KEYWORDS.items():
        if any(k in lower for k in keywords):
            return sev
    return FindingSeverity.INFO


def extract_findings(summary: str) -> list[Finding]:
    """
    Parse structured findings from the LLM markdown summary.
    Looks for bullet-point lists with severity markers.
    """
    findings: list[Finding] = []

    # Match patterns like: "- **[HIGH]** Title: description" or "### Critical: ..."
    bullet_pattern = re.compile(
        r"[-*]\s+(?:\*{1,2})?\[?(CRITICAL|HIGH|MEDIUM|LOW|INFO)\]?(?:\*{1,2})?\s*[:\-]?\s*(.+)",
        re.IGNORECASE
    )
    heading_pattern = re.compile(
        r"#{1,4}\s+(\d+\.?\s+)?(.+)",
        re.IGNORECASE
    )

    file_pattern = re.compile(r"`([^`]+\.(py|js|ts|rs|go|java|cs|rb|php|yaml|json|tf))`")
    line_pattern = re.compile(r"[Ll]ine\s+(\d+)")

    for line in summary.splitlines():
        m = bullet_pattern.match(line.strip())
        if m:
            raw_sev, rest = m.group(1), m.group(2).strip()
            try:
                sev = FindingSeverity(raw_sev.lower())
            except ValueError:
                sev = _infer_severity(raw_sev)

            # Extract file reference if present
            file_match = file_pattern.search(rest)
            file_ref = file_match.group(1) if file_match else None
            line_match = line_pattern.search(rest)
            line_no = int(line_match.group(1)) if line_match else None

            # Split "Title: description"
            parts = rest.split(":", 1)
            title = parts[0].strip().strip("*")
            desc = parts[1].strip() if len(parts) > 1 else title

            findings.append(Finding(
                title=title,
                description=desc,
                severity=sev,
                file=file_ref,
                line=line_no,
            ))
        else:
            # Fallback: infer severity from content
            if any(kw in line.lower() for kws in _SEV_KEYWORDS.values() for kw in kws):
                sev = _infer_severity(line)
                if sev in (FindingSeverity.CRITICAL, FindingSeverity.HIGH):
                    findings.append(Finding(
                        title=line.strip("- *#").split(":")[0][:80],
                        description=line.strip(),
                        severity=sev,
                    ))

    # Deduplicate by title
    seen: set[str] = set()
    unique: list[Finding] = []
    for f in findings:
        if f.title not in seen:
            seen.add(f.title)
            unique.append(f)
    return unique


# ── Main result object ───────────────────────────────────────────────────────

@dataclass
class RakeResult:
    """Complete result of a rake analysis run."""
    summary: str
    trajectory: list[TrajectoryStep]
    findings: list[Finding] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_llm_ms: int = 0
    tool_calls: int = 0
    files_analyzed: list[str] = field(default_factory=list)
    # Files written by the LLM during analysis (reports, CSVs, edited docs, etc.)
    # Keys are filenames, values are raw bytes ready for upload/download.
    output_files: dict[str, bytes] = field(default_factory=dict)

    @classmethod
    def from_trajectory(
        cls,
        raw: list[dict],
        files: list[str],
        output_files: Optional[dict[str, bytes]] = None,
    ) -> "RakeResult":
        steps = parse_trajectory(raw)

        summary = ""
        total_in = total_out = total_ms = n_tools = 0

        for step in steps:
            if isinstance(step, DoneStep):
                summary = step.summary
            elif isinstance(step, LlmTurnStep):
                total_in += step.input_tokens
                total_out += step.output_tokens
                total_ms += step.ms
            elif isinstance(step, ToolCallStep):
                n_tools += 1

        findings = extract_findings(summary) if summary else []

        return cls(
            summary=summary,
            trajectory=steps,
            findings=findings,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_llm_ms=total_ms,
            tool_calls=n_tools,
            files_analyzed=files,
            output_files=output_files or {},
        )

    @property
    def critical_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == FindingSeverity.CRITICAL]

    @property
    def high_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == FindingSeverity.HIGH]

    @property
    def has_critical_issues(self) -> bool:
        return len(self.critical_findings) > 0

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "output_files": {
                name: _b64.b64encode(data).decode("ascii")
                for name, data in self.output_files.items()
            },
            "stats": {
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_llm_ms": self.total_llm_ms,
                "tool_calls": self.tool_calls,
                "files_analyzed": self.files_analyzed,
            },
        }
