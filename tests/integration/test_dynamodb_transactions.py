"""DynamoDB Local transaction/concurrency checks (real container, no mock)."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from raad_money.storage import LedgerRepository


def test_repeated_wallet_pair_has_distinct_canonical_rows_and_only_references(
    ledger_table: str,
) -> None:
    repo = LedgerRepository(ledger_table)
    sender, _ = repo.ensure_wallet("sender")
    recipient, _ = repo.ensure_wallet("recipient")
    first = repo.transfer(sender, recipient, 10)
    second = repo.transfer(repo.get_wallet("sender"), repo.get_wallet("recipient"), 10)
    assert first["transfer_id"] != second["transfer_id"]
    rows = repo.transactions.scan()["Items"]
    assert len(rows) == 2
    assert {r["transferId"] for r in rows} == {first["transfer_id"], second["transfer_id"]}
    refs = [r for r in repo.table.scan()["Items"] if r["sk"].startswith("TRANSFER#")]
    assert len(refs) == 4 and all(set(r) == {"pk", "sk", "transferId"} for r in refs)


def test_stale_recipient_condition_aborts_all_five_actions(ledger_table: str) -> None:
    repo = LedgerRepository(ledger_table)
    sender, _ = repo.ensure_wallet("sender")
    recipient, _ = repo.ensure_wallet("recipient")
    recipient["balance_halalas"] += 1
    with pytest.raises(repo.table.meta.client.exceptions.TransactionCanceledException):
        repo.transfer(sender, recipient, 10)
    assert repo.get_wallet("sender")["balance_halalas"] == 100_000
    assert repo.get_wallet("recipient")["balance_halalas"] == 100_000
    assert repo.transactions.scan()["Items"] == []
    assert repo.history(sender["wallet_id"]) == []


def test_missing_canonical_row_blocks_history_instead_of_omitting(ledger_table: str) -> None:
    from raad_money.storage import HistoryIntegrityError

    repo = LedgerRepository(ledger_table)
    sender, _ = repo.ensure_wallet("sender")
    recipient, _ = repo.ensure_wallet("recipient")
    transfer = repo.transfer(sender, recipient, 10)
    repo.transactions.delete_item(Key={"transferId": transfer["transfer_id"]})
    with pytest.raises(HistoryIntegrityError):
        repo.history(sender["wallet_id"])


def test_opening_wallet_race_creates_one_wallet(ledger_table: str) -> None:
    repo = LedgerRepository(ledger_table)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: repo.ensure_wallet("holder-race"), range(8)))
    assert len({wallet["wallet_id"] for wallet, _ in outcomes}) == 1
    assert sum(created for _, created in outcomes) == 1


def test_transfer_transaction_updates_both_wallets_and_history(ledger_table: str) -> None:
    repo = LedgerRepository(ledger_table)
    sender, _ = repo.ensure_wallet("sender")
    recipient, _ = repo.ensure_wallet("recipient")
    repo.transfer(sender, recipient, 100)
    assert repo.get_wallet("sender")["balance_halalas"] == 99_900
    assert repo.get_wallet("recipient")["balance_halalas"] == 100_100
    assert len(repo.history(sender["wallet_id"])) == 1
    assert len(repo.history(recipient["wallet_id"])) == 1


def test_concurrent_debits_cannot_overdraw(ledger_table: str) -> None:
    repo = LedgerRepository(ledger_table)
    repo.ensure_wallet("sender")
    repo.ensure_wallet("recipient")

    def transfer(_: int) -> bool:
        try:
            current_sender = repo.get_wallet("sender")
            current_recipient = repo.get_wallet("recipient")
            repo.transfer(current_sender, current_recipient, 60_000)
            return True
        except (ValueError, repo.table.meta.client.exceptions.TransactionCanceledException):
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(transfer, range(2)))
    assert sum(results) == 1
    assert repo.get_wallet("sender")["balance_halalas"] >= 0
