"""
Markdown document preprocessor.

For each .md / .txt file it generates a companion `<name>._index.md`
that the LLM reads FIRST before touching the real document.

The index contains:
  - Total line count
  - Table of contents: heading text, level, start_line, end_line
    formatted as   # Chairman's Statement  100,235
    so the LLM can call read_section(path, 100, 235) directly.
  - List of extracted CSV tables: filename, line range, caption

Tables inside the markdown are extracted to separate .csv files and
listed in the index so the LLM can call csv_stats() on each one
instead of parsing raw pipe-delimited text.

Usage:
    from rake_sdk.preprocessors.markdown import MarkdownPreprocessor
    files = MarkdownPreprocessor().process("report.md", content_bytes)
    # Returns {"report.md": ..., "report._index.md": ..., "report.table_001.csv": ...}
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Section:
    level: int        # 1–6 (ATX heading level)
    title: str
    start_line: int   # 1-indexed
    end_line: int     # inclusive


@dataclass
class ExtractedTable:
    name: str          # e.g. "report.table_001.csv"
    csv_content: str
    start_line: int
    end_line: int
    caption: Optional[str]


class MarkdownPreprocessor:
    """
    Preprocesses a markdown (or plain text) document into:
    1. The original file (unchanged)
    2. A compact _index.md with TOC and line ranges
    3. One CSV file per markdown table found
    """

    MAX_INLINE_LINES = 500  # files shorter than this don't need an index

    def process(self, filename: str, content: bytes) -> dict[str, bytes]:
        """
        Returns a dict of {filename: bytes} ready to mount into the sandbox.
        Always includes the original file plus generated companions.
        """
        text = content.decode("utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)

        stem = Path(filename).stem
        output: dict[str, bytes] = {filename: content}

        sections = self._extract_sections(lines)
        tables = self._extract_tables(lines, stem)

        # Always generate an index for any markdown file
        index_name = f"{stem}._index.md"
        index_content = self._build_index(filename, total, sections, tables)
        output[index_name] = index_content.encode("utf-8")

        # Add extracted CSV files
        for tbl in tables:
            output[tbl.name] = tbl.csv_content.encode("utf-8")

        return output

    # ── Section extraction ────────────────────────────────────────────────────

    def _extract_sections(self, lines: list[str]) -> list[Section]:
        """
        Find all ATX headings (# ## ###…) and compute their line ranges.
        """
        total = len(lines)
        heading_re = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?$")
        raw: list[tuple[int, int, str]] = []  # (line_no 1-indexed, level, title)

        for i, line in enumerate(lines, start=1):
            m = heading_re.match(line.rstrip())
            if m:
                raw.append((i, len(m.group(1)), m.group(2).strip()))

        sections: list[Section] = []
        for idx, (lineno, level, title) in enumerate(raw):
            # end_line = line before next heading (or EOF)
            if idx + 1 < len(raw):
                end = raw[idx + 1][0] - 1
            else:
                end = total
            sections.append(Section(level=level, title=title, start_line=lineno, end_line=end))

        return sections

    # ── Table extraction ──────────────────────────────────────────────────────

    def _extract_tables(self, lines: list[str], stem: str) -> list[ExtractedTable]:
        """
        Find pipe-delimited markdown tables, extract them as CSV, and remove them
        from context. Each table is saved as a numbered CSV file.
        """
        tables: list[ExtractedTable] = []
        table_no = 0
        i = 0
        total = len(lines)

        while i < total:
            line = lines[i]
            # A table block starts with a pipe row
            if not self._is_pipe_row(line):
                i += 1
                continue

            # Collect the full table
            start = i
            table_lines: list[str] = []
            while i < total and self._is_pipe_row(lines[i]):
                table_lines.append(lines[i])
                i += 1
            end = i - 1  # inclusive, 1-indexed = end+1

            # Need at least header + separator + 1 data row
            if len(table_lines) < 3:
                continue
            if not self._is_separator_row(table_lines[1]):
                continue

            table_no += 1
            name = f"{stem}.table_{table_no:03d}.csv"

            # Look for a caption immediately before the table (e.g. **Caption**)
            caption: Optional[str] = None
            if start > 0:
                prev = lines[start - 1].strip().strip("*_")
                if prev and len(prev) < 120:
                    caption = prev

            csv_text = self._table_to_csv(table_lines)
            tables.append(ExtractedTable(
                name=name,
                csv_content=csv_text,
                start_line=start + 1,  # 1-indexed
                end_line=end + 1,
                caption=caption,
            ))

        return tables

    @staticmethod
    def _is_pipe_row(line: str) -> bool:
        s = line.strip()
        return bool(s) and s.startswith("|") and "|" in s[1:]

    @staticmethod
    def _is_separator_row(line: str) -> bool:
        return bool(re.match(r"^\|[\s\-:|\s]+\|$", line.strip()))

    @staticmethod
    def _table_to_csv(table_lines: list[str]) -> str:
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        for lineno, row_str in enumerate(table_lines):
            # Skip separator row
            if re.match(r"^\|[\s\-:|\s]+\|$", row_str.strip()):
                continue
            cells = [c.strip() for c in row_str.strip().strip("|").split("|")]
            writer.writerow(cells)
        return buf.getvalue()

    # ── Index generation ──────────────────────────────────────────────────────

    def _build_index(
        self,
        filename: str,
        total_lines: int,
        sections: list[Section],
        tables: list[ExtractedTable],
    ) -> str:
        lines = [
            f"# Document Index: {filename}",
            f"Total lines: {total_lines:,}",
            "",
            "## How to navigate",
            "Use `read_section` to read a specific section without loading the full file.",
            "Format: `start_line,end_line` — e.g. `100,235` means lines 100 to 235.",
            "Use `csv_stats` on extracted table files — never parse raw pipe tables.",
            "",
        ]

        if sections:
            lines.append("## Table of Contents")
            lines.append("")
            for s in sections:
                indent = "  " * (s.level - 1)
                prefix = "#" * s.level
                # Key format the LLM sees: "# Section Title  start,end"
                lines.append(
                    f"{indent}{prefix} {s.title}  {s.start_line},{s.end_line}"
                )
            lines.append("")

        if tables:
            lines.append("## Extracted Tables")
            lines.append("")
            lines.append("These tables have been extracted to CSV files — use `csv_stats` on them.")
            lines.append("")
            for tbl in tables:
                caption_note = f" — {tbl.caption}" if tbl.caption else ""
                lines.append(
                    f"- `{tbl.name}`  (source lines {tbl.start_line}–{tbl.end_line}{caption_note})"
                )
            lines.append("")

        lines += [
            "## Quick reference",
            "```",
            f"read_section(path='{filename}', start_line=N, end_line=M)  # read a section",
            f"grep_files(pattern='keyword', path_filter='{filename}')     # search within doc",
            f"csv_stats(path='<table>.csv')                               # analyse a table",
            "```",
        ]

        return "\n".join(lines) + "\n"
