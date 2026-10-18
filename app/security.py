"""Recursive PII scrubber.

Sensitive keys are matched fuzzily (case, separators). Whitelisted prose
fields pass through untouched so ordinary user text is not obfuscated.
Every other string is scanned for bearer material, key-shaped tokens,
SSNs, and Luhn-valid card numbers.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Prose fields. Matched on the raw key, case-insensitive, not fuzzy —
# otherwise "description" could collide with unrelated fragments.
SAFE_PASS_WHITELIST: frozenset[str] = frozenset({"subject", "message", "description"})

# Fragments compared against a lowered, separator-stripped key.
_SENSITIVE_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "authheader",
    "creditcard",
    "cardnumber",
    "cvv",
    "cvc",
    "ssn",
    "socialsecurity",
    "privatekey",
    "accesskey",
    "refreshtoken",
    "sessionid",
    "bearer",
    "clientsecret",
)

# Strict bearer / vendor-key / JWT catcher. Card and SSN patterns are
# applied separately so a Luhn check can reject innocent digit runs.
TOKEN_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/=]{8,}"
    r"|(?<![A-Za-z0-9])(?:sk|pk|rk|ak)[-_][A-Za-z0-9]{8,}(?![A-Za-z0-9])"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}"
)

_SSN_PATTERN: re.Pattern[str] = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_CANDIDATE: re.Pattern[str] = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def key_is_sensitive(key: str) -> bool:
    """True when a mapping key should have its value replaced wholesale."""
    if key.lower() in SAFE_PASS_WHITELIST:
        return False
    normalized = _normalize_key(key)
    return any(fragment in normalized for fragment in _SENSITIVE_FRAGMENTS)


def _luhn_ok(digits: str) -> bool:
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = ord(char) - 48
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _redact_cards(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if _luhn_ok(digits):
            return REDACTED
        return match.group(0)

    return _CARD_CANDIDATE.sub(replace, text)


def scrub_string(value: str) -> str:
    """Scrub bearer configurations and other raw PII inside a string."""
    cleaned = TOKEN_PATTERN.sub(REDACTED, value)
    cleaned = _SSN_PATTERN.sub(REDACTED, cleaned)
    return _redact_cards(cleaned)


def _passthrough(value: Any) -> Any:
    """Copy a whitelisted subtree without redaction."""
    if isinstance(value, dict):
        return {str(key): _passthrough(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_passthrough(item) for item in value]
    return value


def deep_scrub(payload: Any) -> Any:
    """Recursively redact mappings and sequences.

    Whitelisted keys (`subject`, `message`, `description`) are copied as-is,
    including nested values, so user-authored text is preserved. Sensitive
    keys are replaced entirely. Other strings are pattern-scrubbed.
    """
    return _scrub(payload, 0)


def _scrub(payload: Any, depth: int) -> Any:
    if depth > 40:
        return REDACTED
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        for raw_key, value in payload.items():
            key = str(raw_key)
            if key.lower() in SAFE_PASS_WHITELIST:
                redacted[key] = _passthrough(value)
            elif key_is_sensitive(key):
                redacted[key] = REDACTED
            else:
                redacted[key] = _scrub(value, depth + 1)
        return redacted
    if isinstance(payload, (list, tuple)):
        return [_scrub(item, depth + 1) for item in payload]
    if isinstance(payload, str):
        return scrub_string(payload)
    return payload
