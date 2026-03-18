"""
Preprocessing pipeline — routes each file to the right preprocessor.

Call `preprocess_files(named_files)` before mounting into rake.
It returns an expanded dict that may include index files and extracted CSVs.
"""

from __future__ import annotations

from pathlib import Path


# Extensions handled by each preprocessor
_MARKDOWN_EXTS = {".md", ".markdown", ".txt", ".rst"}
_OFFICE_EXTS   = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf"}
_ARCHIVE_EXTS  = {".zip"}


def preprocess_file(filename: str, content: bytes) -> dict[str, bytes]:
    """
    Preprocess a single file. Returns a dict of virtual {name: bytes}.
    The original file is always included.
    """
    ext = Path(filename).suffix.lower()

    if ext in _ARCHIVE_EXTS:
        from .archive import ArchivePreprocessor
        return ArchivePreprocessor().process(filename, content)

    if ext in _OFFICE_EXTS:
        from .office import convert
        return convert(filename, content)

    if ext in _MARKDOWN_EXTS and _looks_like_document(content):
        from .markdown import MarkdownPreprocessor
        return MarkdownPreprocessor().process(filename, content)

    # Pass through unchanged
    return {filename: content}


def preprocess_files(named_files: dict[str, bytes]) -> dict[str, bytes]:
    """
    Run all files through the preprocessing pipeline.
    Returns an expanded dict ready to mount into the rake sandbox.
    """
    result: dict[str, bytes] = {}
    for filename, content in named_files.items():
        processed = preprocess_file(filename, content)
        result.update(processed)
    return result


def _looks_like_document(content: bytes, sample: int = 2048) -> bool:
    """
    Return True if the content looks like a human-readable document
    (as opposed to code or config files).
    Heuristic: markdown headings present, or more prose punctuation than
    code operators.
    """
    chunk = content[:sample].decode("utf-8", errors="replace")

    # Markdown heading anywhere in the file → always index
    if any(line.lstrip().startswith("#") for line in chunk.splitlines()[:20]):
        return True

    total = len(chunk)
    if total < 100:
        return False

    prose_chars = sum(chunk.count(c) for c in ".!?,;")
    code_chars  = sum(chunk.count(c) for c in "{}[]()=><|&^%$")
    return prose_chars > code_chars
