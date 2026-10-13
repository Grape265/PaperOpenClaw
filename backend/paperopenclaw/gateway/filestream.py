"""File streaming with HTTP Range support.

Starlette's ``FileResponse`` does not honour ``Range`` at the version this app
pins, and an in-app PDF reader needs it: without range support the viewer either
downloads the whole document before showing page one, or fails outright. So the
range handling is implemented here.

Range parsing follows RFC 7233 for the forms that matter in practice
(``bytes=start-end``, ``bytes=start-``, ``bytes=-suffix``) and falls back to a
full response for anything malformed, which is what the spec recommends.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Iterator, Optional, Tuple

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

CHUNK_SIZE = 256 * 1024
_RANGE_RE = re.compile(r"bytes=(?P<start>\d*)-(?P<end>\d*)")


def parse_range(header: str, size: int) -> Optional[Tuple[int, int]]:
    """Return an inclusive ``(start, end)`` byte range, or ``None`` to ignore."""
    if not header or size <= 0:
        return None
    match = _RANGE_RE.fullmatch(header.strip())
    if not match:
        return None
    raw_start = match.group("start")
    raw_end = match.group("end")
    if raw_start == "" and raw_end == "":
        return None
    if raw_start == "":
        # Suffix range: the last N bytes.
        length = int(raw_end)
        if length <= 0:
            return None
        start = max(0, size - length)
        return start, size - 1
    start = int(raw_start)
    if start >= size:
        return None
    if raw_end == "":
        return start, size - 1
    end = min(int(raw_end), size - 1)
    if end < start:
        return None
    return start, end


def _iter_file(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def stream_file(path: Path, request: Request, *, media_type: str = "",
                filename: str = "", inline: bool = True) -> Response:
    """Serve a file, honouring ``Range`` when present."""
    if not path.is_file():
        from ..core.errors import AssetNotFound

        raise AssetNotFound(f"文件不存在：{path.name}")

    size = path.stat().st_size
    guessed = media_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    disposition = "inline" if inline else "attachment"
    if filename:
        # RFC 5987 encoding keeps non-ASCII names intact.
        from urllib.parse import quote

        disposition += f"; filename*=UTF-8''{quote(filename)}"

    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": disposition,
        "Cache-Control": "private, max-age=60",
    }

    range_header = request.headers.get("range") or ""
    parsed = parse_range(range_header, size)
    if parsed is None:
        if range_header:
            # Malformed or unsatisfiable: a full 200 response is the sanctioned
            # fallback and keeps the client working.
            base_headers["Content-Length"] = str(size)
        return StreamingResponse(
            _iter_file(path, 0, max(0, size - 1)) if size else iter(()),
            media_type=guessed,
            headers=base_headers,
            status_code=200,
        )

    start, end = parsed
    headers = dict(base_headers)
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(_iter_file(path, start, end), media_type=guessed,
                             headers=headers, status_code=206)
