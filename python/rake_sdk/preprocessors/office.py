"""
Office document preprocessor.

Converts Microsoft Office formats to text/CSV that rake can analyse:

  .docx  → <name>.md   (heading-aware markdown, tables extracted as CSV)
  .xlsx  → one CSV per sheet, plus a workbook _index.md
  .pptx  → <name>.md   (one section per slide with slide number)
  .pdf   → <name>.txt  (best-effort text extraction, page-based sections)

All converted files get an _index.md so the LLM can navigate sections
without loading the whole document.

Requires (install extras):
  pip install "rake-sdk[office]"
  → python-docx, openpyxl, python-pptx, pdfminer.six
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional

from .markdown import MarkdownPreprocessor


def convert(filename: str, content: bytes) -> dict[str, bytes]:
    """
    Convert an office document to text files suitable for rake analysis.
    Returns a dict of {filename: bytes} — may include multiple files.
    """
    ext = Path(filename).suffix.lower()
    stem = Path(filename).stem

    converters = {
        ".docx": _convert_docx,
        ".doc":  _convert_docx,
        ".xlsx": _convert_xlsx,
        ".xls":  _convert_xlsx,
        ".pptx": _convert_pptx,
        ".ppt":  _convert_pptx,
        ".pdf":  _convert_pdf,
    }

    fn = converters.get(ext)
    if fn is None:
        # Unknown format — mount as-is
        return {filename: content}

    return fn(filename, stem, content)


# ── DOCX ──────────────────────────────────────────────────────────────────────

def _convert_docx(filename: str, stem: str, content: bytes) -> dict[str, bytes]:
    try:
        from docx import Document  # type: ignore
    except ImportError:
        return {filename: _not_installed_placeholder(filename, "python-docx")}

    doc = Document(io.BytesIO(content))
    md_lines: list[str] = []
    table_no = 0
    csv_files: dict[str, bytes] = {}

    for block in doc.element.body:
        tag = block.tag.split("}")[-1] if "}" in block.tag else block.tag

        if tag == "p":
            from docx.oxml.ns import qn
            # Check paragraph style
            para_style = block.find(f".//{{{block.nsmap.get('w', '')}}}pStyle") if block.nsmap else None
            # Walk paragraphs from the doc
            for para in doc.paragraphs:
                if para._element is block:
                    text = para.text.strip()
                    style = para.style.name if para.style else ""
                    if not text:
                        md_lines.append("")
                    elif "Heading 1" in style:
                        md_lines.append(f"# {text}")
                    elif "Heading 2" in style:
                        md_lines.append(f"## {text}")
                    elif "Heading 3" in style:
                        md_lines.append(f"### {text}")
                    elif "Heading" in style:
                        level = re.search(r"\d+", style)
                        prefix = "#" * (int(level.group()) if level else 4)
                        md_lines.append(f"{prefix} {text}")
                    else:
                        md_lines.append(text)
                    break

        elif tag == "tbl":
            # Extract table to CSV
            for tbl in doc.tables:
                if tbl._element is block:
                    table_no += 1
                    csv_name = f"{stem}.table_{table_no:03d}.csv"
                    csv_text = _docx_table_to_csv(tbl)
                    csv_files[csv_name] = csv_text.encode("utf-8")
                    md_lines.append(f"\n[Table {table_no} extracted → `{csv_name}`]\n")
                    break

    md_text = "\n".join(md_lines)
    md_name = f"{stem}.md"
    md_bytes = md_text.encode("utf-8")

    # Generate index for the converted markdown
    result = MarkdownPreprocessor().process(md_name, md_bytes)
    result.update(csv_files)
    return result


def _docx_table_to_csv(tbl) -> str:
    import csv, io
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in tbl.rows:
        writer.writerow([cell.text.strip() for cell in row.cells])
    return buf.getvalue()


# ── XLSX ──────────────────────────────────────────────────────────────────────

def _convert_xlsx(filename: str, stem: str, content: bytes) -> dict[str, bytes]:
    try:
        import openpyxl  # type: ignore
    except ImportError:
        return {filename: _not_installed_placeholder(filename, "openpyxl")}

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    result: dict[str, bytes] = {}
    sheet_names: list[tuple[str, str]] = []  # (sheet_name, csv_filename)

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        safe_name = re.sub(r"[^\w\-]", "_", sheet_name)
        csv_name = f"{stem}.{safe_name}.csv"

        import csv, io as _io
        buf = _io.StringIO()
        writer = csv.writer(buf)

        # Find actual data extent
        max_row = ws.max_row or 0
        max_col = ws.max_column or 0

        if max_row == 0 or max_col == 0:
            continue  # empty sheet

        for row in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col, values_only=True):
            writer.writerow(["" if v is None else str(v) for v in row])

        csv_content = buf.getvalue()
        if csv_content.strip():
            result[csv_name] = csv_content.encode("utf-8")
            sheet_names.append((sheet_name, csv_name))

    # Build workbook index
    index_lines = [
        f"# Workbook Index: {filename}",
        f"Sheets: {len(sheet_names)}",
        "",
        "## Sheets",
        "",
        "Use `csv_stats` on each sheet CSV. Do NOT use `read_file` on them.",
        "",
    ]
    for sheet_name, csv_name in sheet_names:
        index_lines.append(f"- **{sheet_name}** → `{csv_name}`")

    index_lines += [
        "",
        "## Quick reference",
        "```",
        "csv_stats(path='<sheet>.csv', sample_rows=10)  # profile a sheet",
        "grep_files(pattern='keyword', path_filter='.csv')  # search all sheets",
        "```",
    ]
    index_name = f"{stem}._index.md"
    result[index_name] = "\n".join(index_lines).encode("utf-8")

    return result


# ── PPTX ──────────────────────────────────────────────────────────────────────

def _convert_pptx(filename: str, stem: str, content: bytes) -> dict[str, bytes]:
    try:
        from pptx import Presentation  # type: ignore
        from pptx.util import Pt  # type: ignore
    except ImportError:
        return {filename: _not_installed_placeholder(filename, "python-pptx")}

    prs = Presentation(io.BytesIO(content))
    md_lines: list[str] = []

    for slide_no, slide in enumerate(prs.slides, start=1):
        md_lines.append(f"## Slide {slide_no}")
        md_lines.append("")

        # Try to get the slide title
        if slide.shapes.title and slide.shapes.title.text:
            md_lines.append(f"**{slide.shapes.title.text.strip()}**")
            md_lines.append("")

        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            if shape == slide.shapes.title:
                continue  # already printed
            for para in shape.text_frame.paragraphs:
                text = para.text.strip()
                if text:
                    # Check if it looks like a bullet
                    level = para.level
                    prefix = "  " * level + ("- " if level > 0 else "")
                    md_lines.append(f"{prefix}{text}")
        md_lines.append("")

    md_text = "\n".join(md_lines)
    md_name = f"{stem}.md"
    md_bytes = md_text.encode("utf-8")

    return MarkdownPreprocessor().process(md_name, md_bytes)


# ── PDF ───────────────────────────────────────────────────────────────────────

def _convert_pdf(filename: str, stem: str, content: bytes) -> dict[str, bytes]:
    try:
        from pdfminer.high_level import extract_pages  # type: ignore
        from pdfminer.layout import LTTextContainer  # type: ignore
    except ImportError:
        return {filename: _not_installed_placeholder(filename, "pdfminer.six")}

    text_lines: list[str] = []
    page_no = 0

    for page_layout in extract_pages(io.BytesIO(content)):
        page_no += 1
        text_lines.append(f"## Page {page_no}")
        text_lines.append("")
        for element in page_layout:
            if isinstance(element, LTTextContainer):
                for line in element.get_text().splitlines():
                    stripped = line.rstrip()
                    if stripped:
                        text_lines.append(stripped)
        text_lines.append("")

    txt_text = "\n".join(text_lines)
    txt_name = f"{stem}.txt"
    txt_bytes = txt_text.encode("utf-8")

    return MarkdownPreprocessor().process(txt_name, txt_bytes)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _not_installed_placeholder(filename: str, package: str) -> bytes:
    return (
        f"# Conversion skipped: {filename}\n"
        f"Install `{package}` to enable conversion: pip install 'rake-sdk[office]'\n"
    ).encode("utf-8")
