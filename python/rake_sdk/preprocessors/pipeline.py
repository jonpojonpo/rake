"""Route each file to the right preprocessor."""
from __future__ import annotations
from pathlib import Path

_MARKDOWN_EXTS = {".md", ".markdown", ".txt", ".rst"}
_OFFICE_EXTS   = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf"}
_ARCHIVE_EXTS  = {".zip"}


def preprocess_file(filename: str, content: bytes) -> dict[str, bytes]:
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
    return {filename: content}


def preprocess_files(named_files: dict[str, bytes]) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for filename, content in named_files.items():
        result.update(preprocess_file(filename, content))
    return result


def _looks_like_document(content: bytes, sample: int = 2048) -> bool:
    chunk = content[:sample].decode("utf-8", errors="replace")
    # Markdown heading anywhere → always index
    if any(line.lstrip().startswith("#") for line in chunk.splitlines()[:20]):
        return True
    total = len(chunk)
    if total < 100:
        return False
    prose = sum(chunk.count(c) for c in ".!?,;")
    code  = sum(chunk.count(c) for c in "{}[]()=><|&^%$")
    return prose > code
