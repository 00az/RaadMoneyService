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


def ordinal_plan(
    ledger_rows: list[dict[str, Any]], transactions: Any
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """U2 history layout: per-wallet ordinals in ascending C1 order (completedAt, transferId).

    Returns the canonical-row fields and wallet counters that must exist. Existing values
    that disagree are a conflict, never overwritten.
    """
    planned = plan(ledger_rows, transactions)
    if any("transfer_id" in row for _, refs in planned for row in refs):
        raise MigrationConflict("migrate legacy payloads to canonical rows first")
    wallets = {row["wallet_id"]: row for row in ledger_rows if row.get("entity") == "wallet"}
    keys = {
        record["transferId"]: f"{record['completedAt']}#{record['transferId']}"
        for record, _ in planned
    }
    by_wallet: dict[str, list[str]] = defaultdict(list)
    for record, _ in planned:
        for wallet_id in (record["senderWalletId"], record["recipientWalletId"]):
            if wallet_id not in wallets:
                raise MigrationConflict("transfer references an unknown wallet")
            by_wallet[wallet_id].append(record["transferId"])
    ordinals: dict[tuple[str, str], int] = {}
    wallet_fields: dict[str, dict[str, Any]] = {}
    for wallet_id in wallets:
        ordered = sorted(by_wallet.get(wallet_id, []), key=lambda identity: keys[identity])
        for position, identity in enumerate(ordered, start=1):
            ordinals[(wallet_id, identity)] = position
        wallet_fields[wallet_id] = {
            "transfer_sequence": len(ordered),
            "last_completed_sort_key": keys[ordered[-1]] if ordered else None,
        }
    row_fields = {
        record["transferId"]: {
            "completedSortKey": keys[record["transferId"]],
            "senderOrdinal": ordinals[(record["senderWalletId"], record["transferId"])],
            "recipientOrdinal": ordinals[(record["recipientWalletId"], record["transferId"])],
        }
        for record, _ in planned
    }
    for record, _ in planned:
        _assert_agrees(record, row_fields[record["transferId"]])
    for wallet_id, fields in wallet_fields.items():
        _assert_agrees(wallets[wallet_id], fields)
    return row_fields, wallet_fields


def _assert_agrees(existing: dict[str, Any], expected: dict[str, Any]) -> None:
    for field, value in expected.items():
        current = existing.get(field)
        if current is not None and value is not None and current != value:
            raise MigrationConflict("existing history layout disagrees with C1 order")


def seed_ordinals(
    ledger: Any, transactions: Any, *, apply: bool = False, writes_quiesced: bool = False
) -> dict[str, int]:
    """Seed U2 ordinals and wallet high-watermarks without touching balances, IDs or times."""
    if apply and not writes_quiesced:
        raise MigrationConflict("quiesce transfer writes before apply")
    before = scan(ledger)
    row_fields, wallet_fields = ordinal_plan(before, transactions)
    wallets = {row["wallet_id"]: row for row in before if row.get("entity") == "wallet"}
    rows_to_seed = [
        identity
        for identity, fields in row_fields.items()
        if transactions.get_item(Key={"transferId": identity}, ConsistentRead=True)
        .get("Item", {})
        .get("senderOrdinal")
        is None
    ]
    wallets_to_seed = [w for w in wallet_fields if wallets[w].get("transfer_sequence") is None]
    for identity in rows_to_seed if apply else []:
        fields = row_fields[identity]
        transactions.update_item(
            Key={"transferId": identity},
            UpdateExpression="SET completedSortKey = :key, senderOrdinal = :s, "
            "recipientOrdinal = :r",
            ConditionExpression="attribute_exists(transferId) AND attribute_not_exists(senderOrdinal)",
            ExpressionAttributeValues={
                ":key": fields["completedSortKey"],
                ":s": fields["senderOrdinal"],
                ":r": fields["recipientOrdinal"],
            },
        )
    for wallet_id in wallets_to_seed if apply else []:
        fields = wallet_fields[wallet_id]
        expression, values = "SET transfer_sequence = :n", {":n": fields["transfer_sequence"]}
        if fields["last_completed_sort_key"]:
            expression += ", last_completed_sort_key = :k"
            values[":k"] = fields["last_completed_sort_key"]
        values[":b"] = wallets[wallet_id]["balance_halalas"]
        ledger.update_item(
            Key={"pk": f"WALLET#{wallet_id}", "sk": "WALLET"},
            UpdateExpression=expression,
            ConditionExpression="attribute_not_exists(transfer_sequence) AND balance_halalas = :b",
            ExpressionAttributeValues=values,
        )
    if apply:
        check_ordinals_ready(ledger, transactions)
    return {
        "wallets": len(wallet_fields),
        "rows": len(row_fields),
        "rowsSeeded": len(rows_to_seed) if apply else 0,
        "walletsSeeded": len(wallets_to_seed) if apply else 0,
    }


def check_ordinals_ready(ledger: Any, transactions: Any) -> None:
    """Every wallet counter and canonical ordinal must exist and match C1 order exactly."""
    rows = scan(ledger)
    row_fields, wallet_fields = ordinal_plan(rows, transactions)
    for identity, expected in row_fields.items():
        stored = transactions.get_item(Key={"transferId": identity}, ConsistentRead=True)["Item"]
        if any(stored.get(field) != value for field, value in expected.items()):
            raise MigrationConflict("canonical rows are missing U2 ordinals")
    for row in rows:
        if row.get("entity") != "wallet":
            continue
        expected_wallet = wallet_fields[row["wallet_id"]]
        if row.get("transfer_sequence") != expected_wallet["transfer_sequence"] or (
            expected_wallet["last_completed_sort_key"]
            and row.get("last_completed_sort_key") != expected_wallet["last_completed_sort_key"]
        ):
            raise MigrationConflict("wallet history counters are not seeded")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["dev"], required=True)
    parser.add_argument("--ledger-table", required=True)
    parser.add_argument("--transactions-table", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writes-quiesced", action="store_true")
    parser.add_argument(
        "--seed-ordinals",
        action="store_true",
        help="Seed U2 history ordinals and wallet counters (dry run unless --apply)",
    )
    parser.add_argument(
        "--check-ready",
        action="store_true",
        help="Fail unless every row/reference is canonical and U2 ordinals are seeded; "
        "writes nothing",
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
            check_ordinals_ready(ledger, transactions)
            report = {"ready": True, "transfers": len(planned), "schemaVersion": 3}
        elif args.seed_ordinals:
            report = seed_ordinals(
                ledger, transactions, apply=args.apply, writes_quiesced=args.writes_quiesced
            )
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
