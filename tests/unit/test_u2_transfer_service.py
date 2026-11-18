"""U2 test-first duplicate-request handling: replay, key reuse, refusals and lost responses."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from raad_money.ledger import IdempotencyKeyReused, TransferOutcome, TransferRequest
from raad_money.storage import RecipientMissing, WalletMissing
from raad_money.store_errors import (
    CommitUncertain,
    OutcomeAlreadyRecorded,
    ReferenceCollision,
    TransientStoreError,
    WalletChanged,
)
from raad_money.transfers import TransferCommit, TransferService

KEY = "request-key-0001"
START = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


class FakeLedger:
    """In-memory port double; real atomicity and concurrency are proven on DynamoDB Local."""

    def __init__(self) -> None:
        self.wallets: dict[str, dict[str, Any]] = {}
        self.outcomes: dict[tuple[str, str], TransferOutcome] = {}
        self.commits: list[TransferCommit] = []
        self.refusal_guards: list[Any] = []
        self.commit_failures: list[Exception] = []
        self.lose_commit_response = False

    def open(self, owner_id: str, balance: int = 100_000, **extra: Any) -> None:
        self.wallets[owner_id] = {
            "wallet_id": f"wallet-{owner_id}",
            "owner_id": owner_id,
            "balance_halalas": balance,
            "transfer_sequence": 0,
            **extra,
        }

    def get_wallet(self, owner_id: str) -> dict[str, Any] | None:
        wallet = self.wallets.get(owner_id)
        return dict(wallet) if wallet else None

    def get_outcome(self, owner_id: str, key: str) -> TransferOutcome | None:
        return self.outcomes.get((owner_id, key))

    def record_refusal(
        self, owner_id: str, key: str, outcome: TransferOutcome, *, balance_guard: Any
    ) -> None:
        self.refusal_guards.append(balance_guard)
        if (owner_id, key) in self.outcomes:
            raise OutcomeAlreadyRecorded
        self.outcomes[(owner_id, key)] = outcome

    def commit_transfer(self, commit: TransferCommit) -> None:
        if self.commit_failures:
            raise self.commit_failures.pop(0)
        if (commit.owner_id, commit.idempotency_key) in self.outcomes:
            raise OutcomeAlreadyRecorded
        for state, balance in (
            (commit.sender, commit.sender_balance_after),
            (commit.recipient, commit.recipient_balance_after),
        ):
            stored = self.wallets[state.owner_id]
            if (
                stored["balance_halalas"] != state.balance_halalas
                or stored["transfer_sequence"] != state.transfer_sequence
            ):
                raise WalletChanged
            stored.update(
                balance_halalas=balance,
                transfer_sequence=state.transfer_sequence + 1,
                last_completed_sort_key=commit.sort_key,
            )
        self.outcomes[(commit.owner_id, commit.idempotency_key)] = commit.outcome
        self.commits.append(commit)
        if self.lose_commit_response:
            self.lose_commit_response = False
            raise CommitUncertain


class FakeDirectory:
    def __init__(self) -> None:
        self.holders: dict[str, str] = {}
        self.version_requests: list[str] = []
        self.version_failures: list[Exception] = []

    def add(self, email: str, holder_id: str) -> None:
        self.holders[email] = holder_id

    def find_by_email(self, email: str) -> dict[str, Any]:
        if email not in self.holders:
            raise RecipientMissing
        return {"holder_id": self.holders[email]}

    def ensure_profile_version(self, holder_id: str) -> dict[str, Any]:
        self.version_requests.append(holder_id)
        if self.version_failures:
            raise self.version_failures.pop(0)
        return {"profile_version_id": f"version-{holder_id}"}


class Clock:
    def __init__(self) -> None:
        self.now = START
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def build(
    references: list[str] | None = None,
) -> tuple[TransferService, FakeLedger, FakeDirectory, Clock]:
    ledger, directory, clock = FakeLedger(), FakeDirectory(), Clock()
    ledger.open("alice")
    ledger.open("bob", balance=0)
    directory.add("alice@example.test", "alice")
    directory.add("bob@example.test", "bob")
    ids = (f"00000000-0000-4000-8000-{index:012d}" for index in itertools.count(1))
    refs = iter(references or [f"R-{'ABCDEFGHJKMN'[:11]}{c}" for c in "23456789AB"])
    service = TransferService(
        ledger,
        directory,
        clock=clock,
        sleep=clock.sleep,
        new_transfer_id=lambda: next(ids),
        new_reference=lambda: next(refs),
    )
    return service, ledger, directory, clock


def request(amount: int = 2_500, email: str = "bob@example.test") -> TransferRequest:
    return TransferRequest(email, amount)


def test_first_request_commits_once_and_identical_retry_replays_it() -> None:
    service, ledger, _, _ = build()
    first = service.submit("alice", KEY, request(email=" Bob@Example.TEST "))
    again = service.submit("alice", KEY, request())
    assert (first.replayed, again.replayed) == (False, True)
    assert again.outcome == first.outcome and first.outcome.status == 201
    assert len(ledger.commits) == 1
    assert ledger.wallets["alice"]["balance_halalas"] == 97_500
    assert ledger.wallets["bob"]["balance_halalas"] == 2_500


def test_changed_request_with_same_key_is_key_reuse_and_keeps_first_outcome() -> None:
    service, ledger, _, _ = build()
    first = service.submit("alice", KEY, request(2_500))
    with pytest.raises(IdempotencyKeyReused):
        service.submit("alice", KEY, request(2_600))
    assert ledger.outcomes[("alice", KEY)] == first.outcome
    assert len(ledger.commits) == 1


def test_persisted_low_balance_refusal_replays_even_after_money_arrives() -> None:
    service, ledger, _, _ = build()
    first = service.submit("bob", KEY, request(email="alice@example.test"))
    assert first.outcome.code == "INSUFFICIENT_FUNDS" and not first.replayed
    ledger.wallets["bob"]["balance_halalas"] = 50_000
    again = service.submit("bob", KEY, request(email="alice@example.test"))
    assert again.outcome.code == "INSUFFICIENT_FUNDS" and again.replayed
    assert ledger.commits == []


def test_low_balance_refusal_is_guarded_by_the_observed_wallet_state() -> None:
    service, ledger, _, _ = build()
    service.submit("bob", KEY, request(email="alice@example.test"))
    service.submit("alice", "other-key-000001", request(email="nobody@example.test"))
    guard, unguarded = ledger.refusal_guards
    assert guard.owner_id == "bob" and guard.balance_halalas == 0
    assert unguarded is None


def test_unknown_recipient_is_first_refusal_and_creates_no_profile_version() -> None:
    service, ledger, directory, _ = build()
    result = service.submit("alice", KEY, request(10, email="nobody@example.test"))
    assert result.outcome == TransferOutcome.refused(
        request(10, "nobody@example.test").fingerprint("alice"), "RECIPIENT_NOT_FOUND"
    )
    assert directory.version_requests == [] and ledger.commits == []


def test_recipient_without_wallet_is_not_a_transfer_target() -> None:
    service, ledger, directory, _ = build()
    directory.add("ghost@example.test", "ghost")
    result = service.submit("alice", KEY, request(email="ghost@example.test"))
    assert result.outcome.code == "RECIPIENT_NOT_FOUND"
    assert ledger.commits == []


def test_self_transfer_is_refused_after_range_and_persisted() -> None:
    service, ledger, _, _ = build()
    result = service.submit("alice", KEY, request(email="alice@example.test"))
    assert result.outcome.code == "SELF_TRANSFER"
    assert ledger.outcomes[("alice", KEY)].code == "SELF_TRANSFER"


def test_missing_sender_wallet_consumes_no_key() -> None:
    service, ledger, _, _ = build()
    with pytest.raises(WalletMissing):
        service.submit("carol", KEY, request())
    assert ledger.outcomes == {}
    ledger.open("carol")
    assert service.submit("carol", KEY, request()).outcome.status == 201


def test_concurrent_winner_is_replayed_to_the_loser() -> None:
    service, ledger, _, _ = build()
    winner = TransferOutcome.refused(request().fingerprint("alice"), "INSUFFICIENT_FUNDS")

    def lose(_: TransferCommit) -> None:
        ledger.outcomes[("alice", KEY)] = winner
        raise OutcomeAlreadyRecorded

    ledger.commit_transfer = lose  # type: ignore[method-assign]
    result = service.submit("alice", KEY, request())
    assert result.outcome is winner and result.replayed


def test_lost_commit_response_returns_own_saved_outcome_once() -> None:
    service, ledger, _, _ = build()
    ledger.lose_commit_response = True
    result = service.submit("alice", KEY, request())
    assert not result.replayed and result.outcome.status == 201
    assert len(ledger.commits) == 1
    assert service.submit("alice", KEY, request()).replayed


def test_unconfirmed_commit_is_transient_and_retry_moves_money_once() -> None:
    service, ledger, _, _ = build()
    ledger.commit_failures.append(CommitUncertain())
    with pytest.raises(TransientStoreError):
        service.submit("alice", KEY, request())
    assert ledger.outcomes == {}
    result = service.submit("alice", KEY, request())
    assert not result.replayed and len(ledger.commits) == 1


def test_lost_response_after_concurrent_identical_winner_replays_winner() -> None:
    service, ledger, _, _ = build()
    winner = TransferOutcome.refused(request().fingerprint("alice"), "INSUFFICIENT_FUNDS")

    def uncertain(_: TransferCommit) -> None:
        ledger.outcomes[("alice", KEY)] = winner
        raise CommitUncertain

    ledger.commit_transfer = uncertain  # type: ignore[method-assign]
    result = service.submit("alice", KEY, request())
    assert result.outcome is winner and result.replayed


def test_process_failure_before_commit_leaves_no_key_and_retry_succeeds() -> None:
    service, ledger, directory, _ = build()
    directory.version_failures.append(TimeoutError("directory offline"))
    with pytest.raises(TimeoutError):
        service.submit("alice", KEY, request())
    assert ledger.outcomes == {} and ledger.wallets["alice"]["balance_halalas"] == 100_000
    assert service.submit("alice", KEY, request()).outcome.status == 201
    assert len(ledger.commits) == 1


def test_stale_balance_is_re_evaluated_before_choosing_an_outcome() -> None:
    service, ledger, _, _ = build()
    ledger.commit_failures.append(WalletChanged())
    original_get = ledger.get_wallet
    reads = {"count": 0}

    def drained(owner_id: str) -> dict[str, Any] | None:
        wallet = original_get(owner_id)
        if owner_id == "alice" and wallet:
            reads["count"] += 1
            if reads["count"] > 1:
                ledger.wallets["alice"]["balance_halalas"] = 1_000
                wallet["balance_halalas"] = 1_000
        return wallet

    ledger.get_wallet = drained  # type: ignore[method-assign]
    result = service.submit("alice", KEY, request())
    assert result.outcome.code == "INSUFFICIENT_FUNDS"
    assert ledger.commits == []


def test_reference_collision_retries_with_a_new_reference() -> None:
    service, ledger, _, _ = build(["R-222222222222", "R-333333333333"])
    ledger.commit_failures.append(ReferenceCollision())
    result = service.submit("alice", KEY, request())
    assert result.outcome.reference == "R-333333333333"


def test_attempt_budget_is_bounded() -> None:
    service, ledger, _, clock = build()
    ledger.commit_failures.extend(WalletChanged() for _ in range(10))
    with pytest.raises(TransientStoreError):
        service.submit("alice", KEY, request())
    assert ledger.outcomes == {}
    assert 0 < len(clock.sleeps) <= 4


def test_completion_waits_briefly_for_a_wallet_clock_ahead() -> None:
    service, ledger, _, clock = build()
    ahead = "2026-11-16T09:00:00.040Z#ffffffff-ffff-4fff-bfff-ffffffffffff"
    ledger.wallets["bob"]["last_completed_sort_key"] = ahead
    result = service.submit("alice", KEY, request())
    assert result.outcome.completed_at is not None
    assert ledger.commits[0].sort_key > ahead
    assert clock.sleeps and clock.sleeps[0] >= 0.04


def test_completion_never_waits_past_the_budget() -> None:
    service, ledger, _, _ = build()
    far = "2026-11-16T09:00:05.000Z#ffffffff-ffff-4fff-bfff-ffffffffffff"
    ledger.wallets["alice"]["last_completed_sort_key"] = far
    with pytest.raises(TransientStoreError):
        service.submit("alice", KEY, request())
    assert ledger.commits == []


def test_clock_still_behind_after_one_wait_fails_without_commit() -> None:
    service, ledger, _, clock = build()
    ahead = "2026-11-16T09:00:00.040Z#ffffffff-ffff-4fff-bfff-ffffffffffff"
    ledger.wallets["bob"]["last_completed_sort_key"] = ahead
    clock.sleep = lambda seconds: clock.sleeps.append(seconds)  # type: ignore[method-assign]
    service._sleep = clock.sleep
    with pytest.raises(TransientStoreError):
        service.submit("alice", KEY, request())
    assert ledger.commits == [] and len(clock.sleeps) == 1


def test_production_defaults_issue_uuid_reference_and_trusted_utc_time() -> None:
    ledger, directory = FakeLedger(), FakeDirectory()
    ledger.open("alice")
    ledger.open("bob", balance=0)
    directory.add("bob@example.test", "bob")
    result = TransferService(ledger, directory).submit("alice", KEY, request())
    outcome = result.outcome
    assert outcome.transfer_id and len(outcome.transfer_id) == 36
    assert outcome.reference and outcome.reference.startswith("R-")
    assert outcome.completed_at and outcome.completed_at.endswith("Z")


def test_unmigrated_wallet_fails_closed_without_writing() -> None:
    service, ledger, _, _ = build()
    del ledger.wallets["alice"]["transfer_sequence"]
    with pytest.raises(TransientStoreError):
        service.submit("alice", KEY, request())
    assert ledger.outcomes == {}


def test_commit_carries_ordinals_balances_and_profile_version() -> None:
    service, ledger, _, _ = build()
    ledger.wallets["bob"]["transfer_sequence"] = 4
    service.submit("alice", KEY, request(5_000))
    commit = ledger.commits[0]
    assert (commit.sender.transfer_sequence, commit.recipient.transfer_sequence) == (0, 4)
    assert (commit.sender_balance_after, commit.recipient_balance_after) == (95_000, 5_000)
    assert commit.outcome.recipient_profile_version_id == "version-bob"
    assert commit.outcome.balance_after_halalas == 95_000
    assert replace(commit.outcome) == ledger.outcomes[("alice", KEY)]


def test_refusal_write_conflict_rereads_and_replays_winner() -> None:
    service, ledger, _, _ = build()
    winner = TransferOutcome.refused(
        request(email="nobody@example.test").fingerprint("alice"), "RECIPIENT_NOT_FOUND"
    )

    def lose(owner_id: str, key: str, outcome: TransferOutcome, *, balance_guard: Any) -> None:
        ledger.outcomes[(owner_id, key)] = winner
        raise OutcomeAlreadyRecorded

    ledger.record_refusal = lose  # type: ignore[method-assign]
    result = service.submit("alice", KEY, request(email="nobody@example.test"))
    assert result.outcome is winner and result.replayed


def test_unconfirmed_refusal_write_is_resolved_by_reread() -> None:
    service, ledger, _, _ = build()
    calls = {"count": 0}

    def uncertain(owner_id: str, key: str, outcome: TransferOutcome, *, balance_guard: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            ledger.outcomes[(owner_id, key)] = outcome
        raise CommitUncertain

    ledger.record_refusal = uncertain  # type: ignore[method-assign]
    result = service.submit("alice", KEY, request(email="nobody@example.test"))
    assert result.outcome.code == "RECIPIENT_NOT_FOUND" and not result.replayed
    del ledger.outcomes[("alice", KEY)]
    with pytest.raises(TransientStoreError):
        service.submit("alice", KEY, request(email="nobody@example.test"))
