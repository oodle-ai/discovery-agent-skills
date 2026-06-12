"""Redaction of secrets from captured API responses and configs.

Every byte written to disk by a collector passes through redact() first.
This repo is public and the evidence files travel with the report, so the
bias is toward over-redaction: a scrubbed storage key costs nothing, a
leaked one is an incident.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "<redacted>"

# Key names whose values are always scrubbed, wherever they appear.
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)^(.*[_\-])?("
    r"password|passwd|secret|token|api[_\-]?key|apikey|app[_\-]?key|appkey"
    r"|authorization|auth|cookie|session[_\-]?id|credential[s]?"
    r"|access[_\-]?key([_\-]?id)?|private[_\-]?key|client[_\-]?secret"
    r"|bearer|sigv4|signature"
    r")([_\-].*)?$"
)

# Header names scrubbed when dicts look like HTTP headers.
_SENSITIVE_HEADER_RE = re.compile(
    r"(?i)^(authorization|cookie|set-cookie|proxy-authorization"
    r"|dd-api-key|dd-application-key|x-amz-security-token|x-goog-.*-token"
    r"|x-api-key)$"
)

# Inline credentials in URL strings: scheme://user:pass@host
_URL_USERINFO_RE = re.compile(r"(\w+://)([^/@\s]+)@")

# Long opaque strings that look like credentials inside free-form text
# (e.g. config dumps rendered as strings). Conservative: only obvious shapes.
_INLINE_SECRET_RE = re.compile(
    r"(?i)((?:password|secret|token|api[_\-]?key|access[_\-]?key(?:[_\-]?id)?)"
    r"\s*[:=]\s*)(\"[^\"]+\"|'[^']+'|\S+)"
)


def redact_key(key: str) -> bool:
    """True if a dict key's value must be scrubbed."""
    return bool(_SENSITIVE_KEY_RE.match(key) or _SENSITIVE_HEADER_RE.match(key))


def redact_string(s: str) -> str:
    """Scrub credential shapes inside a free-form string."""
    s = _URL_USERINFO_RE.sub(r"\1" + REDACTED + "@", s)
    s = _INLINE_SECRET_RE.sub(r"\1" + REDACTED, s)
    return s


def redact(obj: Any) -> Any:
    """Return a deep copy of obj with secrets scrubbed.

    - dict values under sensitive keys -> "<redacted>"
    - URL userinfo and key=value credential shapes inside strings -> scrubbed
    - lists/tuples recursed
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and redact_key(k):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_string(obj)
    return obj
