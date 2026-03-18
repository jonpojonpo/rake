"""Extract ZIP archives, route each member through the preprocessor pipeline."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

_SKIP = {".pyc",".pyo",".class",".o",".so",".dll",".exe",
         ".jpg",".jpeg",".png",".gif",".mp3",".mp4",".mov"}
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file


class ArchivePreprocessor:
    def process(self, filename: str, content: bytes) -> dict[str, bytes]:
        stem = Path(filename).stem
        result: dict[str, bytes] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                members = [m for m in zf.infolist()
                           if not m.is_dir()
                           and Path(m.filename).suffix.lower() not in _SKIP
                           and m.file_size <= _MAX_BYTES]
                for m in members:
                    vname = f"{stem}/{m.filename}"
                    from .pipeline import preprocess_file
                    result.update(preprocess_file(vname, zf.read(m.filename)))
                manifest = [f"# Archive: {filename}",
                            f"Extracted: {len(members)} files", ""]
                for m in members:
                    result_name = f"{stem}/{m.filename}"
                    manifest.append(f"- `{result_name}` ({m.file_size/1024:.1f} KB)")
                result[f"{stem}._manifest.md"] = "\n".join(manifest).encode()
        except zipfile.BadZipFile as e:
            result[filename] = f"[Bad ZIP: {e}]\n".encode()
        return result
