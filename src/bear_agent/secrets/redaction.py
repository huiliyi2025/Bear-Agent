"""Secret value and credential-pattern redaction.

Redaction is applied before tool output is persisted/returned, before event
payloads are written, and before audit details are stored. Exact registered
secret values are replaced first; common credential shapes are caught by
patterns as defense in depth.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|"
        r"secret|password)[\"']?\s*[:=]\s*)([\"']?)([^\s\"',}]{6,})"
    ),
    re.compile(r"(?i)(authorization:\s*bearer\s+)([^\s]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
        re.DOTALL,
    ),
)


class SecretRedactor:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def register(self, name: str, value: str) -> None:
        # Short values create too many false positives; real credentials are
        # longer. The name is retained for diagnostics, never the value.
        if value and len(value) >= 6:
            self._values[name] = value

    def unregister(self, name: str) -> None:
        self._values.pop(name, None)

    def redact(self, text: str) -> str:
        if not text:
            return text
        result = text
        for name in sorted(self._values, key=lambda key: len(self._values[key]), reverse=True):
            result = result.replace(self._values[name], f"{REDACTED}:{name}")
        for pattern in _PATTERNS:
            if pattern.groups == 3:
                result = pattern.sub(
                    lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",
                    result,
                )
            elif pattern.groups == 2:
                result = pattern.sub(lambda match: f"{match.group(1)}{REDACTED}", result)
            else:
                result = pattern.sub(REDACTED, result)
        return result

    def redact_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.redact_obj(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_obj(item) for item in value)
        if isinstance(value, dict):
            return {key: self.redact_obj(item) for key, item in value.items()}
        return value
