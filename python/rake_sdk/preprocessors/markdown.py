"""
Markdown / plain-text preprocessor.

For each .md/.txt file generates:
  <stem>._index.md  — compact TOC with heading level, title, and line range
                       formatted as "# Title  start,end" so the LLM can call
                       read_section(path, start, end) without loading the whole file.
  <stem>.table_NNN.csv — one CSV per pipe-delimited markdown table found.
"""
from __future__ import annotations

import csv as _csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Section:
    level: int
    title: str
    start_line: int
    end_line: int


@dataclass
class ExtractedTable:
    name: str
    csv_content: str
    start_line: int
    end_line: int
    caption: Optional[str]


class MarkdownPreprocessor:
    def process(self, filename: str, content: bytes) -> dict[str, bytes]:
        text = content.decode("utf-8", errors="replace")
        lines = text.splitlines()
        stem = Path(filename).stem

        sections = self._extract_sections(lines)
        tables   = self._extract_tables(lines, stem)

        result: dict[str, bytes] = {filename: content}
        result[f"{stem}._index.md"] = self._build_index(filename, len(lines), sections, tables).encode()
        for tbl in tables:
            result[tbl.name] = tbl.csv_content.encode()
        return result

    # ── sections ─────────────────────────────────────────────────────────────

    def _extract_sections(self, lines: list[str]) -> list[Section]:
        total = len(lines)
        heading_re = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?$")
        raw: list[tuple[int, int, str]] = []
        for i, line in enumerate(lines, 1):
            m = heading_re.match(line.rstrip())
            if m:
                raw.append((i, len(m.group(1)), m.group(2).strip()))
        sections = []
        for idx, (lineno, level, title) in enumerate(raw):
            end = raw[idx + 1][0] - 1 if idx + 1 < len(raw) else total
            sections.append(Section(level=level, title=title, start_line=lineno, end_line=end))
        return sections

    # ── tables ────────────────────────────────────────────────────────────────

    def _extract_tables(self, lines: list[str], stem: str) -> list[ExtractedTable]:
        tables, table_no, i, total = [], 0, 0, len(lines)
        while i < total:
            if not self._is_pipe_row(lines[i]):
                i += 1
                continue
            start = i
            table_lines = []
            while i < total and self._is_pipe_row(lines[i]):
                table_lines.append(lines[i])
                i += 1
            if len(table_lines) < 3 or not self._is_sep(table_lines[1]):
                continue
            table_no += 1
            caption = lines[start - 1].strip().strip("*_") if start > 0 else None
            if caption and len(caption) > 120:
                caption = None
            tables.append(ExtractedTable(
                name=f"{stem}.table_{table_no:03d}.csv",
                csv_content=self._to_csv(table_lines),
                start_line=start + 1,
                end_line=i,
                caption=caption,
            ))
        return tables

    @staticmethod
    def _is_pipe_row(line: str) -> bool:
        s = line.strip()
        return bool(s) and s.startswith("|") and "|" in s[1:]

    @staticmethod
    def _is_sep(line: str) -> bool:
        return bool(re.match(r"^\|[\s\-:|\s]+\|$", line.strip()))

    @staticmethod
    def _to_csv(table_lines: list[str]) -> str:
        buf = io.StringIO()
        w = _csv.writer(buf)
        for row in table_lines:
            if re.match(r"^\|[\s\-:|\s]+\|$", row.strip()):
                continue
            w.writerow([c.strip() for c in row.strip().strip("|").split("|")])
        return buf.getvalue()

    # ── index ─────────────────────────────────────────────────────────────────

    def _build_index(self, filename: str, total: int, sections: list[Section], tables: list[ExtractedTable]) -> str:
        out = [
            f"# Document Index: {filename}",
            f"Total lines: {total:,}",
            "",
            "## Navigation",
            "Use `read_section(path, start_line, end_line)` to read a section.",
            "Format shown below: `# Heading  start,end`",
            "Use `csv_stats` on extracted table CSV files.",
            "",
        ]
        if sections:
            out.append("## Table of Contents")
            out.append("")
            for s in sections:
                indent = "  " * (s.level - 1)
                out.append(f"{indent}{'#' * s.level} {s.title}  {s.start_line},{s.end_line}")
            out.append("")
        if tables:
            out.append("## Extracted Tables")
            out.append("")
            for t in tables:
                note = f" — {t.caption}" if t.caption else ""
                out.append(f"- `{t.name}` (lines {t.start_line}–{t.end_line}{note})")
            out.append("")
        return "\n".join(out) + "\n"
