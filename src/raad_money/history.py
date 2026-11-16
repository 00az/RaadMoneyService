"""Stable transfer history over eventually consistent role indexes.

Each page queries the sender and recipient indexes at or below the snapshot key, merges them
newest first, strongly reads the canonical rows, and accepts the page only when the wallet's
ordinals are exactly N, N-1, ... for every slot. Index lag or any gap gets one bounded
reread, then a safe failure; a partial page or a false end is never returned.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from boto3.dynamodb.conditions import Key

from raad_money.cursor import CURSOR_LIFETIME_SECONDS, HistoryPosition
from raad_money.storage import HistoryIntegrityError, LedgerRepository, _transfer_from_item

SENDER_INDEX = "bySenderWallet"
RECIPIENT_INDEX = "byRecipientWallet"
_ROLE_INDEX = {
    "sender": (SENDER_INDEX, "senderWalletId"),
    "recipient": (RECIPIENT_INDEX, "recipientWalletId"),
}
_REREADS = 1
_REREAD_PAUSE_SECONDS = 0.05


class TransferIndex(Protocol):
    def query_role(
        self, wallet_id: str, role: str, upper_key: str, inclusive: bool, limit: int
    ) -> list[dict[str, Any]]: ...

    def get_canonical(self, transfer_ids: list[str]) -> dict[str, dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class HistoryResult:
    rows: list[dict[str, Any]]
    next_position: HistoryPosition | None


class TransactionsIndex:
    """DynamoDB adapter: keyed role-index queries (never scans) and strong canonical reads."""

    def __init__(self, table: Any) -> None:
        self.table = table

    def query_role(
        self, wallet_id: str, role: str, upper_key: str, inclusive: bool, limit: int
    ) -> list[dict[str, Any]]:
        index_name, partition = _ROLE_INDEX[role]
        bound = (
            Key("completedSortKey").lte(upper_key)
            if inclusive
            else Key("completedSortKey").lt(upper_key)
        )
        response = self.table.query(
            IndexName=index_name,
            KeyConditionExpression=Key(partition).eq(wallet_id) & bound,
            ScanIndexForward=False,
            Limit=limit,
        )
        return list(response.get("Items", []))

    def get_canonical(self, transfer_ids: list[str]) -> dict[str, dict[str, Any]]:
        rows = LedgerRepository._batch_get(
            self.table, [{"transferId": transfer_id} for transfer_id in transfer_ids]
        )
        return {str(row["transferId"]): row for row in rows}


class HistoryReader:
    def __init__(
        self, index: TransferIndex, *, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._index = index
        self._sleep = sleep

    def page(
        self,
        wallet: dict[str, Any],
        limit: int,
        position: HistoryPosition | None,
        now: int,
    ) -> HistoryResult:
        wallet_id = str(wallet["wallet_id"])
        if position is None:
            sequence = wallet.get("transfer_sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise HistoryIntegrityError("wallet history ordinals are not ready")
            if sequence == 0:
                return HistoryResult([], None)
            snapshot_key = str(wallet.get("last_completed_sort_key") or "")
            if not snapshot_key:
                raise HistoryIntegrityError("wallet snapshot key is missing")
            position = HistoryPosition(
                expires_at=now + CURSOR_LIFETIME_SECONDS,
                limit=limit,
                snapshot_sequence=sequence,
                snapshot_key=snapshot_key,
                next_ordinal=sequence,
                last_key=snapshot_key,
            )
            inclusive = True
        else:
            inclusive = False
        count = min(limit, position.next_ordinal)
        rows = self._verified_rows(wallet_id, position, inclusive, count)
        remaining = position.next_ordinal - count
        following = (
            HistoryPosition(
                expires_at=position.expires_at,
                limit=limit,
                snapshot_sequence=position.snapshot_sequence,
                snapshot_key=position.snapshot_key,
                next_ordinal=remaining,
                last_key=rows[-1]["completed_sort_key"],
            )
            if remaining > 0
            else None
        )
        return HistoryResult(rows, following)

    def _verified_rows(
        self, wallet_id: str, position: HistoryPosition, inclusive: bool, count: int
    ) -> list[dict[str, Any]]:
        for attempt in range(_REREADS + 1):
            rows = self._load(wallet_id, position, inclusive, count)
            if rows is not None:
                return rows
            if attempt < _REREADS:
                self._sleep(_REREAD_PAUSE_SECONDS)
        raise HistoryIntegrityError("history page could not be proven complete")

    def _load(
        self, wallet_id: str, position: HistoryPosition, inclusive: bool, count: int
    ) -> list[dict[str, Any]] | None:
        candidates = [
            item
            for role in ("sender", "recipient")
            for item in self._index.query_role(wallet_id, role, position.last_key, inclusive, count)
        ]
        merged = sorted(candidates, key=lambda item: str(item["completedSortKey"]), reverse=True)
        merged = merged[:count]
        identities = [str(item["transferId"]) for item in merged]
        if len(merged) < count or len(set(identities)) != len(identities):
            return None
        canonical = self._index.get_canonical(identities)
        rows: list[dict[str, Any]] = []
        for slot, (item, identity) in enumerate(zip(merged, identities, strict=True)):
            row = canonical.get(identity)
            if row is None or row.get("completedSortKey") != item["completedSortKey"]:
                return None
            if _ordinal(row, wallet_id) != position.next_ordinal - slot:
                return None
            rows.append(_history_row(row))
        return rows


def _ordinal(row: dict[str, Any], wallet_id: str) -> int:
    if row.get("senderWalletId") == wallet_id:
        value = row.get("senderOrdinal")
    elif row.get("recipientWalletId") == wallet_id:
        value = row.get("recipientOrdinal")
    else:
        raise HistoryIntegrityError("history row does not belong to the wallet")
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise HistoryIntegrityError("history ordinal is missing or not a number")
    if int(value) != value:
        raise HistoryIntegrityError("history ordinal is not a whole number")
    return int(value)


def _history_row(row: dict[str, Any]) -> dict[str, Any]:
    try:
        record = _transfer_from_item(row)
    except (KeyError, TypeError, ValueError) as error:
        raise HistoryIntegrityError("invalid canonical history row") from error
    return {**record, "completed_sort_key": str(row["completedSortKey"])}
