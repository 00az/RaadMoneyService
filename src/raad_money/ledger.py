"""Pure integer-halalas rules for wallets and atomic transfer decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

OPENING_BALANCE_HALALAS = 100_000


class InsufficientFunds(ValueError):
    """Raised when a transfer would make the sender balance negative."""


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
