"""Structured logging with automatic secret/credential redaction."""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Iterable

_SECRET_PATTERNS = [
    re.compile(r"(PK[A-Z0-9]{14,})", re.IGNORECASE),
    re.compile(r"(AK[A-Z0-9]{14,})", re.IGNORECASE),
    re.compile(r"(SK[A-Z0-9]{14,})", re.IGNORECASE),
    re.compile(r"(APCA-API-SECRET-KEY\s*[:=]\s*)([^\s,;\"']+)", re.IGNORECASE),
    re.compile(r"(APCA-API-KEY-ID\s*[:=]\s*)([^\s,;\"']+)", re.IGNORECASE),
    re.compile(r"(Bearer\s+)([A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE),
]


class SecretScrubbingFilter(logging.Filter):
    """Redacts API keys, secret tokens, and explicit sensitive env values from logs."""

    def __init__(self, explicit_secrets: Iterable[str] | None = None) -> None:
        super().__init__()
        self._explicit: list[str] = [
            s for s in (explicit_secrets or []) if s and len(s) >= 6
        ]

    def register_secret(self, secret: str | None) -> None:
        if secret and len(secret) >= 6 and secret not in self._explicit:
            self._explicit.append(secret)

    def _scrub(self, text: str) -> str:
        scrubbed = text
        for secret in self._explicit:
            if secret in scrubbed:
                scrubbed = scrubbed.replace(secret, "[REDACTED]")
        for pattern in _SECRET_PATTERNS:
            if pattern.groups == 2:
                scrubbed = pattern.sub(r"\1[REDACTED]", scrubbed)
            else:
                scrubbed = pattern.sub("[REDACTED]", scrubbed)
        return scrubbed

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._scrub(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._scrub(a) if isinstance(a, str) else a for a in record.args
                )
        return True


_GLOBAL_SCRUBBER = SecretScrubbingFilter()


def get_logger(name: str = "spy_daytrader", level: str | None = None) -> logging.Logger:
    """Return a configured logger protected by SecretScrubbingFilter."""
    for env_key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        _GLOBAL_SCRUBBER.register_secret(os.getenv(env_key))

    logger = logging.getLogger(name)
    resolved_level = getattr(
        logging, (level or os.getenv("LOG_LEVEL", "INFO")).upper(), logging.INFO
    )
    logger.setLevel(resolved_level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler.addFilter(_GLOBAL_SCRUBBER)
        logger.addHandler(handler)
        logger.propagate = False
    else:
        for handler in logger.handlers:
            if _GLOBAL_SCRUBBER not in handler.filters:
                handler.addFilter(_GLOBAL_SCRUBBER)

    return logger

