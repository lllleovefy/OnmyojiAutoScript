from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from typing import Any


_SENSITIVE_KEY_PARTS = (
    "access_token",
    "accesstoken",
    "authorization",
    "authorized-token",
    "cookie",
    "password",
    "refresh_token",
    "refreshtoken",
    "secret",
)
_BEARER_RE = re.compile(r"(?i)^\s*bearer\s+\S+")
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}$")


def is_sensitive_key(key: object) -> bool:
    normalized = str(key).replace("-", "_").lower()
    return (
        normalized == "token"
        or normalized.endswith("_token")
        or any(part.replace("-", "_") in normalized for part in _SENSITIVE_KEY_PARTS)
    )


def sanitize_for_storage(value: Any) -> Any:
    """Recursively remove credentials before a payload reaches SQLite."""
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return sanitize_for_storage(value.model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return sanitize_for_storage(asdict(value))
    if isinstance(value, dict):
        return {
            str(key): sanitize_for_storage(item)
            for key, item in value.items()
            if not is_sensitive_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_for_storage(item) for item in value]
    if isinstance(value, str) and (_BEARER_RE.match(value) or _JWT_RE.match(value)):
        return "<redacted>"
    return value
