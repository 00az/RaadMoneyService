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
