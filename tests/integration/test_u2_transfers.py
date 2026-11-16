"""U2 test-first keyed transfers against a real DynamoDB Local container (no mocks)."""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from boto3.dynamodb.conditions import Key

from raad_money.ledger import IdempotencyKeyReused, TransferRequest
from raad_money.storage import LedgerRepository, WalletDirectory
from raad_money.store_errors import CommitUncertain, TransientStoreError
from raad_money.transfers import TransferCommit, TransferService


@pytest.fixture
def stores(ledger_table: str, directory_table: str) -> tuple[LedgerRepository, WalletDirectory]:
    return LedgerRepository(ledger_table), WalletDirectory(directory_table)


def person(stores: tuple[LedgerRepository, WalletDirectory], name: str) -> str:
    ledger, directory = stores
    ledger.ensure_wallet(name)
    directory.ensure_holder(name, f"{name}@example.test", name.title())
    return name


def balance(ledger: LedgerRepository, owner: str) -> int:
    wallet = ledger.get_wallet(owner)
    assert wallet is not None
    return int(wallet["balance_halalas"])


def canonical_rows(ledger: LedgerRepository) -> list[dict[str, Any]]:
    return list(ledger.transactions.scan(ConsistentRead=True)["Items"])


def test_sequential_replay_returns_first_result_and_moves_money_once(stores) -> None:
    ledger, directory = stores
    person(stores, "alice"), person(stores, "bob")
    service = TransferService(ledger, directory)
    first = service.submit("alice", "key-sequential-01", TransferRequest("bob@example.test", 2_500))
    again = service.submit(
        "alice", "key-sequential-01", TransferRequest(" BOB@example.test ", 2_500)
    )
    assert (first.replayed, again.replayed) == (False, True)
    assert again.outcome == first.outcome
    assert (balance(ledger, "alice"), balance(ledger, "bob")) == (97_500, 102_500)
    assert len(canonical_rows(ledger)) == 1


def test_different_request_with_same_key_is_refused_without_movement(stores) -> None:
    ledger, directory = stores
    person(stores, "alice"), person(stores, "bob")
    service = TransferService(ledger, directory)
    service.submit("alice", "key-conflict-0001", TransferRequest("bob@example.test", 2_500))
    with pytest.raises(IdempotencyKeyReused):
        service.submit("alice", "key-conflict-0001", TransferRequest("bob@example.test", 2_501))
    assert balance(ledger, "alice") == 97_500
    assert len(canonical_rows(ledger)) == 1


def test_simultaneous_duplicates_converge_on_one_movement(stores) -> None:
    ledger, directory = stores
    person(stores, "alice"), person(stores, "bob")
    service = TransferService(ledger, directory)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: service.submit(
                    "alice", "key-duplicate-0001", TransferRequest("bob@example.test", 4_000)
                ),
                range(6),
            )
        )
    assert len({result.outcome.transfer_id for result in results}) == 1
    assert sum(not result.replayed for result in results) == 1
    assert (balance(ledger, "alice"), balance(ledger, "bob")) == (96_000, 104_000)
    assert len(canonical_rows(ledger)) == 1


def test_simultaneous_different_requests_on_one_key_give_one_outcome_and_one_reuse(stores) -> None:
    ledger, directory = stores
    person(stores, "alice"), person(stores, "bob")
    service = TransferService(ledger, directory)

    def send(amount: int) -> str:
        try:
            service.submit(
                "alice", "key-race-00000001", TransferRequest("bob@example.test", amount)
            )
            return "outcome"
        except IdempotencyKeyReused:
            return "reused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(send, (1_000, 2_000)))
    assert results == ["outcome", "reused"]
    assert len(canonical_rows(ledger)) == 1


def test_two_simultaneous_withdrawals_cannot_overdraw(stores) -> None:
    """AC3.7.1: 500 SAR balance, two simultaneous 300 SAR sends; one succeeds, one is refused."""
    ledger, directory = stores
    for name in ("alice", "bob", "carol", "dave"):
        person(stores, name)
    service = TransferService(ledger, directory)
    service.submit("alice", "key-drain-0000001", TransferRequest("dave@example.test", 50_000))
    assert balance(ledger, "alice") == 50_000

    def send(target: str) -> str | None:
        return service.submit(
            "alice", f"key-race-{target}-000001", TransferRequest(f"{target}@example.test", 30_000)
        ).outcome.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(send, ("bob", "carol")))
    assert sorted(codes, key=str) == ["INSUFFICIENT_FUNDS", None]
    assert balance(ledger, "alice") == 20_000


def test_many_simultaneous_transfers_conserve_money_and_never_go_negative(stores) -> None:
    """AC3.7.2 against the real store: total unchanged, no negative balance, one row per success."""
    ledger, directory = stores
    names = ["alice", "bob", "carol"]
    for name in names:
        person(stores, name)
    service = TransferService(ledger, directory)
    generator = random.Random(20261116)
    plan = [
        (generator.choice(names), generator.choice(names), generator.randint(100, 50_000))
        for _ in range(30)
    ]

    def send(index: int) -> int | str | None:
        sender, target, amount = plan[index]
        try:
            result = service.submit(
                sender, f"key-many-{index:08d}", TransferRequest(f"{target}@example.test", amount)
            )
        except TransientStoreError:
            # Bounded contention budget exhausted: safe 503, nothing may be persisted.
            assert ledger.get_outcome(sender, f"key-many-{index:08d}") is None
            return "transient"
        return result.outcome.amount_halalas

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(send, range(len(plan))))
    for index, result in enumerate(results):
        if result == "transient":
            # The person's retry with the same key settles exactly once.
            results[index] = send(index)
    assert "transient" not in results
    moved = [amount for amount in results if amount is not None]
    balances = [balance(ledger, name) for name in names]
    assert sum(balances) == 300_000
    assert min(balances) >= 0
    assert len(canonical_rows(ledger)) == len(moved)


def test_lost_response_after_commit_is_recovered_by_strong_reread(stores) -> None:
    ledger, directory = stores
    person(stores, "alice"), person(stores, "bob")
    real_commit = ledger.commit_transfer

    def commit_then_lose(commit: TransferCommit) -> None:
        real_commit(commit)
        raise CommitUncertain

    ledger.commit_transfer = commit_then_lose  # type: ignore[method-assign]
    service = TransferService(ledger, directory)
    first = service.submit("alice", "key-lostresp-0001", TransferRequest("bob@example.test", 700))
    ledger.commit_transfer = real_commit  # type: ignore[method-assign]
    retry = service.submit("alice", "key-lostresp-0001", TransferRequest("bob@example.test", 700))
    assert not first.replayed and retry.replayed
    assert retry.outcome == first.outcome
    assert balance(ledger, "alice") == 99_300
    assert len(canonical_rows(ledger)) == 1


def test_process_failure_before_commit_leaves_no_trace_and_retry_moves_once(stores) -> None:
    ledger, directory = stores
    person(stores, "alice"), person(stores, "bob")
    real_version = directory.ensure_profile_version

    def crash(_: str) -> dict[str, Any]:
        raise RuntimeError("worker killed before commit")

    directory.ensure_profile_version = crash  # type: ignore[method-assign]
    service = TransferService(ledger, directory)
    with pytest.raises(RuntimeError):
        service.submit("alice", "key-crash-0000001", TransferRequest("bob@example.test", 900))
    assert ledger.get_outcome("alice", "key-crash-0000001") is None
    assert balance(ledger, "alice") == 100_000
    directory.ensure_profile_version = real_version  # type: ignore[method-assign]
    result = service.submit("alice", "key-crash-0000001", TransferRequest("bob@example.test", 900))
    assert not result.replayed
    assert balance(ledger, "alice") == 99_100
    assert len(canonical_rows(ledger)) == 1


def test_persisted_unknown_recipient_outlives_later_registration(stores) -> None:
    ledger, directory = stores
    person(stores, "alice")
    service = TransferService(ledger, directory)
    first = service.submit("alice", "key-unknown-00001", TransferRequest("erin@example.test", 500))
    person(stores, "erin")
    again = service.submit("alice", "key-unknown-00001", TransferRequest("erin@example.test", 500))
    assert first.outcome.code == again.outcome.code == "RECIPIENT_NOT_FOUND"
    assert again.replayed
    fresh = service.submit("alice", "key-unknown-00002", TransferRequest("erin@example.test", 500))
    assert fresh.outcome.status == 201


def test_reference_collision_is_regenerated_before_commit(stores) -> None:
    ledger, directory = stores
    person(stores, "alice"), person(stores, "bob")
    taken = iter(["R-222222222222", "R-222222222222", "R-333333333333"])
    service = TransferService(ledger, directory, new_reference=lambda: next(taken))
    one = service.submit("alice", "key-reference-001", TransferRequest("bob@example.test", 100))
    two = service.submit("alice", "key-reference-002", TransferRequest("bob@example.test", 100))
    assert (one.outcome.reference, two.outcome.reference) == ("R-222222222222", "R-333333333333")
    assert len(canonical_rows(ledger)) == 2


def test_commit_assigns_contiguous_wallet_ordinals_and_keeps_u1_reference_rows(stores) -> None:
    ledger, directory = stores
    person(stores, "alice"), person(stores, "bob")
    service = TransferService(ledger, directory)
    for index in range(3):
        service.submit(
            "alice", f"key-ordinal-{index:05d}", TransferRequest("bob@example.test", 100)
        )
    service.submit("bob", "key-ordinal-back1", TransferRequest("alice@example.test", 100))
    alice, bob = ledger.get_wallet("alice"), ledger.get_wallet("bob")
    assert alice is not None and bob is not None
    assert (alice["transfer_sequence"], bob["transfer_sequence"]) == (4, 4)
    rows = sorted(canonical_rows(ledger), key=lambda row: row["completedSortKey"])
    ordinals = [
        int(
            row["senderOrdinal"]
            if row["senderWalletId"] == alice["wallet_id"]
            else row["recipientOrdinal"]
        )
        for row in rows
    ]
    assert ordinals == [1, 2, 3, 4]
    assert alice["last_completed_sort_key"] == rows[-1]["completedSortKey"]
    refs = ledger.table.query(
        KeyConditionExpression=Key("pk").eq(f"WALLET#{alice['wallet_id']}")
        & Key("sk").begins_with("TRANSFER#")
    )["Items"]
    assert len(refs) == 4


def test_ledger_outcomes_hold_no_personal_data(stores) -> None:
    ledger, directory = stores
    person(stores, "alice"), person(stores, "bob")
    service = TransferService(ledger, directory)
    service.submit("alice", "key-privacy-00001", TransferRequest("bob@example.test", 300))
    service.submit("alice", "key-privacy-00002", TransferRequest("zed@example.test", 300))
    items = ledger.table.scan(ConsistentRead=True)["Items"] + canonical_rows(ledger)
    rendered = repr(items)
    assert "@" not in rendered and "Bob" not in rendered
