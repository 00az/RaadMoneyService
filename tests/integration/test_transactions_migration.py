"""Real DynamoDB Local migration preserves balances and canonical histories."""

import importlib.util
from pathlib import Path

import pytest

from raad_money.storage import LedgerRepository

spec = importlib.util.spec_from_file_location(
    "migrate_transactions", Path(__file__).parents[2] / "scripts/migrate_transactions.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_migration_is_atomic_rerunnable_and_preserves_both_histories(
    dynamodb_resource, ledger_table, transactions_table
):
    repo = LedgerRepository(
        ledger_table, resource=dynamodb_resource, transactions_table=transactions_table
    )
    sender, _ = repo.ensure_wallet("s")
    recipient, _ = repo.ensure_wallet("r")
    identity, stamp = "2bc440be-a85a-4cf8-9bf1-128fc7214c60", "2026-11-14T00:00:00.000Z"
    payload = {
        "entity": "transfer",
        "transfer_id": identity,
        "reference": "TXN-ONE",
        "sender_wallet_id": sender["wallet_id"],
        "recipient_wallet_id": recipient["wallet_id"],
        "amount_halalas": 10,
        "completed_at": stamp,
    }
    for wallet in (sender, recipient):
        repo.table.put_item(
            Item={
                "pk": f"WALLET#{wallet['wallet_id']}",
                "sk": f"TRANSFER#{stamp}#{identity}",
                **payload,
            }
        )
    before = [repo.get_wallet(owner)["balance_halalas"] for owner in ("s", "r")]
    assert migration.migrate(repo.table, repo.transactions)["transfers"] == 1
    first = migration.migrate(repo.table, repo.transactions, apply=True, writes_quiesced=True)
    second = migration.migrate(repo.table, repo.transactions, apply=True, writes_quiesced=True)
    assert first == second
    assert [repo.get_wallet(owner)["balance_halalas"] for owner in ("s", "r")] == before
    assert repo.history(sender["wallet_id"])[0]["transfer_id"] == identity
    assert repo.history(recipient["wallet_id"])[0]["transfer_id"] == identity
    rows = repo.transactions.scan()["Items"]
    assert len(rows) == 1
    refs = repo.table.query(
        KeyConditionExpression=__import__("boto3.dynamodb.conditions", fromlist=["Key"])
        .Key("pk")
        .eq(f"WALLET#{sender['wallet_id']}")
    )["Items"]
    assert all(
        set(row) == {"pk", "sk", "transferId"} for row in refs if row["sk"].startswith("TRANSFER#")
    )


def _u1_shaped(dynamodb_resource, ledger_table, transactions_table, directory_table):
    """Wallets, canonical rows and reference rows exactly as the U1 writer left them."""
    from raad_money.ledger import CompletedTransfer
    from raad_money.storage import WalletDirectory
    from raad_money.wallet_setup import ensure_wallet_and_holder

    repo = LedgerRepository(
        ledger_table, resource=dynamodb_resource, transactions_table=transactions_table
    )
    directory = WalletDirectory(directory_table)
    wallets = {}
    for owner in ("s", "r"):
        wallet, _ = ensure_wallet_and_holder(repo, directory, owner, f"{owner}@example.test", owner)
        repo.table.update_item(
            Key={"pk": f"WALLET#{wallet['wallet_id']}", "sk": "WALLET"},
            UpdateExpression="REMOVE transfer_sequence",
        )
        wallets[owner] = wallet["wallet_id"]
    history = [
        ("s", "r", "2026-11-14T00:00:00.000Z"),
        ("r", "s", "2026-11-14T00:00:00.000Z"),
        ("s", "r", "2026-11-14T01:00:00.000Z"),
    ]
    for index, (sender, recipient, stamp) in enumerate(history):
        identity = f"00000000-0000-4000-8000-{index + 1:012d}"
        repo.transactions.put_item(
            Item=CompletedTransfer(
                identity, f"TXN-{index}", wallets[sender], wallets[recipient], 100, stamp
            ).as_item()
        )
        for owner in (sender, recipient):
            repo.table.put_item(
                Item={
                    "pk": f"WALLET#{wallets[owner]}",
                    "sk": f"TRANSFER#{stamp}#{identity}",
                    "transferId": identity,
                }
            )
    return repo, directory, wallets


def test_u1_wallets_are_seeded_in_c1_order_before_u2_moves_money(
    dynamodb_resource, ledger_table, transactions_table, directory_table
):
    from raad_money.history import HistoryReader, TransactionsIndex
    from raad_money.ledger import TransferRequest
    from raad_money.store_errors import TransientStoreError
    from raad_money.transfers import TransferService

    repo, directory, wallets = _u1_shaped(
        dynamodb_resource, ledger_table, transactions_table, directory_table
    )
    service = TransferService(repo, directory)
    with pytest.raises(TransientStoreError):
        service.submit("s", "key-premigrate-01", TransferRequest("r@example.test", 100))
    with pytest.raises(migration.MigrationConflict):
        migration.check_ordinals_ready(repo.table, repo.transactions)
    dry = migration.seed_ordinals(repo.table, repo.transactions)
    assert (dry["rows"], dry["rowsSeeded"]) == (3, 0)
    with pytest.raises(migration.MigrationConflict):
        migration.seed_ordinals(repo.table, repo.transactions, apply=True)
    applied = migration.seed_ordinals(
        repo.table, repo.transactions, apply=True, writes_quiesced=True
    )
    assert (applied["rowsSeeded"], applied["walletsSeeded"]) == (3, 2)
    seeded = repo.get_wallet_by_id(wallets["s"])
    assert seeded["transfer_sequence"] == 3
    assert seeded["last_completed_sort_key"].startswith("2026-11-14T01:00:00.000Z#")
    again = migration.seed_ordinals(repo.table, repo.transactions, apply=True, writes_quiesced=True)
    assert (again["rowsSeeded"], again["walletsSeeded"]) == (0, 0)
    balances = [repo.get_wallet(owner)["balance_halalas"] for owner in ("s", "r")]
    assert balances == [100_000, 100_000]
    assert (
        service.submit(
            "s", "key-postmigrate-1", TransferRequest("r@example.test", 100)
        ).outcome.status
        == 201
    )
    reader = HistoryReader(TransactionsIndex(repo.transactions), sleep=lambda _: None)
    result = reader.page(repo.get_wallet("s"), 2, None, 1_800_000_000)
    seen = [row["transfer_id"] for row in result.rows]
    while result.next_position:
        result = reader.page(repo.get_wallet("s"), 2, result.next_position, 1_800_000_000)
        seen += [row["transfer_id"] for row in result.rows]
    assert len(seen) == len(set(seen)) == 4
    assert seen[1:] == [f"00000000-0000-4000-8000-{n:012d}" for n in (3, 2, 1)]
    migration.check_ordinals_ready(repo.table, repo.transactions)


def test_unmatched_legacy_record_is_retained_for_investigation(
    dynamodb_resource, ledger_table, transactions_table
):
    ledger, transactions = (
        dynamodb_resource.Table(ledger_table),
        dynamodb_resource.Table(transactions_table),
    )
    ledger.put_item(
        Item={
            "pk": "WALLET#s",
            "sk": "TRANSFER#2026-11-14T00:00:00.000Z#2bc440be-a85a-4cf8-9bf1-128fc7214c60",
            "transfer_id": "2bc440be-a85a-4cf8-9bf1-128fc7214c60",
            "reference": "TXN-ONE",
            "sender_wallet_id": "s",
            "recipient_wallet_id": "r",
            "amount_halalas": 1,
            "completed_at": "2026-11-14T00:00:00.000Z",
        }
    )
    with pytest.raises(migration.MigrationConflict):
        migration.migrate(ledger, transactions, apply=True, writes_quiesced=True)
    assert len(ledger.scan()["Items"]) == 1
    assert transactions.scan()["Items"] == []
