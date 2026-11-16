"""U2 test-first stable history on DynamoDB Local role indexes (real container, no mocks)."""

from __future__ import annotations

import itertools
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest

from raad_money.history import HistoryReader, TransactionsIndex
from raad_money.ledger import TransferRequest
from raad_money.storage import LedgerRepository, WalletDirectory
from raad_money.transfers import TransferService

NOW = 1_800_000_000


@pytest.fixture
def stores(ledger_table: str, directory_table: str) -> tuple[LedgerRepository, WalletDirectory]:
    ledger, directory = LedgerRepository(ledger_table), WalletDirectory(directory_table)
    for name in ("alice", "bob", "carol"):
        ledger.ensure_wallet(name)
        directory.ensure_holder(name, f"{name}@example.test", name.title())
    return ledger, directory


def wallet(ledger: LedgerRepository, owner: str) -> dict[str, Any]:
    record = ledger.get_wallet(owner)
    assert record is not None
    return record


def walk(ledger: LedgerRepository, owner: str, limit: int, between: Any = None) -> list[str]:
    reader = HistoryReader(TransactionsIndex(ledger.transactions))
    result = reader.page(wallet(ledger, owner), limit, None, NOW)
    seen = [row["transfer_id"] for row in result.rows]
    while result.next_position is not None:
        if between:
            between()
        result = reader.page(wallet(ledger, owner), limit, result.next_position, NOW)
        seen.extend(row["transfer_id"] for row in result.rows)
    return seen


def test_pages_cover_snapshot_once_even_when_transfers_arrive_between_pages(stores) -> None:
    ledger, directory = stores
    service = TransferService(ledger, directory)
    ids: list[str] = []
    for index in range(10):
        sender, target = ("alice", "bob") if index % 3 else ("bob", "alice")
        result = service.submit(
            sender, f"key-page-{index:07d}", TransferRequest(f"{target}@example.test", 100)
        )
        ids.append(str(result.outcome.transfer_id))
    later = itertools.count()

    def arrive() -> None:
        service.submit(
            "carol", f"key-late-{next(later):07d}", TransferRequest("alice@example.test", 100)
        )

    seen = walk(ledger, "alice", 3, between=arrive)
    assert len(seen) == len(set(seen)) == 10
    assert set(seen) == set(ids)
    rows = ledger.transactions.scan(ConsistentRead=True)["Items"]
    keys = {row["transferId"]: row["completedSortKey"] for row in rows}
    assert [keys[tid] for tid in seen] == sorted((keys[tid] for tid in seen), reverse=True)


def test_equal_completion_instants_are_ordered_by_transfer_id(stores) -> None:
    ledger, directory = stores
    frozen = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    ids = (f"00000000-0000-4000-8000-{index:012d}" for index in itertools.count(1))
    service = TransferService(
        ledger, directory, clock=lambda: frozen, new_transfer_id=lambda: next(ids)
    )
    for index in range(5):
        service.submit("alice", f"key-tie-{index:08d}", TransferRequest("bob@example.test", 100))
    seen = walk(ledger, "bob", 2)
    assert seen == sorted(seen, reverse=True) and len(seen) == 5


def test_opposing_simultaneous_transfers_keep_both_histories_contiguous(stores) -> None:
    ledger, directory = stores
    service = TransferService(ledger, directory)

    def send(index: int) -> None:
        sender, target = ("alice", "bob") if index % 2 else ("bob", "alice")
        for attempt in range(5):
            try:
                service.submit(
                    sender, f"key-opp-{index:08d}", TransferRequest(f"{target}@example.test", 100)
                )
                return
            except Exception:
                if attempt == 4:
                    raise

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(send, range(16)))
    for owner in ("alice", "bob"):
        assert wallet(ledger, owner)["transfer_sequence"] == 16
        seen = walk(ledger, owner, 5)
        assert len(seen) == len(set(seen)) == 16


def test_more_than_one_thousand_entries_page_completely(stores) -> None:
    """AC4.3.1/AC4.3.2: over 1,000 entries load page by page with nothing skipped or repeated."""
    ledger, directory = stores
    service = TransferService(ledger, directory)
    for index in range(1_005):
        sender, target = ("alice", "bob") if index % 2 else ("bob", "alice")
        service.submit(
            sender, f"key-bulk-{index:08d}", TransferRequest(f"{target}@example.test", 100)
        )
    seen = walk(ledger, "alice", 100)
    assert len(seen) == len(set(seen)) == 1_005


def test_canonical_read_is_strong_and_index_queries_stay_keyed(stores) -> None:
    ledger, directory = stores
    TransferService(ledger, directory).submit(
        "alice", "key-keyed-0000001", TransferRequest("bob@example.test", 100)
    )
    index = TransactionsIndex(ledger.transactions)
    sent = index.query_role(wallet(ledger, "alice")["wallet_id"], "sender", "~", True, 5)
    assert len(sent) == 1 and set(sent[0]) >= {"transferId", "completedSortKey"}
    rows = index.get_canonical([sent[0]["transferId"]])
    assert rows[sent[0]["transferId"]]["senderOrdinal"] == 1
