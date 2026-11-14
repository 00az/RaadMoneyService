"""DynamoDB repositories with strict Ledger/WalletDirectory separation."""

from __future__ import annotations

import os
import time
import unicodedata
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

from raad_money.ledger import CompletedTransfer, LedgerRules, Wallet


def _resource() -> Any:
    return boto3.resource(
        "dynamodb",
        endpoint_url=os.environ.get("DYNAMODB_ENDPOINT"),
        config=Config(
            connect_timeout=0.25,
            read_timeout=0.75,
            retries={"total_max_attempts": 1},
            max_pool_connections=2,
        ),
    )


class HistoryIntegrityError(RuntimeError):
    """Canonical history cannot be safely assembled."""


class EmailConflict(ValueError):
    """Verified email is already held by another identity."""


class HistoryPage(list[dict[str, Any]]):
    def __init__(self, rows: list[dict[str, Any]], has_more: bool) -> None:
        super().__init__(rows)
        self.has_more = has_more


class WalletMissing(LookupError):
    """The caller has not initialized a wallet."""


class RecipientMissing(LookupError):
    """No holder resolves to the supplied complete email address."""


def normalize_email(email: str) -> str:
    if not isinstance(email, str):
        raise TypeError("valid email is required")
    normalized = unicodedata.normalize("NFC", email.strip()).lower()
    local, separator, domain = normalized.partition("@")
    if (
        not separator
        or not local
        or not domain
        or "@" in domain
        or len(normalized) > 254
        or "." not in domain
    ):
        raise ValueError("valid email is required")
    return normalized


class LedgerRepository:
    """Owns wallet and transfer records; all balance writes use transactions."""

    def __init__(
        self, table_name: str, resource: Any | None = None, *, transactions_table: str | None = None
    ) -> None:
        database = resource or _resource()
        self.table = database.Table(table_name)
        self.transactions = database.Table(transactions_table or os.environ["TRANSACTIONS_TABLE"])

    def get_wallet(self, owner_id: str) -> dict[str, Any] | None:
        key = {"pk": f"OWNER#{owner_id}", "sk": "WALLET"}
        result = self.table.get_item(Key=key, ConsistentRead=True)
        item = result.get("Item")
        if item:
            wallet = self.table.get_item(
                Key={"pk": item["wallet_pk"], "sk": "WALLET"}, ConsistentRead=True
            ).get("Item")
            return _normalize_money_item(wallet) if wallet else None
        return None

    def get_wallet_by_id(self, wallet_id: str) -> dict[str, Any] | None:
        wallet = self.table.get_item(
            Key={"pk": f"WALLET#{wallet_id}", "sk": "WALLET"}, ConsistentRead=True
        ).get("Item")
        return _normalize_money_item(wallet) if wallet else None

    def get_wallets_by_ids(self, wallet_ids: list[str]) -> dict[str, dict[str, Any]]:
        keys = [
            {"pk": f"WALLET#{wallet_id}", "sk": "WALLET"} for wallet_id in dict.fromkeys(wallet_ids)
        ]
        items = self._batch_get(self.table, keys)
        return {item["wallet_id"]: _normalize_money_item(item) for item in items}

    def ensure_wallet(self, owner_id: str) -> tuple[dict[str, Any], bool]:
        existing = self.get_wallet(owner_id)
        if existing:
            return existing, False
        wallet_id = str(uuid.uuid4())
        wallet_pk = f"WALLET#{wallet_id}"
        now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        owner = {
            "pk": f"OWNER#{owner_id}",
            "sk": "WALLET",
            "wallet_pk": wallet_pk,
            "owner_id": owner_id,
        }
        wallet = {
            "pk": wallet_pk,
            "sk": "WALLET",
            "entity": "wallet",
            "wallet_id": wallet_id,
            "owner_id": owner_id,
            "balance_halalas": 100_000,
            "opening_balance_halalas": 100_000,
            "opened_at": now,
        }
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": self._encode(owner),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": self._encode(wallet),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
            return wallet, True
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                existing = self.get_wallet(owner_id)
                if existing:
                    return existing, False
            raise

    def transfer(
        self, sender: dict[str, Any], recipient: dict[str, Any], amount: int
    ) -> dict[str, Any]:
        decision = LedgerRules.transfer(
            Wallet(sender["wallet_id"], sender["balance_halalas"]),
            Wallet(recipient["wallet_id"], recipient["balance_halalas"]),
            amount,
        )
        transfer_id, reference = str(uuid.uuid4()), f"TXN-{uuid.uuid4().hex[:20].upper()}"
        completed_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        canonical = CompletedTransfer(
            transfer_id,
            reference,
            sender["wallet_id"],
            recipient["wallet_id"],
            amount,
            completed_at,
        )
        transfer = _transfer_from_item(canonical.as_item())
        items: list[dict[str, Any]] = []
        for wallet, new_balance in (
            (sender, decision.sender.balance_halalas),
            (recipient, decision.recipient.balance_halalas),
        ):
            items.append(
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": self._encode(
                            {"pk": f"WALLET#{wallet['wallet_id']}", "sk": "WALLET"}
                        ),
                        "UpdateExpression": "SET balance_halalas = :balance",
                        "ConditionExpression": "balance_halalas = :old AND balance_halalas >= :amount",
                        "ExpressionAttributeValues": self._encode(
                            {
                                ":balance": new_balance,
                                ":old": wallet["balance_halalas"],
                                ":amount": amount if wallet is sender else 0,
                            }
                        ),
                    }
                }
            )
        items.append(
            {
                "Put": {
                    "TableName": self.transactions.name,
                    "Item": canonical.as_item(),
                    "ConditionExpression": "attribute_not_exists(transferId)",
                }
            }
        )
        for wallet in (sender, recipient):
            items.append(
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": {
                            "pk": f"WALLET#{wallet['wallet_id']}",
                            "sk": f"TRANSFER#{completed_at}#{transfer_id}",
                            "transferId": transfer_id,
                        },
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
            )
        self.table.meta.client.transact_write_items(TransactItems=items)
        return {**transfer, "sender_balance_halalas": decision.sender.balance_halalas}

    def history(self, wallet_id: str, limit: int = 20) -> HistoryPage:
        size = min(max(limit, 1), 100)
        result = self.table.query(
            KeyConditionExpression=Key("pk").eq(f"WALLET#{wallet_id}")
            & Key("sk").begins_with("TRANSFER#"),
            ScanIndexForward=False,
            Limit=size + 1,
            ConsistentRead=True,
        )
        refs = result.get("Items", [])
        selected = refs[:size]
        try:
            identities = [ref["transferId"] for ref in selected]
            if len(set(identities)) != len(identities):
                raise HistoryIntegrityError("duplicate history reference")
            rows = self._batch_get(
                self.transactions, [{"transferId": identity} for identity in identities]
            )
            canonical = {row["transferId"]: row for row in rows}
            if len(canonical) != len(identities):
                raise HistoryIntegrityError("missing canonical transfer")
            records = []
            for ref, identity in zip(selected, identities, strict=True):
                row = canonical[identity]
                record = _transfer_from_item(row)
                expected = f"TRANSFER#{record['completed_at']}#{identity}"
                if (
                    wallet_id not in {record["sender_wallet_id"], record["recipient_wallet_id"]}
                    or ref["sk"] != expected
                ):
                    raise HistoryIntegrityError("history ownership or ordering mismatch")
                records.append(record)
            return HistoryPage(records, len(refs) > size or bool(result.get("LastEvaluatedKey")))
        except (KeyError, ValueError, TypeError) as error:
            raise HistoryIntegrityError("invalid canonical history") from error

    @staticmethod
    def _encode(value: dict[str, Any]) -> dict[str, Any]:
        # Resource clients already marshal native values, including transactions.
        # Encoding here again causes DynamoDB to receive maps instead of key strings.
        return value

    @staticmethod
    def _batch_get(table: Any, keys: list[dict[str, str]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        deadline = time.monotonic() + 1
        for offset in range(0, len(keys), 100):
            pending = keys[offset : offset + 100]
            for attempt in range(3):
                if time.monotonic() >= deadline:
                    raise RuntimeError("bounded batch lookup deadline exceeded")
                response = table.meta.client.batch_get_item(
                    RequestItems={table.name: {"Keys": pending, "ConsistentRead": True}}
                )
                items.extend(response.get("Responses", {}).get(table.name, []))
                pending = response.get("UnprocessedKeys", {}).get(table.name, {}).get("Keys", [])
                if not pending:
                    break
                if attempt == 2:
                    raise RuntimeError("bounded batch lookup did not complete")
                time.sleep(0.01 * 2**attempt)
        return [_normalize_money_item(item) for item in items]


class WalletDirectory:
    """Owns holder personal data and exact normalized-email resolution."""

    def __init__(self, table_name: str, resource: Any | None = None) -> None:
        self.table = (resource or _resource()).Table(table_name)

    def register(self, holder_id: str, email: str, display_name: str) -> None:
        normalized = normalize_email(email)
        if not holder_id or not display_name.strip():
            raise ValueError("holder id and display name are required")
        self.table.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": LedgerRepository._encode(
                            {
                                "pk": f"HOLDER#{holder_id}",
                                "sk": "PROFILE",
                                "holder_id": holder_id,
                                "email_normalized": normalized,
                                "display_name": display_name.strip(),
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": LedgerRepository._encode(
                            {"pk": f"EMAIL#{normalized}", "sk": "HOLDER", "holder_id": holder_id}
                        ),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ]
        )

    def ensure_holder(self, holder_id: str, email: str, display_name: str) -> None:
        """Create a profile only when absent; never overwrite an existing identity."""
        key = {"pk": f"HOLDER#{holder_id}", "sk": "PROFILE"}
        existing = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
        if existing:
            return
        try:
            self.register(holder_id, email, display_name)
        except self.table.meta.client.exceptions.TransactionCanceledException:
            winner = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
            if winner and winner.get("holder_id") == holder_id:
                return
            owner = self.table.get_item(
                Key={"pk": f"EMAIL#{normalize_email(email)}", "sk": "HOLDER"}, ConsistentRead=True
            ).get("Item")
            if owner and owner.get("holder_id") != holder_id:
                raise EmailConflict("verified email ownership conflict")
            raise

    def find_by_email(self, email: str) -> dict[str, Any]:
        normalized = normalize_email(email)
        key = {"pk": f"EMAIL#{normalized}", "sk": "HOLDER"}
        result = self.table.get_item(Key=key, ConsistentRead=True)
        holder = result.get("Item")
        if not holder:
            raise RecipientMissing
        return dict(
            self.table.get_item(
                Key={"pk": f"HOLDER#{holder['holder_id']}", "sk": "PROFILE"}, ConsistentRead=True
            )["Item"]
        )

    def get_by_holder_id(self, holder_id: str) -> dict[str, Any] | None:
        item = self.table.get_item(
            Key={"pk": f"HOLDER#{holder_id}", "sk": "PROFILE"}, ConsistentRead=True
        ).get("Item")
        return dict(item) if item else None

    def get_many(self, holder_ids: list[str]) -> dict[str, dict[str, Any]]:
        keys = [
            {"pk": f"HOLDER#{holder_id}", "sk": "PROFILE"}
            for holder_id in dict.fromkeys(holder_ids)
        ]
        items = LedgerRepository._batch_get(self.table, keys)
        return {item["holder_id"]: item for item in items}


def _normalize_money_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert DynamoDB Decimal values to whole-halalas ints; reject fractional data."""
    normalized = dict(item)
    for field in ("balance_halalas", "opening_balance_halalas", "amount_halalas"):
        value = normalized.get(field)
        if value is None:
            continue
        if isinstance(value, bool):
            raise TypeError("money record contains an invalid integer")
        if isinstance(value, Decimal):
            if value != value.to_integral_value():
                raise ValueError("money record contains fractional halalas")
            normalized[field] = int(value)
        elif not isinstance(value, int):
            raise TypeError("money record contains an invalid integer")
    return normalized


def _transfer_from_item(item: dict[str, Any]) -> dict[str, Any]:
    amount = _normalize_money_item({"amount_halalas": item["amountHalalas"]})["amount_halalas"]
    if item["currency"] != "SAR":
        raise ValueError("unexpected transaction currency")
    record = CompletedTransfer(
        item["transferId"],
        item["reference"],
        item["senderWalletId"],
        item["recipientWalletId"],
        amount,
        item["completedAt"],
    )
    return {
        "transfer_id": record.transfer_id,
        "reference": record.reference,
        "sender_wallet_id": record.sender_wallet_id,
        "recipient_wallet_id": record.recipient_wallet_id,
        "amount_halalas": record.amount_halalas,
        "completed_at": record.completed_at,
    }
