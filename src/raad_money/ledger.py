"""Pure integer-halalas rules for wallets, transfer requests, outcomes and completion order."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from raad_money.email_address import normalize_email

OPENING_BALANCE_HALALAS = 100_000
MIN_TRANSFER_HALALAS = 100
MAX_TRANSFER_HALALAS = 50_000
CONTRACT_MAJOR_VERSION = "1"
REFERENCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
REFERENCE_LENGTH = 12
REFUSAL_STATUS: dict[str, int] = {
    "RECIPIENT_NOT_FOUND": 404,
    "AMOUNT_OUT_OF_RANGE": 422,
    "SELF_TRANSFER": 409,
    "INSUFFICIENT_FUNDS": 422,
}
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
_REFERENCE_PATTERN = re.compile(rf"R-[{REFERENCE_ALPHABET}]{{{REFERENCE_LENGTH}}}")
_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT_DOMAIN = "raad-c1-transfer-fingerprint"


class InsufficientFunds(ValueError):
    """Raised when a transfer would make the sender balance negative."""


class IdempotencyKeyReused(Exception):
    """The caller's key already holds a first outcome for a different fingerprint."""


class CompletionClockBehind(Exception):
    """A wallet's last completion key is not behind the trusted clock yet."""

    def __init__(self, wait_ms: int) -> None:
        super().__init__(f"completion clock is behind by {wait_ms} ms")
        self.wait_ms = wait_ms


@dataclass(frozen=True, slots=True)
class Wallet:
    wallet_id: str
    balance_halalas: int

    def __post_init__(self) -> None:
        if (
            not self.wallet_id
            or isinstance(self.balance_halalas, bool)
            or not isinstance(self.balance_halalas, int)
            or self.balance_halalas < 0
        ):
            raise ValueError("wallet requires an id and non-negative integer balance")


@dataclass(frozen=True, slots=True)
class TransferDecision:
    sender: Wallet
    recipient: Wallet


@dataclass(frozen=True, slots=True)
class CompletedTransfer:
    """One immutable canonical record, identified by transfer rather than wallet."""

    transfer_id: str
    reference: str
    sender_wallet_id: str
    recipient_wallet_id: str
    amount_halalas: int
    completed_at: str

    def __post_init__(self) -> None:
        UUID(self.transfer_id)
        validate_amount(self.amount_halalas)
        if not self.reference or len(self.reference) > 64:
            raise ValueError("transfer reference is required")
        if (
            not self.sender_wallet_id
            or not self.recipient_wallet_id
            or self.sender_wallet_id == self.recipient_wallet_id
        ):
            raise ValueError("distinct wallet references required")
        if not self.completed_at.endswith("Z"):
            raise ValueError("UTC completion timestamp required")
        datetime.fromisoformat(self.completed_at)

    def as_item(self) -> dict[str, Any]:
        return {
            "transferId": self.transfer_id,
            "reference": self.reference,
            "senderWalletId": self.sender_wallet_id,
            "recipientWalletId": self.recipient_wallet_id,
            "amountHalalas": self.amount_halalas,
            "currency": "SAR",
            "completedAt": self.completed_at,
        }


def validate_amount(amount_halalas: int) -> int:
    """Accept only a positive integer number of halalas."""
    if (
        isinstance(amount_halalas, bool)
        or not isinstance(amount_halalas, int)
        or amount_halalas <= 0
    ):
        raise ValueError("amount must be a positive whole number of halalas")
    return amount_halalas


def _whole_int64(value: object) -> int:
    """Wire amounts are JSON integers within int64; booleans, floats and text are not."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not _INT64_MIN <= value <= _INT64_MAX
    ):
        raise ValueError("amount must be a whole number of halalas")
    return value


def _stored_int(value: object) -> int:
    """Accept a stored whole number (int or integral Decimal); reject anything else."""
    if isinstance(value, bool):
        raise TypeError("stored value is not a whole number")
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError("stored value is not a whole number")
        return int(value)
    if not isinstance(value, int):
        raise TypeError("stored value is not a whole number")
    return value


def _length_prefixed(*parts: str) -> bytes:
    return "".join(f"{len(part)}:{part};" for part in parts).encode()


@dataclass(frozen=True, slots=True)
class TransferRequest:
    """A schema-valid C1 transfer request with its recipient email normalized once."""

    recipient_email: str
    amount_halalas: int

    def __post_init__(self) -> None:
        try:
            normalized = normalize_email(self.recipient_email)
        except TypeError as error:
            raise ValueError("valid email is required") from error
        _whole_int64(self.amount_halalas)
        object.__setattr__(self, "recipient_email", normalized)

    def fingerprint(self, sender_user_id: str) -> str:
        """One-way digest of contract major, sender scope, normalized recipient and amount."""
        if not sender_user_id:
            raise ValueError("sender is required")
        material = _length_prefixed(
            _FINGERPRINT_DOMAIN,
            CONTRACT_MAJOR_VERSION,
            sender_user_id,
            self.recipient_email,
            str(self.amount_halalas),
        )
        return hashlib.sha256(material).hexdigest()


def business_refusal(
    request: TransferRequest, *, recipient_exists: bool, own_wallet: bool, balance: int
) -> str | None:
    """Return the first C1 business refusal in contract order, or None when allowed."""
    if not recipient_exists:
        return "RECIPIENT_NOT_FOUND"
    if not MIN_TRANSFER_HALALAS <= request.amount_halalas <= MAX_TRANSFER_HALALAS:
        return "AMOUNT_OUT_OF_RANGE"
    if own_wallet:
        return "SELF_TRANSFER"
    if balance < request.amount_halalas:
        return "INSUFFICIENT_FUNDS"
    return None


def new_reference(choose: Callable[[str], str] = secrets.choice) -> str:
    """User-facing reference: `R-` and 12 uniform characters with no ambiguous glyphs."""
    return "R-" + "".join(choose(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))


def format_instant(moment: datetime) -> str:
    """Millisecond UTC instant with a `Z` offset; naive clocks are refused."""
    if moment.tzinfo is None:
        raise ValueError("trusted clock must be timezone-aware")
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def completion_sort_key(completed_at: str, transfer_id: str) -> str:
    """History order: completedAt then transferId, both compared descending as text."""
    return f"{completed_at}#{transfer_id}"


def plan_completion(now: datetime, transfer_id: str, prior_keys: Iterable[str]) -> tuple[str, str]:
    """Choose a completion key strictly after both wallets' last keys, or say how long to wait."""
    completed_at = format_instant(now)
    sort_key = completion_sort_key(completed_at, transfer_id)
    latest = max((key for key in prior_keys if key), default="")
    if sort_key > latest:
        return completed_at, sort_key
    prior_instant = datetime.fromisoformat(latest.split("#", 1)[0])
    current_instant = datetime.fromisoformat(completed_at)
    behind_ms = int((prior_instant - current_instant).total_seconds() * 1000)
    raise CompletionClockBehind(max(1, behind_ms + 1))


@dataclass(frozen=True, slots=True)
class TransferOutcome:
    """The immutable first business outcome for one (sender, Idempotency-Key) pair.

    It holds no personal data: the recipient is an opaque WalletDirectory profile version.
    """

    fingerprint: str
    code: str | None = None
    transfer_id: str | None = None
    reference: str | None = None
    amount_halalas: int | None = None
    balance_after_halalas: int | None = None
    completed_at: str | None = None
    recipient_profile_version_id: str | None = None

    def __post_init__(self) -> None:
        if not _FINGERPRINT_PATTERN.fullmatch(self.fingerprint):
            raise ValueError("fingerprint digest required")
        success_fields = (
            self.transfer_id,
            self.reference,
            self.amount_halalas,
            self.balance_after_halalas,
            self.completed_at,
            self.recipient_profile_version_id,
        )
        if self.code is not None:
            if self.code not in REFUSAL_STATUS or any(f is not None for f in success_fields):
                raise ValueError("refused outcome carries only its refusal code")
            return
        self._validate_completed()

    def _validate_completed(self) -> None:
        UUID(str(self.transfer_id))
        if not _REFERENCE_PATTERN.fullmatch(str(self.reference)):
            raise ValueError("transfer reference has the wrong format")
        amount = _stored_int(self.amount_halalas)
        if not MIN_TRANSFER_HALALAS <= amount <= MAX_TRANSFER_HALALAS:
            raise ValueError("completed amount outside the transfer range")
        if _stored_int(self.balance_after_halalas) < 0:
            raise ValueError("balance after transfer cannot be negative")
        completed_at = str(self.completed_at)
        if not completed_at.endswith("Z"):
            raise ValueError("UTC completion timestamp required")
        datetime.fromisoformat(completed_at)
        version = self.recipient_profile_version_id
        if not version or len(version) > 128:
            raise ValueError("recipient profile version required")

    @property
    def status(self) -> int:
        return 201 if self.code is None else REFUSAL_STATUS[self.code]

    @classmethod
    def refused(cls, fingerprint: str, code: str) -> TransferOutcome:
        return cls(fingerprint=fingerprint, code=code)

    @classmethod
    def completed(
        cls,
        *,
        fingerprint: str,
        transfer_id: str,
        reference: str,
        amount_halalas: int,
        balance_after_halalas: int,
        completed_at: str,
        recipient_profile_version_id: str,
    ) -> TransferOutcome:
        return cls(
            fingerprint=fingerprint,
            transfer_id=transfer_id,
            reference=reference,
            amount_halalas=amount_halalas,
            balance_after_halalas=balance_after_halalas,
            completed_at=completed_at,
            recipient_profile_version_id=recipient_profile_version_id,
        )

    def as_item(self) -> dict[str, Any]:
        if self.code is not None:
            return {"fingerprint": self.fingerprint, "outcome": "refused", "code": self.code}
        return {
            "fingerprint": self.fingerprint,
            "outcome": "completed",
            "transferId": self.transfer_id,
            "reference": self.reference,
            "amountHalalas": self.amount_halalas,
            "balanceAfterHalalas": self.balance_after_halalas,
            "completedAt": self.completed_at,
            "recipientProfileVersionId": self.recipient_profile_version_id,
        }

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> TransferOutcome:
        if item.get("outcome") == "refused":
            return cls.refused(str(item["fingerprint"]), str(item["code"]))
        if item.get("outcome") != "completed":
            raise ValueError("unknown stored outcome")
        return cls.completed(
            fingerprint=str(item["fingerprint"]),
            transfer_id=str(item["transferId"]),
            reference=str(item["reference"]),
            amount_halalas=_stored_int(item["amountHalalas"]),
            balance_after_halalas=_stored_int(item["balanceAfterHalalas"]),
            completed_at=str(item["completedAt"]),
            recipient_profile_version_id=str(item["recipientProfileVersionId"]),
        )


def match_existing_outcome(stored: TransferOutcome, fingerprint: str) -> TransferOutcome:
    """Replay the first outcome for an identical request; refuse any different request."""
    if stored.fingerprint != fingerprint:
        raise IdempotencyKeyReused("idempotency key already used for a different request")
    return stored


class LedgerRules:
    """Pure domain operations; persistence adapters commit results atomically."""

    @staticmethod
    def opening_wallet(wallet_id: str) -> Wallet:
        if not wallet_id:
            raise ValueError("wallet id is required")
        return Wallet(wallet_id=wallet_id, balance_halalas=OPENING_BALANCE_HALALAS)

    @staticmethod
    def transfer(sender: Wallet, recipient: Wallet, amount_halalas: int) -> TransferDecision:
        amount = validate_amount(amount_halalas)
        if sender.wallet_id == recipient.wallet_id:
            raise ValueError("wallets must be different")
        if sender.balance_halalas < amount:
            raise InsufficientFunds("insufficient funds")
        return TransferDecision(
            sender=Wallet(sender.wallet_id, sender.balance_halalas - amount),
            recipient=Wallet(recipient.wallet_id, recipient.balance_halalas + amount),
        )
