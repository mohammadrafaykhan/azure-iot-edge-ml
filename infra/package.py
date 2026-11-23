"""Package the image build context (edgeml/, ml/, modules/) as a reproducible tar.gz.

    python infra/package.py <repo-root> <output-dir>

The archive is named after a hash of its contents, so the same source always produces the same
name and the images built from it get the same tag. Prints JSON: {"path", "blob", "hash"}.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

INCLUDE = ("edgeml", "ml", "modules")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def source_files(root: Path) -> list[str]:
    files = []
    for top in INCLUDE:
        for path in (root / top).rglob("*"):
            relative = path.relative_to(root)
            if path.is_file() and not SKIP_DIRS & set(relative.parts) and path.suffix not in SKIP_SUFFIXES:
                files.append(relative.as_posix())
    return sorted(files)


def content_hash(root: Path, files: list[str]) -> str:
    digest = hashlib.sha256()
    for name in files:
        digest.update(name.encode() + b"\0" + (root / name).read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def build(root: Path, out_dir: Path) -> dict[str, str]:
    files = source_files(root)
    if not files:
        raise SystemExit(f"nothing to package under {root}")
    source_hash = content_hash(root, files)
    blob = f"context-{source_hash}.tar.gz"
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for name in files:
            data = (root / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / blob
    path.write_bytes(buffer.getvalue())
    return {"path": str(path), "blob": blob, "hash": source_hash}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: package.py <repo-root> <output-dir>")
    print(json.dumps(build(Path(sys.argv[1]), Path(sys.argv[2]))))
