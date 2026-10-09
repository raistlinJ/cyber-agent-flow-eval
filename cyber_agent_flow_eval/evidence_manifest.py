"""Hashes of collected evidence, independent of verdicts and agent claims."""

from pathlib import Path
import hashlib
import mimetypes
from .storage import write_json


def capture(directory):
    root = Path(directory).resolve()
    source = root / "guest-output" if (root / "guest-output").is_dir() else root
    entries = []
    for path in sorted(source.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(source)
        ):
            continue
        if source == root and any(
            part in {"reset-suite"} for part in path.relative_to(root).parts
        ):
            continue
        if path.name in {
            "evidence-manifest.json",
            "judge.json",
            "evaluation.json",
            "attempt.json",
        }:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                digest.update(block)
        entries.append(
            dict(
                file=str(path.relative_to(source)),
                bytes=path.stat().st_size,
                sha256=digest.hexdigest(),
                mime=mimetypes.guess_type(path.name)[0],
            )
        )
    result = dict(
        version=1,
        files=entries,
        source="guest-output" if source != root else ".",
        complete=True,
    )
    write_json(root / "evidence-manifest.json", result)
    return result
