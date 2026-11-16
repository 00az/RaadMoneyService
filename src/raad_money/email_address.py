"""C1 email normalization shared by lookup, self-transfer comparison and fingerprints."""

from __future__ import annotations

import unicodedata

MAX_RAW_EMAIL_LENGTH = 512
MAX_NORMALIZED_EMAIL_LENGTH = 254


def normalize_email(email: str) -> str:
    """Trim Unicode whitespace, NFC-normalize and lowercase, then validate the whole address."""
    if not isinstance(email, str):
        raise TypeError("valid email is required")
    if len(email) > MAX_RAW_EMAIL_LENGTH:
        raise ValueError("valid email is required")
    normalized = unicodedata.normalize("NFC", email.strip()).lower()
    local, separator, domain = normalized.partition("@")
    if (
        not separator
        or not local
        or not domain
        or "@" in domain
        or len(normalized) > MAX_NORMALIZED_EMAIL_LENGTH
        or "." not in domain
        or any(
            character.isspace() or unicodedata.category(character) == "Cc"
            for character in normalized
        )
    ):
        raise ValueError("valid email is required")
    return normalized
