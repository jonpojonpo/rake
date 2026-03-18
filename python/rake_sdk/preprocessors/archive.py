"""
Archive preprocessor — extracts ZIP files and preprocesses each member.

Supports nested archives (one level of recursion).

Usage:
    from rake_sdk.preprocessors.archive import ArchivePreprocessor
    files = ArchivePreprocessor().process("bundle.zip", zip_bytes)
    # Returns all extracted files, each run through the appropriate preprocessor
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Optional

# Extensions to skip inside archives
_SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".class", ".o", ".a", ".so", ".dylib",
    ".dll", ".exe", ".bin", ".dmg", ".iso",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".svg",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
}

# Maximum size per extracted file (bytes) — skip larger files
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


class ArchivePreprocessor:
    """Extracts ZIP archives and routes each file to the right preprocessor."""

    def __init__(self, max_file_bytes: int = _MAX_FILE_BYTES):
        self.max_file_bytes = max_file_bytes

    def process(self, filename: str, content: bytes) -> dict[str, bytes]:
        """
        Extract a ZIP archive and preprocess each member.
        Returns a flat dict of {virtual_path: bytes}.
        """
        result: dict[str, bytes] = {}

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                members = self._filter_members(zf.infolist())
                zip_stem = Path(filename).stem

                for info in members:
                    member_bytes = zf.read(info.filename)
                    # Prefix with archive stem to avoid name collisions
                    virtual_name = f"{zip_stem}/{info.filename}"
                    sub = self._process_member(virtual_name, info.filename, member_bytes)
                    result.update(sub)

                # Generate a zip manifest as an index
                result[f"{zip_stem}._manifest.md"] = self._build_manifest(
                    filename, zf.infolist(), members
                ).encode("utf-8")

        except zipfile.BadZipFile as e:
            result[filename] = f"[Could not read ZIP: {e}]\n".encode("utf-8")

        return result

    def _filter_members(self, members: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
        """Remove directories, oversized files, and binary junk."""
        out = []
        for m in members:
            if m.is_dir():
                continue
            ext = Path(m.filename).suffix.lower()
            if ext in _SKIP_EXTENSIONS:
                continue
            if m.file_size > self.max_file_bytes:
                continue
            out.append(m)
        return out

    def _process_member(
        self, virtual_name: str, original_name: str, content: bytes
    ) -> dict[str, bytes]:
        """Route a single extracted file to the appropriate preprocessor."""
        from . import pipeline
        return pipeline.preprocess_file(virtual_name, content)

    def _build_manifest(
        self,
        filename: str,
        all_members: list[zipfile.ZipInfo],
        included: list[zipfile.ZipInfo],
    ) -> str:
        stem = Path(filename).stem
        skipped = len(all_members) - len(included)

        lines = [
            f"# Archive Manifest: {filename}",
            f"Total entries: {len(all_members)}  |  Included: {len(included)}  |  Skipped: {skipped}",
            "",
            "## Extracted files",
            "",
            "Use `read_section` + `_index.md` files for large documents.",
            "Use `csv_stats` for CSV/Excel data.",
            "",
        ]

        for m in included:
            size_kb = m.file_size / 1024
            lines.append(
                f"- `{stem}/{m.filename}`  ({size_kb:.1f} KB)"
            )

        if skipped > 0:
            lines += [
                "",
                f"## Skipped entries ({skipped})",
                "Binary files, images, compiled code, and files > 10 MB were excluded.",
            ]

        return "\n".join(lines) + "\n"
