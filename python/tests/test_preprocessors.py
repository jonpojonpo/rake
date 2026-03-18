"""
Tests for document preprocessors.

Run:
  pytest python/tests/test_preprocessors.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from rake_sdk.preprocessors.markdown import MarkdownPreprocessor, Section
from rake_sdk.preprocessors.pipeline import preprocess_files, preprocess_file


SAMPLE_MD = b"""# Annual Report 2024

## Chairman's Statement

Dear Shareholders, this was a great year.

## Financial Highlights

| Metric | 2024 | 2023 |
|--------|------|------|
| Revenue | 100 | 85 |
| Profit | 20 | 15 |

## Risk Factors

Risk 1: Market risk.
Risk 2: Operational risk.

### Regulatory Risk

New regulations may apply.
"""


# ── MarkdownPreprocessor ──────────────────────────────────────────────────────

class TestMarkdownPreprocessor:

    def test_generates_index(self):
        result = MarkdownPreprocessor().process("report.md", SAMPLE_MD)
        assert "report._index.md" in result

    def test_original_preserved(self):
        result = MarkdownPreprocessor().process("report.md", SAMPLE_MD)
        assert "report.md" in result
        assert result["report.md"] == SAMPLE_MD

    def test_extracts_table_as_csv(self):
        result = MarkdownPreprocessor().process("report.md", SAMPLE_MD)
        csv_files = [k for k in result if k.endswith(".csv")]
        assert len(csv_files) == 1
        csv_name = csv_files[0]
        csv_content = result[csv_name].decode("utf-8")
        assert "Revenue" in csv_content
        assert "100" in csv_content

    def test_index_contains_section_line_ranges(self):
        result = MarkdownPreprocessor().process("report.md", SAMPLE_MD)
        index = result["report._index.md"].decode("utf-8")
        # Should contain heading with line range like "# Title  N,M"
        assert "Chairman's Statement" in index
        assert "Financial Highlights" in index
        assert "Risk Factors" in index
        # Line ranges should be present (digits separated by comma)
        import re
        assert re.search(r"\d+,\d+", index), "Expected line ranges in index"

    def test_index_references_extracted_csv(self):
        result = MarkdownPreprocessor().process("report.md", SAMPLE_MD)
        index = result["report._index.md"].decode("utf-8")
        # Index should mention the extracted CSV
        assert ".csv" in index

    def test_section_extraction(self):
        proc = MarkdownPreprocessor()
        lines = SAMPLE_MD.decode("utf-8").splitlines()
        sections = proc._extract_sections(lines)
        titles = [s.title for s in sections]
        assert "Annual Report 2024" in titles
        assert "Chairman's Statement" in titles
        assert "Financial Highlights" in titles
        assert "Risk Factors" in titles
        assert "Regulatory Risk" in titles

    def test_section_line_numbers_are_positive(self):
        proc = MarkdownPreprocessor()
        lines = SAMPLE_MD.decode("utf-8").splitlines()
        sections = proc._extract_sections(lines)
        for s in sections:
            assert s.start_line >= 1
            assert s.end_line >= s.start_line

    def test_section_levels(self):
        proc = MarkdownPreprocessor()
        lines = SAMPLE_MD.decode("utf-8").splitlines()
        sections = proc._extract_sections(lines)
        h1 = [s for s in sections if s.level == 1]
        h2 = [s for s in sections if s.level == 2]
        h3 = [s for s in sections if s.level == 3]
        assert len(h1) == 1   # "Annual Report 2024"
        assert len(h2) == 3   # Chairman, Financial, Risk
        assert len(h3) == 1   # Regulatory Risk

    def test_table_to_csv(self):
        table_lines = [
            "| Metric | 2024 | 2023 |",
            "|--------|------|------|",
            "| Revenue | 100 | 85 |",
            "| Profit | 20 | 15 |",
        ]
        csv = MarkdownPreprocessor._table_to_csv(table_lines)
        assert "Metric" in csv
        assert "Revenue" in csv
        assert "100" in csv

    def test_no_tables(self):
        simple = b"# Title\n\nSome text without any tables.\n"
        result = MarkdownPreprocessor().process("simple.md", simple)
        csv_files = [k for k in result if k.endswith(".csv")]
        assert len(csv_files) == 0

    def test_multiple_tables(self):
        multi = b"""# Report

## Section A

| A | B |
|---|---|
| 1 | 2 |

## Section B

| C | D |
|---|---|
| 3 | 4 |
"""
        result = MarkdownPreprocessor().process("multi.md", multi)
        csv_files = [k for k in result if k.endswith(".csv")]
        assert len(csv_files) == 2


# ── Pipeline ──────────────────────────────────────────────────────────────────

class TestPipeline:

    def test_passthrough_python(self):
        content = b"print('hello')"
        result = preprocess_file("script.py", content)
        assert "script.py" in result
        assert result["script.py"] == content

    def test_passthrough_json(self):
        content = b'{"key": "value"}'
        result = preprocess_file("data.json", content)
        assert "data.json" in result
        assert result["data.json"] == content

    def test_markdown_processed(self):
        content = b"# Title\n\nContent with **markdown**.\n"
        result = preprocess_file("doc.md", content)
        # Should generate an index
        assert any(k.endswith("._index.md") for k in result)

    def test_multiple_files(self):
        files = {
            "report.md": SAMPLE_MD,
            "code.py": b"x = 1",
        }
        result = preprocess_files(files)
        # Original files preserved
        assert "report.md" in result
        assert "code.py" in result
        # Index generated for markdown
        assert "report._index.md" in result
        # Python not indexed
        assert "code._index.md" not in result

    def test_zip_not_installed_gracefully(self):
        """ZIP processing should not crash if zipfile can be read."""
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("hello.md", "# Hello\n\nWorld\n")
        content = buf.getvalue()
        result = preprocess_file("bundle.zip", content)
        assert len(result) > 0  # at least something returned
