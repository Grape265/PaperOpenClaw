"""Content-addressed hashing.

A paper's identity is the SHA-256 of its PDF byte stream (FR-1.2/FR-2.2), so
hashing must be streaming: papers routinely exceed the memory we want to spend
on a single upload.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO, Optional, Union

CHUNK_SIZE = 1024 * 1024  # 1 MiB


def hash_stream(stream: BinaryIO, chunk_size: int = CHUNK_SIZE) -> str:
    """Return the lowercase hex SHA-256 of a binary stream."""
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def hash_file(path: Union[str, Path], chunk_size: int = CHUNK_SIZE) -> str:
    """Return the lowercase hex SHA-256 of a file on disk."""
    with open(path, "rb") as handle:
        return hash_stream(handle, chunk_size=chunk_size)


def hash_bytes(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of an in-memory buffer."""
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    """Return the lowercase hex SHA-256 of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(value: str, length: int = 8) -> str:
    """Human-facing abbreviation of a hash, e.g. ``3f2a1b9c``."""
    if not value:
        return ""
    return value[:length]


def stable_id(*parts: object, length: Optional[int] = 16) -> str:
    """Deterministic identifier derived from arbitrary parts.

    Used for job ids, node keys and unit ids so that re-running a pipeline
    stage is idempotent rather than producing duplicates.
    """
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest if length is None else digest[:length]


def is_sha256(value: str) -> bool:
    """True when ``value`` looks like a full SHA-256 hex digest."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value.lower())
