"""Opaque history cursors, HMAC-bound to caller, environment, page size, snapshot and expiry.

The token is `<key version>.<base64url payload>.<base64url MAC>`. The payload holds only
positions (ordinals and sort keys), never the caller, wallet, environment or profile data;
those are bound by the MAC instead.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

CURSOR_LIFETIME_SECONDS = 86_400
MAX_CURSOR_LENGTH = 2048
_MIN_KEY_LENGTH = 32
_MAC_DOMAIN = "raad-history-cursor"
_FORMAT_VERSION = 1
_SORT_ORDER = "completedAt-desc,transferId-desc"


class CursorInvalid(ValueError):
    """Altered, expired, foreign or malformed cursor: the client reloads page one."""


@dataclass(frozen=True, slots=True)
class CursorKeys:
    """Signing key for new cursors plus the previous key during a >=24h rotation overlap."""

    version: str
    key: bytes
    previous: tuple[tuple[str, bytes], ...] = ()

    @classmethod
    def from_secret(cls, secret_string: str) -> CursorKeys:
        try:
            secret = json.loads(secret_string)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("cursor secret is not valid JSON") from error
        if not isinstance(secret, dict):
            raise TypeError("cursor secret must be an object")
        version, key = _key_pair(secret.get("version"), secret.get("key"))
        previous: tuple[tuple[str, bytes], ...] = ()
        if secret.get("previousVersion") or secret.get("previousKey"):
            previous = (_key_pair(secret.get("previousVersion"), secret.get("previousKey")),)
        return cls(version, key, previous)

    def for_version(self, version: str) -> bytes | None:
        if hmac.compare_digest(version, self.version):
            return self.key
        for candidate, key in self.previous:
            if hmac.compare_digest(version, candidate):
                return key
        return None


def _key_pair(version: object, key: object) -> tuple[str, bytes]:
    if not isinstance(version, str) or not version or "." in version or len(version) > 32:
        raise ValueError("cursor key version is required")
    if not isinstance(key, str) or len(key) < _MIN_KEY_LENGTH:
        raise ValueError("cursor key material is too weak")
    return version, key.encode()


@dataclass(frozen=True, slots=True)
class HistoryPosition:
    """Where a valid chain continues: strictly after `last_key`, expecting `next_ordinal`."""

    expires_at: int
    limit: int
    snapshot_sequence: int
    snapshot_key: str
    next_ordinal: int
    last_key: str


def issue_cursor(
    position: HistoryPosition, *, keys: CursorKeys, environment: str, caller: str
) -> str:
    payload = {
        "v": _FORMAT_VERSION,
        "exp": position.expires_at,
        "lim": position.limit,
        "sn": position.snapshot_sequence,
        "sk": position.snapshot_key,
        "no": position.next_ordinal,
        "lk": position.last_key,
    }
    encoded = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    mac = _mac(keys.key, keys.version, encoded, environment, caller)
    token = f"{keys.version}.{encoded}.{mac}"
    if len(token) > MAX_CURSOR_LENGTH:
        raise ValueError("cursor exceeds the contract length")
    return token


def read_cursor(
    token: str,
    *,
    keys: CursorKeys,
    environment: str,
    caller: str,
    limit: int,
    now: int,
) -> HistoryPosition:
    """Verify every binding before trusting any position in the cursor."""
    if not isinstance(token, str) or not 0 < len(token) <= MAX_CURSOR_LENGTH:
        raise CursorInvalid("cursor length")
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise CursorInvalid("cursor shape")
    version, encoded, mac = parts
    key = keys.for_version(version)
    if key is None or not hmac.compare_digest(
        mac, _mac(key, version, encoded, environment, caller)
    ):
        raise CursorInvalid("cursor signature")
    position = _decode(encoded)
    if (
        position.limit != limit
        or position.expires_at <= now
        or not 1 <= position.next_ordinal < position.snapshot_sequence
        or not position.last_key
        or position.last_key > position.snapshot_key
    ):
        raise CursorInvalid("cursor binding")
    return position


def _decode(encoded: str) -> HistoryPosition:
    try:
        payload: Any = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        raise CursorInvalid("cursor payload") from error
    expected = {"v", "exp", "lim", "sn", "sk", "no", "lk"}
    if not isinstance(payload, dict) or set(payload) != expected or payload["v"] != 1:
        raise CursorInvalid("cursor payload fields")
    numbers = [payload[field] for field in ("exp", "lim", "sn", "no")]
    texts = [payload[field] for field in ("sk", "lk")]
    if any(isinstance(n, bool) or not isinstance(n, int) for n in numbers) or any(
        not isinstance(t, str) for t in texts
    ):
        raise CursorInvalid("cursor payload types")
    return HistoryPosition(
        expires_at=payload["exp"],
        limit=payload["lim"],
        snapshot_sequence=payload["sn"],
        snapshot_key=payload["sk"],
        next_ordinal=payload["no"],
        last_key=payload["lk"],
    )


def _mac(key: bytes, version: str, encoded: str, environment: str, caller: str) -> str:
    message = "|".join(
        f"{len(part)}:{part}"
        for part in (_MAC_DOMAIN, version, environment, caller, _SORT_ORDER, encoded)
    )
    return _b64(hmac.new(key, message.encode(), hashlib.sha256).digest())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")
