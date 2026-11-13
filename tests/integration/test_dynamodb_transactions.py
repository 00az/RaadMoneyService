"""DynamoDB Local transaction/concurrency checks (real container, no mock)."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from raad_money.storage import LedgerRepository


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
    sender, _ = repo.ensure_wallet("sender")
    recipient, _ = repo.ensure_wallet("recipient")
    def transfer(_: int) -> bool:
        try:
            current_sender = repo.get_wallet("sender")
            current_recipient = repo.get_wallet("recipient")
            repo.transfer(current_sender, current_recipient, 60_000)
            return True
        except Exception:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(transfer, range(2)))
    assert sum(results) == 1
    assert repo.get_wallet("sender")["balance_halalas"] >= 0
