"""DynamoDB Local transaction/concurrency checks (real container, no mock)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest

from raad_money.history import HistoryReader, TransactionsIndex
from raad_money.ledger import TransferOutcome, TransferRequest
from raad_money.storage import HistoryIntegrityError, LedgerRepository, WalletDirectory
from raad_money.store_errors import OutcomeAlreadyRecorded, ReferenceCollision, WalletChanged
from raad_money.transfers import TransferCommit, TransferService, WalletState
from raad_money.wallet_setup import ensure_wallet_and_holder


@pytest.fixture
def stores(ledger_table: str, directory_table: str) -> tuple[LedgerRepository, WalletDirectory]:
    ledger, directory = LedgerRepository(ledger_table), WalletDirectory(directory_table)
    for name in ("sender", "recipient"):
        ensure_wallet_and_holder(ledger, directory, name, f"{name}@example.test", name.title())
    return ledger, directory


def send(stores, amount: int, key: str, sender: str = "sender", to: str = "recipient") -> Any:
    ledger, directory = stores
    return TransferService(ledger, directory).submit(
        sender, key, TransferRequest(f"{to}@example.test", amount)
    )


def state(ledger: LedgerRepository, owner: str) -> WalletState:
    record = ledger.get_wallet(owner)
    assert record is not None
    return WalletState.from_record(record)


def snapshot(ledger: LedgerRepository) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        sorted(ledger.table.scan(ConsistentRead=True)["Items"], key=lambda i: (i["pk"], i["sk"])),
        ledger.transactions.scan(ConsistentRead=True)["Items"],
    )


def planned_commit(
    ledger: LedgerRepository, key: str, reference: str = "R-23456789ABCD"
) -> TransferCommit:
    sender, recipient = state(ledger, "sender"), state(ledger, "recipient")
    stamp = "2030-01-01T00:00:00.000Z"
    transfer_id = "2bc440be-a85a-4cf8-9bf1-128fc7214c60"
    return TransferCommit(
        owner_id="sender",
        idempotency_key=key,
        sender=sender,
        recipient=recipient,
        sender_balance_after=sender.balance_halalas - 100,
        recipient_balance_after=recipient.balance_halalas + 100,
        sort_key=f"{stamp}#{transfer_id}",
        outcome=TransferOutcome.completed(
            fingerprint="a" * 64,
            transfer_id=transfer_id,
            reference=reference,
            amount_halalas=100,
            balance_after_halalas=sender.balance_halalas - 100,
            completed_at=stamp,
            recipient_profile_version_id="version-recipient",
        ),
    )


def test_repeated_wallet_pair_has_distinct_canonical_rows_and_only_references(stores) -> None:
    ledger, _ = stores
    first = send(stores, 100, "key-repeat-000001")
    second = send(stores, 100, "key-repeat-000002")
    assert first.outcome.transfer_id != second.outcome.transfer_id
    rows = ledger.transactions.scan()["Items"]
    assert {r["transferId"] for r in rows} == {
        first.outcome.transfer_id,
        second.outcome.transfer_id,
    }
    refs = [r for r in ledger.table.scan()["Items"] if r["sk"].startswith("TRANSFER#")]
    assert len(refs) == 4 and all(set(r) == {"pk", "sk", "transferId"} for r in refs)


def test_stale_wallet_condition_aborts_all_seven_actions(stores) -> None:
    ledger, _ = stores
    before = snapshot(ledger)
    stale = planned_commit(ledger, "key-stale-0000001")
    stale = replace(
        stale,
        recipient=replace(stale.recipient, balance_halalas=stale.recipient.balance_halalas + 1),
    )
    with pytest.raises(WalletChanged):
        ledger.commit_transfer(stale)
    assert snapshot(ledger) == before


def test_taken_reference_aborts_everything_and_is_reported(stores) -> None:
    ledger, _ = stores
    ledger.table.put_item(
        Item={"pk": "REFERENCE#R-23456789ABCD", "sk": "REFERENCE", "transferId": "x"}
    )
    before = snapshot(ledger)
    with pytest.raises(ReferenceCollision):
        ledger.commit_transfer(planned_commit(ledger, "key-reftaken-00001"))
    assert snapshot(ledger) == before


def test_existing_outcome_aborts_everything_and_keeps_first(stores) -> None:
    ledger, _ = stores
    first = send(stores, 300, "key-existing-00001")
    before = snapshot(ledger)
    with pytest.raises(OutcomeAlreadyRecorded):
        ledger.commit_transfer(planned_commit(ledger, "key-existing-00001", "R-33333333333X"))
    assert snapshot(ledger) == before
    assert ledger.get_outcome("sender", "key-existing-00001") == first.outcome


def test_missing_canonical_row_blocks_history_instead_of_omitting(stores) -> None:
    ledger, _ = stores
    result = send(stores, 100, "key-missing-00001")
    ledger.transactions.delete_item(Key={"transferId": result.outcome.transfer_id})
    sender = ledger.get_wallet("sender")
    assert sender is not None
    with pytest.raises(HistoryIntegrityError):
        ledger.history(sender["wallet_id"])
    with pytest.raises(HistoryIntegrityError):
        HistoryReader(TransactionsIndex(ledger.transactions), sleep=lambda _: None).page(
            sender, 20, None, 1_800_000_000
        )


def test_opening_wallet_race_creates_one_wallet(ledger_table: str) -> None:
    repo = LedgerRepository(ledger_table)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: repo.ensure_wallet("holder-race"), range(8)))
    assert len({wallet["wallet_id"] for wallet, _ in outcomes}) == 1
    assert sum(created for _, created in outcomes) == 1
    wallet = repo.get_wallet("holder-race")
    assert wallet is not None and wallet["transfer_sequence"] == 0


def test_transfer_transaction_updates_both_wallets_and_history(stores) -> None:
    ledger, _ = stores
    send(stores, 100, "key-update-000001")
    assert ledger.get_wallet("sender")["balance_halalas"] == 99_900
    assert ledger.get_wallet("recipient")["balance_halalas"] == 100_100
    for owner in ("sender", "recipient"):
        wallet = ledger.get_wallet(owner)
        assert len(ledger.history(wallet["wallet_id"])) == 1


def test_concurrent_debits_cannot_overdraw(stores) -> None:
    ledger, _ = stores
    send(stores, 40_000, "key-predrain-0001")

    def debit(index: int) -> str | None:
        return send(stores, 50_000, f"key-overdraw-{index:04d}").outcome.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(debit, range(2)))
    assert sorted(codes, key=str) == ["INSUFFICIENT_FUNDS", None]
    assert ledger.get_wallet("sender")["balance_halalas"] == 10_000
