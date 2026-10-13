"""Logging setup with secret redaction (NFR-5, FR-14.3)."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Optional

_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)([A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)([A-Za-z0-9._\-]{6})[A-Za-z0-9._\-]+"),
]


class RedactSecretsFilter(logging.Filter):
    """Scrub credential-looking fragments before they reach a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def redact(text: str) -> str:
    """Mask API keys and bearer tokens inside arbitrary text."""
    if not text:
        return text
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: m.group(1) + "****", out)
    return out


_configured = False


def setup_logging(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    filename: str = "paperopenclaw.log",
) -> None:
    """Configure root logging once: console always, rotating file when possible."""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = RedactSecretsFilter()

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(redactor)
    root.addHandler(console)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / filename,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redactor)
            root.addHandler(file_handler)
        except Exception as exc:  # pragma: no cover - environment dependent
            root.warning("无法创建日志文件：%s", exc)

    # Third-party noise control.
    for noisy in ("httpx", "httpcore", "chromadb", "urllib3", "asyncio", "jieba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
