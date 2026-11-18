"""Keyed transfer orchestration: replay, key reuse, ordered refusals and one atomic commit.

The service decides; the Ledger store only applies conditional writes and reports conflicts.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from raad_money.ledger import (
    CompletionClockBehind,
    LedgerRules,
    TransferOutcome,
    TransferRequest,
    Wallet,
    business_refusal,
    match_existing_outcome,
    new_reference,
    plan_completion,
)
from raad_money.storage import RecipientMissing, WalletMissing
from raad_money.store_errors import (
    CommitUncertain,
    OutcomeAlreadyRecorded,
    ReferenceCollision,
    TransientStoreError,
    WalletChanged,
)

MAX_ATTEMPTS = 4
MAX_CLOCK_WAIT_MS = 250


@dataclass(frozen=True, slots=True)
class WalletState:
    """The wallet fields a transfer is conditioned on; read strongly before every attempt."""

    wallet_id: str
    owner_id: str
    balance_halalas: int
    transfer_sequence: int
    last_completed_sort_key: str

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> WalletState:
        sequence = record.get("transfer_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            # A U1 wallet without seeded ordinals must be migrated before it can move money.
            raise TransientStoreError("wallet history ordinals are not ready")
        return cls(
            wallet_id=str(record["wallet_id"]),
            owner_id=str(record["owner_id"]),
            balance_halalas=Wallet(
                str(record["wallet_id"]), record["balance_halalas"]
            ).balance_halalas,
            transfer_sequence=sequence,
            last_completed_sort_key=str(record.get("last_completed_sort_key") or ""),
        )


@dataclass(frozen=True, slots=True)
class TransferCommit:
    """Everything the store writes in one all-or-nothing transaction."""

    owner_id: str
    idempotency_key: str
    sender: WalletState
    recipient: WalletState
    sender_balance_after: int
    recipient_balance_after: int
    sort_key: str
    outcome: TransferOutcome


@dataclass(frozen=True, slots=True)
class TransferResponse:
    outcome: TransferOutcome
    replayed: bool


class LedgerStore(Protocol):
    def get_wallet(self, owner_id: str) -> dict[str, Any] | None: ...

    def get_outcome(self, owner_id: str, idempotency_key: str) -> TransferOutcome | None: ...

    def record_refusal(
        self,
        owner_id: str,
        idempotency_key: str,
        outcome: TransferOutcome,
        *,
        balance_guard: WalletState | None,
    ) -> None: ...

    def commit_transfer(self, commit: TransferCommit) -> None: ...


class HolderDirectory(Protocol):
    def find_by_email(self, email: str) -> dict[str, Any]: ...

    def ensure_profile_version(self, holder_id: str) -> dict[str, Any]: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_transfer_id() -> str:
    return str(uuid.uuid4())


class TransferService:
    """Apply C1 transfer semantics for one verified sender and Idempotency-Key."""

    def __init__(
        self,
        ledger: LedgerStore,
        directory: HolderDirectory,
        *,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        new_transfer_id: Callable[[], str] = _new_transfer_id,
        new_reference: Callable[[], str] = new_reference,
    ) -> None:
        self._ledger = ledger
        self._directory = directory
        self._clock = clock
        self._sleep = sleep
        self._new_transfer_id = new_transfer_id
        self._new_reference = new_reference

    def submit(
        self, owner_id: str, idempotency_key: str, request: TransferRequest
    ) -> TransferResponse:
        fingerprint = request.fingerprint(owner_id)
        for attempt in range(MAX_ATTEMPTS):
            record = self._ledger.get_wallet(owner_id)
            if record is None:
                raise WalletMissing
            sender = WalletState.from_record(record)
            stored = self._ledger.get_outcome(owner_id, idempotency_key)
            if stored is not None:
                return TransferResponse(match_existing_outcome(stored, fingerprint), True)
            try:
                return self._decide(owner_id, idempotency_key, request, fingerprint, sender)
            except (OutcomeAlreadyRecorded, WalletChanged, ReferenceCollision):
                # Bounded wait so a concurrent winner can finish before the strong reread.
                self._sleep(random.uniform(0.01, 0.03) * (attempt + 1))
        raise TransientStoreError("transfer could not be settled within the attempt budget")

    def _decide(
        self,
        owner_id: str,
        idempotency_key: str,
        request: TransferRequest,
        fingerprint: str,
        sender: WalletState,
    ) -> TransferResponse:
        holder = self._find_holder(request.recipient_email)
        recipient_record = self._ledger.get_wallet(holder["holder_id"]) if holder else None
        refusal = business_refusal(
            request,
            recipient_exists=recipient_record is not None,
            own_wallet=holder is not None and holder["holder_id"] == owner_id,
            balance=sender.balance_halalas,
        )
        if refusal is not None:
            outcome = TransferOutcome.refused(fingerprint, refusal)
            guard = sender if refusal == "INSUFFICIENT_FUNDS" else None
            return self._record_refusal(owner_id, idempotency_key, outcome, guard)
        assert holder is not None and recipient_record is not None
        recipient = WalletState.from_record(recipient_record)
        version = self._directory.ensure_profile_version(str(holder["holder_id"]))
        return self._commit(
            owner_id,
            idempotency_key,
            fingerprint,
            sender,
            recipient,
            request.amount_halalas,
            str(version["profile_version_id"]),
        )

    def _find_holder(self, email: str) -> dict[str, Any] | None:
        try:
            return self._directory.find_by_email(email)
        except RecipientMissing:
            return None

    def _record_refusal(
        self,
        owner_id: str,
        idempotency_key: str,
        outcome: TransferOutcome,
        guard: WalletState | None,
    ) -> TransferResponse:
        try:
            self._ledger.record_refusal(owner_id, idempotency_key, outcome, balance_guard=guard)
        except CommitUncertain:
            stored = self._ledger.get_outcome(owner_id, idempotency_key)
            if stored is None:
                raise TransientStoreError("refusal outcome unconfirmed") from None
            return TransferResponse(
                match_existing_outcome(stored, outcome.fingerprint), stored != outcome
            )
        return TransferResponse(outcome, False)

    def _commit(
        self,
        owner_id: str,
        idempotency_key: str,
        fingerprint: str,
        sender: WalletState,
        recipient: WalletState,
        amount: int,
        profile_version_id: str,
    ) -> TransferResponse:
        decision = LedgerRules.transfer(
            Wallet(sender.wallet_id, sender.balance_halalas),
            Wallet(recipient.wallet_id, recipient.balance_halalas),
            amount,
        )
        transfer_id = self._new_transfer_id()
        completed_at, sort_key = self._plan_completion(transfer_id, sender, recipient)
        outcome = TransferOutcome.completed(
            fingerprint=fingerprint,
            transfer_id=transfer_id,
            reference=self._new_reference(),
            amount_halalas=amount,
            balance_after_halalas=decision.sender.balance_halalas,
            completed_at=completed_at,
            recipient_profile_version_id=profile_version_id,
        )
        commit = TransferCommit(
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            sender=sender,
            recipient=recipient,
            sender_balance_after=decision.sender.balance_halalas,
            recipient_balance_after=decision.recipient.balance_halalas,
            sort_key=sort_key,
            outcome=outcome,
        )
        try:
            self._ledger.commit_transfer(commit)
        except CommitUncertain:
            return self._resolve_uncertain(owner_id, idempotency_key, fingerprint, transfer_id)
        return TransferResponse(outcome, False)

    def _plan_completion(
        self, transfer_id: str, sender: WalletState, recipient: WalletState
    ) -> tuple[str, str]:
        prior = (sender.last_completed_sort_key, recipient.last_completed_sort_key)
        try:
            return plan_completion(self._clock(), transfer_id, prior)
        except CompletionClockBehind as behind:
            if behind.wait_ms > MAX_CLOCK_WAIT_MS:
                raise TransientStoreError("wallet completion clock is too far ahead") from None
            self._sleep(behind.wait_ms / 1000)
        try:
            return plan_completion(self._clock(), transfer_id, prior)
        except CompletionClockBehind:
            raise TransientStoreError("wallet completion clock is still ahead") from None

    def _resolve_uncertain(
        self, owner_id: str, idempotency_key: str, fingerprint: str, transfer_id: str
    ) -> TransferResponse:
        stored = self._ledger.get_outcome(owner_id, idempotency_key)
        if stored is None:
            raise TransientStoreError("transfer outcome unconfirmed")
        if stored.transfer_id == transfer_id:
            return TransferResponse(stored, False)
        return TransferResponse(match_existing_outcome(stored, fingerprint), True)
