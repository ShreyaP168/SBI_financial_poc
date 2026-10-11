"""Reference indexes follow file versions instead of reloading Python modules."""

import hashlib
from pathlib import Path


def source_revision(path: Path) -> tuple:
    path = path.resolve()
    if not path.is_file():
        return (str(path), None)
    stat = path.stat()
    return (
        str(path),
        stat.st_mtime_ns,
        stat.st_size,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
