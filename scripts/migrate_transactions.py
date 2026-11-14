"""Validate and migrate legacy transfer payloads while writes are quiesced."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from raad_money.ledger import CompletedTransfer
from raad_money.storage import _transfer_from_item


class MigrationConflict(ValueError):
    """Stop without discarding conflicting or unmatched records."""


def scan(table: Any) -> list[dict[str, Any]]:
    pages = table.meta.client.get_paginator("scan").paginate(
        TableName=table.name, ConsistentRead=True
    )
    return [row for page in pages for row in page.get("Items", [])]


def canonical(row: dict[str, Any]) -> dict[str, Any]:
    amount = row["amount_halalas"]
    if isinstance(amount, bool) or amount != int(amount):
        raise MigrationConflict("invalid legacy amount")
    return CompletedTransfer(
        row["transfer_id"],
        row["reference"],
        row["sender_wallet_id"],
        row["recipient_wallet_id"],
        int(amount),
        row["completed_at"],
    ).as_item()


def plan(
    rows: list[dict[str, Any]], transactions: Any
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("sk", "").startswith("TRANSFER#"):
            identity = row.get("transferId") or row.get("transfer_id")
            if not identity:
                raise MigrationConflict("transfer identity missing")
            grouped[identity].append(row)
    result = []
    for identity, refs in grouped.items():
        existing = transactions.get_item(Key={"transferId": identity}, ConsistentRead=True).get(
            "Item"
        )
        legacy = [row for row in refs if "transfer_id" in row]
        record = canonical(legacy[0]) if legacy else existing
        if record is None or len(refs) != 2:
            raise MigrationConflict("unmatched transfer")
        _transfer_from_item(record)
        if existing and existing != record:
            raise MigrationConflict("canonical transfer conflicts")
        if any(canonical(row) != record for row in legacy):
            raise MigrationConflict("legacy payloads disagree")
        expected = {f"WALLET#{record['senderWalletId']}", f"WALLET#{record['recipientWalletId']}"}
        expected_sort = f"TRANSFER#{record['completedAt']}#{identity}"
        if {row["pk"] for row in refs} != expected or any(
            row["sk"] != expected_sort for row in refs
        ):
            raise MigrationConflict("lookup mismatch")
        result.append((record, refs))
    return result


def conditional_put(
    table: str, new: dict[str, Any], old: dict[str, Any], *, absent: bool = False
) -> dict[str, Any]:
    names = {f"#f{i}": key for i, key in enumerate(old)}
    values = {f":v{i}": value for i, value in enumerate(old.values())}
    condition = " AND ".join(f"#f{i} = :v{i}" for i in range(len(old)))
    if absent:
        condition = "attribute_not_exists(transferId) OR (" + condition + ")"
    return {
        "Put": {
            "TableName": table,
            "Item": new,
            "ConditionExpression": condition,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
    }


def migrate(
    ledger: Any, transactions: Any, *, apply: bool = False, writes_quiesced: bool = False
) -> dict[str, int]:
    if apply and not writes_quiesced:
        raise MigrationConflict("quiesce transfer writes before apply")
    before = scan(ledger)
    planned = plan(before, transactions)
    balances = {
        row["pk"]: row["balance_halalas"] for row in before if row.get("entity") == "wallet"
    }
    for record, refs in planned if apply else []:
        writes = [conditional_put(transactions.name, record, record, absent=True)]
        for row in refs:
            reference = {"pk": row["pk"], "sk": row["sk"], "transferId": record["transferId"]}
            writes.append(conditional_put(ledger.name, reference, row))
        ledger.meta.client.transact_write_items(TransactItems=writes)
    if apply:
        after = scan(ledger)
        if balances != {
            row["pk"]: row["balance_halalas"] for row in after if row.get("entity") == "wallet"
        }:
            raise MigrationConflict("balances changed; keep writes quiesced")
        verified = plan(after, transactions)
        if len(verified) != len(planned) or any(
            "transfer_id" in row for _, refs in verified for row in refs
        ):
            raise MigrationConflict("post-cutover history verification failed")
    return {
        "wallets": len(balances),
        "transfers": len(planned),
        "references": 2 * len(planned),
        "applied": int(apply),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["dev"], required=True)
    parser.add_argument("--ledger-table", required=True)
    parser.add_argument("--transactions-table", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writes-quiesced", action="store_true")
    parser.add_argument(
        "--check-ready",
        action="store_true",
        help="Fail unless every row/reference is canonical and complete; writes nothing",
    )
    args = parser.parse_args()
    # Default dry run. Supply actual environment table names, never credentials.
    database = boto3.resource(
        "dynamodb",
        config=Config(connect_timeout=0.25, read_timeout=2, retries={"total_max_attempts": 1}),
    )
    try:
        ledger, transactions = (
            database.Table(args.ledger_table),
            database.Table(args.transactions_table),
        )
        if args.check_ready:
            rows = scan(ledger)
            planned = plan(rows, transactions)
            if any("transfer_id" in row for _, refs in planned for row in refs):
                raise MigrationConflict(
                    "legacy payload references remain; migrate with writes disabled"
                )
            if {row["transferId"] for row in scan(transactions)} != {
                record["transferId"] for record, _ in planned
            }:
                raise MigrationConflict("canonical/reference counts disagree")
            report = {"ready": True, "transfers": len(planned), "schemaVersion": 2}
        else:
            report = migrate(
                ledger, transactions, apply=args.apply, writes_quiesced=args.writes_quiesced
            )
        print(json.dumps(report))
        return 0
    except (ValueError, TypeError, KeyError, BotoCoreError, ClientError):
        print(json.dumps({"status": "failed", "action": "keep writes quiesced; investigate"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
