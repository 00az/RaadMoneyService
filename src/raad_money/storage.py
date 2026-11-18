"""DynamoDB repositories with strict Ledger/WalletDirectory separation."""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from raad_money.email_address import normalize_email
from raad_money.ledger import CompletedTransfer, TransferOutcome, format_instant
from raad_money.store_errors import (
    CommitUncertain,
    OutcomeAlreadyRecorded,
    ReferenceCollision,
    TransientStoreError,
    WalletChanged,
)

if TYPE_CHECKING:
    from raad_money.transfers import TransferCommit, WalletState

_UNCERTAIN_CODES = {"InternalServerError", "ServiceUnavailable"}
_REASONS_IN_MESSAGE = re.compile(r"\[([^\]]*)\]\s*$")


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


def _now() -> str:
    return format_instant(datetime.now(UTC))


class HistoryIntegrityError(RuntimeError):
    """Canonical history cannot be safely assembled."""


class ProfileVersionMissing(RuntimeError):
    """A saved outcome references a profile version that cannot be read; never recompute it."""


class EmailConflict(ValueError):
    """Verified email is already held by another identity."""


class HolderExists(ValueError):
    """The holder is already registered; an existing identity is never overwritten."""


class HistoryPage(list[dict[str, Any]]):
    def __init__(self, rows: list[dict[str, Any]], has_more: bool) -> None:
        super().__init__(rows)
        self.has_more = has_more


class WalletMissing(LookupError):
    """The caller has not initialized a wallet."""


class RecipientMissing(LookupError):
    """No holder resolves to the supplied complete email address."""


def error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def cancellation_codes(error: ClientError) -> list[str]:
    """Per-item transaction cancellation codes, from the response or its message."""
    reasons = error.response.get("CancellationReasons")
    if reasons:
        return [str(reason.get("Code") or "None") for reason in reasons]
    match = _REASONS_IN_MESSAGE.search(str(error.response.get("Error", {}).get("Message", "")))
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",")]


class LedgerRepository:
    """Owns wallets, keyed outcomes, receipts and transfer records; money moves only in
    conditional transactions."""

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
            "opened_at": _now(),
            "transfer_sequence": 0,
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
            if error_code(error) == "TransactionCanceledException":
                existing = self.get_wallet(owner_id)
                if existing:
                    return existing, False
            raise

    def get_outcome(self, owner_id: str, idempotency_key: str) -> TransferOutcome | None:
        """Strong read of the first outcome for (verified sender, Idempotency-Key)."""
        item = self.table.get_item(
            Key=self._outcome_key(owner_id, idempotency_key), ConsistentRead=True
        ).get("Item")
        return TransferOutcome.from_item(item) if item else None

    def record_refusal(
        self,
        owner_id: str,
        idempotency_key: str,
        outcome: TransferOutcome,
        *,
        balance_guard: WalletState | None,
    ) -> None:
        """Save a final business refusal once; a low-balance refusal is guarded by the wallet
        state it was decided on, so a concurrent change forces re-evaluation."""
        items: list[dict[str, Any]] = [self._outcome_put(owner_id, idempotency_key, outcome)]
        if balance_guard is not None:
            items.append(
                {
                    "ConditionCheck": {
                        "TableName": self.table.name,
                        "Key": {"pk": f"WALLET#{balance_guard.wallet_id}", "sk": "WALLET"},
                        "ConditionExpression": "balance_halalas = :balance AND "
                        "transfer_sequence = :sequence",
                        "ExpressionAttributeValues": {
                            ":balance": balance_guard.balance_halalas,
                            ":sequence": balance_guard.transfer_sequence,
                        },
                    }
                }
            )
        self._transact(items, outcome_index=0, wallet_indexes={1}, reference_index=None)

    def commit_transfer(self, commit: TransferCommit) -> None:
        """One all-or-nothing change: both wallets, the canonical row, U1 reference rows,
        the first outcome and the unique reference marker."""
        outcome = commit.outcome
        if (
            outcome.transfer_id is None
            or outcome.reference is None
            or outcome.completed_at is None
            or outcome.amount_halalas is None
        ):
            raise ValueError("only a completed outcome can be committed")
        sender_ordinal = commit.sender.transfer_sequence + 1
        recipient_ordinal = commit.recipient.transfer_sequence + 1
        canonical = CompletedTransfer(
            outcome.transfer_id,
            outcome.reference,
            commit.sender.wallet_id,
            commit.recipient.wallet_id,
            outcome.amount_halalas,
            outcome.completed_at,
        ).as_item()
        canonical.update(
            completedSortKey=commit.sort_key,
            senderOrdinal=sender_ordinal,
            recipientOrdinal=recipient_ordinal,
        )
        items = [
            self._wallet_update(commit.sender, commit.sender_balance_after, commit.sort_key),
            self._wallet_update(commit.recipient, commit.recipient_balance_after, commit.sort_key),
            {
                "Put": {
                    "TableName": self.transactions.name,
                    "Item": canonical,
                    "ConditionExpression": "attribute_not_exists(transferId)",
                }
            },
        ]
        for state in (commit.sender, commit.recipient):
            items.append(
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": {
                            "pk": f"WALLET#{state.wallet_id}",
                            "sk": f"TRANSFER#{outcome.completed_at}#{outcome.transfer_id}",
                            "transferId": outcome.transfer_id,
                        },
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
            )
        items.append(self._outcome_put(commit.owner_id, commit.idempotency_key, outcome))
        items.append(
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": {
                        "pk": f"REFERENCE#{outcome.reference}",
                        "sk": "REFERENCE",
                        "transferId": outcome.transfer_id,
                    },
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            }
        )
        self._transact(items, outcome_index=5, wallet_indexes={0, 1}, reference_index=6)

    def get_event_receipt(self, event_id: str) -> dict[str, Any] | None:
        item = self.table.get_item(
            Key={"pk": f"EVENT#{event_id}", "sk": "RECEIPT"}, ConsistentRead=True
        ).get("Item")
        return dict(item) if item else None

    def get_user_receipt(self, user_id: str) -> dict[str, Any] | None:
        item = self.table.get_item(
            Key={"pk": f"USER_RECEIPT#{user_id}", "sk": "RECEIPT"}, ConsistentRead=True
        ).get("Item")
        return dict(item) if item else None

    def record_receipt(self, event_id: str, user_id: str, environment: str) -> str:
        """Claim the first consistent (eventId, userId) pair; both mappings are unique.

        Returns `recorded`, `duplicate` (this pair or this user was already processed) or
        `conflict` (the event ID is already bound to a different user).
        """
        received_at = _now()
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": {
                                "pk": f"EVENT#{event_id}",
                                "sk": "RECEIPT",
                                "entity": "event_receipt",
                                "user_id": user_id,
                                "environment": environment,
                                "received_at": received_at,
                            },
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": {
                                "pk": f"USER_RECEIPT#{user_id}",
                                "sk": "RECEIPT",
                                "entity": "user_receipt",
                                "event_id": event_id,
                                "environment": environment,
                                "received_at": received_at,
                            },
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except ClientError as error:
            if error_code(error) != "TransactionCanceledException":
                raise
            existing = self.get_event_receipt(event_id)
            if existing and existing.get("user_id") != user_id:
                return "conflict"
            if existing or self.get_user_receipt(user_id):
                return "duplicate"
            raise TransientStoreError("receipt was not recorded") from error
        return "recorded"

    def history(self, wallet_id: str, limit: int = 20) -> HistoryPage:
        """U1 strongly consistent reference read, retained for migration verification."""
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

    def _wallet_update(self, state: WalletState, balance: int, sort_key: str) -> dict[str, Any]:
        return {
            "Update": {
                "TableName": self.table.name,
                "Key": {"pk": f"WALLET#{state.wallet_id}", "sk": "WALLET"},
                "UpdateExpression": "SET balance_halalas = :balance, "
                "transfer_sequence = :next, last_completed_sort_key = :key",
                "ConditionExpression": "balance_halalas = :old AND transfer_sequence = :sequence",
                "ExpressionAttributeValues": {
                    ":balance": balance,
                    ":old": state.balance_halalas,
                    ":sequence": state.transfer_sequence,
                    ":next": state.transfer_sequence + 1,
                    ":key": sort_key,
                },
            }
        }

    def _outcome_put(
        self, owner_id: str, idempotency_key: str, outcome: TransferOutcome
    ) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self.table.name,
                "Item": {
                    **self._outcome_key(owner_id, idempotency_key),
                    "entity": "transfer_request",
                    "createdAt": _now(),
                    **outcome.as_item(),
                },
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }

    @staticmethod
    def _outcome_key(owner_id: str, idempotency_key: str) -> dict[str, str]:
        if not owner_id or not idempotency_key:
            raise ValueError("sender and idempotency key are required")
        return {"pk": f"REQUEST#{owner_id}", "sk": f"KEY#{idempotency_key}"}

    def _transact(
        self,
        items: list[dict[str, Any]],
        *,
        outcome_index: int,
        wallet_indexes: set[int],
        reference_index: int | None,
    ) -> None:
        """Run one transaction and translate its cancellation into a service decision."""
        try:
            self.table.meta.client.transact_write_items(TransactItems=items)
        except ClientError as error:
            code = error_code(error)
            if code in _UNCERTAIN_CODES:
                raise CommitUncertain from error
            if code != "TransactionCanceledException":
                raise TransientStoreError("ledger write was not applied") from error
            reasons = cancellation_codes(error)
            if _reason(reasons, outcome_index) == "ConditionalCheckFailed":
                raise OutcomeAlreadyRecorded from error
            if reference_index is not None and _reason(reasons, reference_index) == (
                "ConditionalCheckFailed"
            ):
                raise ReferenceCollision from error
            if "TransactionConflict" in reasons or any(
                _reason(reasons, index) == "ConditionalCheckFailed" for index in wallet_indexes
            ):
                raise WalletChanged from error
            raise TransientStoreError("ledger transaction was cancelled") from error
        except BotoCoreError as error:
            raise CommitUncertain from error

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


def _reason(reasons: list[str], index: int) -> str:
    return reasons[index] if index < len(reasons) else "None"


class WalletDirectory:
    """Owns holder personal data, immutable profile versions and exact email resolution."""

    def __init__(self, table_name: str, resource: Any | None = None) -> None:
        self.table = (resource or _resource()).Table(table_name)

    def register(self, holder_id: str, email: str, display_name: str) -> None:
        normalized = normalize_email(email)
        if not holder_id or not display_name.strip():
            raise ValueError("holder id and display name are required")
        version_id = str(uuid.uuid4())
        registered_at = _now()
        display_email = email.strip()
        try:
            self._register_items(
                holder_id, normalized, display_email, display_name, registered_at, version_id
            )
        except ClientError as error:
            if error_code(error) != "TransactionCanceledException":
                raise
            reasons = cancellation_codes(error)
            if _reason(reasons, 0) == "ConditionalCheckFailed":
                raise HolderExists("holder is already registered") from None
            if _reason(reasons, 1) == "ConditionalCheckFailed":
                raise EmailConflict("verified email ownership conflict") from None
            raise TransientStoreError("holder registration was not applied") from None

    def _register_items(
        self,
        holder_id: str,
        normalized: str,
        display_email: str,
        display_name: str,
        registered_at: str,
        version_id: str,
    ) -> None:
        self.table.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": {
                            "pk": f"HOLDER#{holder_id}",
                            "sk": "PROFILE",
                            "holder_id": holder_id,
                            "email": display_email,
                            "email_normalized": normalized,
                            "display_name": display_name.strip(),
                            "registered_at": registered_at,
                            "profile_version_id": version_id,
                        },
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": {
                            "pk": f"EMAIL#{normalized}",
                            "sk": "HOLDER",
                            "holder_id": holder_id,
                        },
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                self._version_put(version_id, holder_id, display_email, display_name.strip()),
            ]
        )

    def ensure_holder(self, holder_id: str, email: str, display_name: str) -> None:
        """Create a profile only when absent; never overwrite or merge an existing identity."""
        key = {"pk": f"HOLDER#{holder_id}", "sk": "PROFILE"}
        existing = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
        if existing:
            return
        try:
            self.register(holder_id, email, display_name)
        except (HolderExists, EmailConflict, TransientStoreError) as refusal:
            winner = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
            if winner and winner.get("holder_id") == holder_id:
                return  # A concurrent setup for this same holder won; converge on it.
            if isinstance(refusal, EmailConflict):
                raise
            owner = self.table.get_item(
                Key={"pk": f"EMAIL#{normalize_email(email)}", "sk": "HOLDER"}, ConsistentRead=True
            ).get("Item")
            if owner and owner.get("holder_id") != holder_id:
                raise EmailConflict("verified email ownership conflict") from None
            raise

    def find_by_email(self, email: str) -> dict[str, Any]:
        normalized = normalize_email(email)
        key = {"pk": f"EMAIL#{normalized}", "sk": "HOLDER"}
        result = self.table.get_item(Key=key, ConsistentRead=True)
        holder = result.get("Item")
        if not holder:
            raise RecipientMissing
        profile = self.table.get_item(
            Key={"pk": f"HOLDER#{holder['holder_id']}", "sk": "PROFILE"}, ConsistentRead=True
        ).get("Item")
        if not profile or profile.get("holder_id") != holder["holder_id"]:
            raise RecipientMissing
        return dict(profile)

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

    def ensure_profile_version(self, holder_id: str) -> dict[str, Any]:
        """Return the holder's immutable profile version, creating one for a U1 holder."""
        profile = self.get_by_holder_id(holder_id)
        if profile is None:
            raise RecipientMissing
        version_id = profile.get("profile_version_id")
        if version_id:
            version = self.get_profile_version(str(version_id))
            if version is None or version["holder_id"] != holder_id:
                raise ProfileVersionMissing("current profile version is unreadable")
            return version
        created = str(uuid.uuid4())
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    self._version_put(
                        created, holder_id, display_email(profile), str(profile["display_name"])
                    ),
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {"pk": f"HOLDER#{holder_id}", "sk": "PROFILE"},
                            "UpdateExpression": "SET profile_version_id = :version",
                            "ConditionExpression": "attribute_exists(pk) AND "
                            "attribute_not_exists(profile_version_id)",
                            "ExpressionAttributeValues": {":version": created},
                        }
                    },
                ]
            )
        except self.table.meta.client.exceptions.TransactionCanceledException:
            # A concurrent caller attached a version first; use that one, never a second.
            winner = self.get_by_holder_id(holder_id) or {}
            if not winner.get("profile_version_id"):
                raise TransientStoreError("profile version could not be attached") from None
            created = str(winner["profile_version_id"])
        version = self.get_profile_version(created)
        if version is None:
            raise ProfileVersionMissing("new profile version is unreadable")
        return version

    def get_profile_version(self, version_id: str) -> dict[str, Any] | None:
        item = self.table.get_item(
            Key={"pk": f"PROFILE_VERSION#{version_id}", "sk": "VERSION"}, ConsistentRead=True
        ).get("Item")
        return dict(item) if item else None

    def _version_put(
        self, version_id: str, holder_id: str, email: str, display_name: str
    ) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self.table.name,
                "Item": {
                    "pk": f"PROFILE_VERSION#{version_id}",
                    "sk": "VERSION",
                    "profile_version_id": version_id,
                    "holder_id": holder_id,
                    "email": email,
                    "display_name": display_name,
                    "created_at": _now(),
                },
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }


def display_email(profile: dict[str, Any]) -> str:
    """The confirmed spelling kept for display; U1 holders only kept the normalized form."""
    return str(profile.get("email") or profile["email_normalized"])


def _normalize_money_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert DynamoDB Decimal values to whole-number ints; reject fractional data."""
    normalized = dict(item)
    for field in (
        "balance_halalas",
        "opening_balance_halalas",
        "amount_halalas",
        "transfer_sequence",
    ):
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
