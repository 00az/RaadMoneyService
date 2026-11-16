"""U2 test-first stable history: signed cursors, snapshot paging and ordinal gap detection."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from raad_money.cursor import (
    CURSOR_LIFETIME_SECONDS,
    CursorInvalid,
    CursorKeys,
    HistoryPosition,
    issue_cursor,
    read_cursor,
)
from raad_money.history import HistoryReader
from raad_money.storage import HistoryIntegrityError

NOW = 1_800_000_000
KEYS = CursorKeys.from_secret(json.dumps({"version": "2", "key": "k" * 48}))
WALLET = "wallet-a"


def position(**overrides: Any) -> HistoryPosition:
    values: dict[str, Any] = {
        "expires_at": NOW + CURSOR_LIFETIME_SECONDS,
        "limit": 20,
        "snapshot_sequence": 45,
        "snapshot_key": "2026-11-16T10:00:00.000Z#ffffffff-ffff-4fff-bfff-ffffffffffff",
        "next_ordinal": 25,
        "last_key": "2026-11-16T09:00:00.000Z#00000000-0000-4000-8000-000000000020",
    }
    values.update(overrides)
    return HistoryPosition(**values)


def issued(**overrides: Any) -> str:
    return issue_cursor(position(**overrides), keys=KEYS, environment="dev", caller="sub-1")


def read(token: str, **overrides: Any) -> HistoryPosition:
    values: dict[str, Any] = {
        "keys": KEYS,
        "environment": "dev",
        "caller": "sub-1",
        "limit": 20,
        "now": NOW,
    }
    values.update(overrides)
    return read_cursor(token, **values)


def test_cursor_round_trips_every_binding() -> None:
    assert read(issued()) == position()
    assert len(issued()) <= 2048


@pytest.mark.parametrize(
    "override",
    [{"caller": "sub-2"}, {"environment": "stg"}, {"limit": 21}, {"now": NOW + 86_400}],
)
def test_cursor_refuses_wrong_user_environment_limit_or_expiry(override: dict[str, Any]) -> None:
    with pytest.raises(CursorInvalid):
        read(issued(), **override)


def test_cursor_refuses_any_alteration() -> None:
    token = issued()
    version, payload, mac = token.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded["no"] = 44
    forged = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
    with pytest.raises(CursorInvalid):
        read(f"{version}.{forged}.{mac}")
    with pytest.raises(CursorInvalid):
        read(token[:-2] + ("AA" if not token.endswith("AA") else "BB"))


@pytest.mark.parametrize(
    "token", ["", "abc", "a.b.c", "2.%%%.$$$", "9." + "x" * 2100 + ".y", "2..", "2.e30.x"]
)
def test_cursor_refuses_garbage(token: str) -> None:
    with pytest.raises(CursorInvalid):
        read(token)


@pytest.mark.parametrize(
    "change",
    [
        {"next_ordinal": 0},
        {"next_ordinal": 45},
        {"last_key": "2026-11-16T11:00:00.000Z#ffffffff-ffff-4fff-bfff-ffffffffffff"},
    ],
)
def test_cursor_refuses_positions_outside_its_snapshot(change: dict[str, Any]) -> None:
    with pytest.raises(CursorInvalid):
        read(issued(**change))


def test_cursor_signed_with_previous_key_survives_rotation_overlap() -> None:
    old = CursorKeys.from_secret(json.dumps({"version": "1", "key": "o" * 48}))
    rotated = CursorKeys.from_secret(
        json.dumps(
            {"version": "2", "key": "k" * 48, "previousVersion": "1", "previousKey": "o" * 48}
        )
    )
    token = issue_cursor(position(), keys=old, environment="dev", caller="sub-1")
    assert read(token, keys=rotated) == position()
    with pytest.raises(CursorInvalid):
        read(token, keys=KEYS)


@pytest.mark.parametrize(
    "secret",
    ["{}", '{"version":"1","key":"short"}', "not-json", '{"version":"","key":"' + "k" * 48 + '"}'],
)
def test_cursor_keys_require_strong_versioned_material(secret: str) -> None:
    with pytest.raises(ValueError):
        CursorKeys.from_secret(secret)


@pytest.mark.parametrize(
    "payload",
    [
        {"v": 1},
        {"v": 2, "exp": NOW + 10, "lim": 20, "sn": 5, "sk": "b", "no": 2, "lk": "a"},
        {"v": 1, "exp": "soon", "lim": 20, "sn": 5, "sk": "b", "no": 2, "lk": "a"},
        {"v": 1, "exp": NOW + 10, "lim": True, "sn": 5, "sk": "b", "no": 2, "lk": "a"},
        {"v": 1, "exp": NOW + 10, "lim": 20, "sn": 5, "sk": 7, "no": 2, "lk": "a"},
        [1, 2, 3],
    ],
)
def test_correctly_signed_but_malformed_payload_is_refused(payload: object) -> None:
    from raad_money import cursor

    encoded = cursor._b64(json.dumps(payload).encode())
    mac = cursor._mac(KEYS.key, KEYS.version, encoded, "dev", "sub-1")
    with pytest.raises(CursorInvalid):
        read(f"{KEYS.version}.{encoded}.{mac}")


def test_signed_payload_that_is_not_json_is_refused() -> None:
    from raad_money import cursor

    encoded = cursor._b64(b"\xff\xfe not json")
    mac = cursor._mac(KEYS.key, KEYS.version, encoded, "dev", "sub-1")
    with pytest.raises(CursorInvalid):
        read(f"{KEYS.version}.{encoded}.{mac}")


def test_cursor_secret_must_be_an_object_and_issued_tokens_stay_bounded() -> None:
    with pytest.raises(TypeError):
        CursorKeys.from_secret("[]")
    with pytest.raises(ValueError):
        issue_cursor(position(last_key="x" * 2_000), keys=KEYS, environment="dev", caller="s")


def test_cursor_carries_no_caller_or_environment_in_clear() -> None:
    token = issue_cursor(position(), keys=KEYS, environment="dev", caller="holder-secret-sub")
    payload = token.split(".")[1]
    clear = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    assert "holder-secret-sub" not in clear and "dev" not in clear


class FakeIndex:
    """Role indexes and canonical rows; `hidden` simulates an index projection that lags."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.hidden: set[str] = set()
        self.reveal_after_reads = 0
        self.reads = 0

    def add(
        self, transfer_id: str, sender: str, recipient: str, stamp: str, ordinals: tuple[int, int]
    ) -> None:
        self.rows[transfer_id] = {
            "transferId": transfer_id,
            "reference": "R-" + transfer_id[-12:].upper().replace("0", "2"),
            "senderWalletId": sender,
            "recipientWalletId": recipient,
            "amountHalalas": 100,
            "currency": "SAR",
            "completedAt": stamp,
            "completedSortKey": f"{stamp}#{transfer_id}",
            "senderOrdinal": ordinals[0],
            "recipientOrdinal": ordinals[1],
        }

    def query_role(
        self, wallet_id: str, role: str, upper_key: str, inclusive: bool, limit: int
    ) -> list[dict[str, Any]]:
        self.reads += 1
        if self.reveal_after_reads and self.reads > self.reveal_after_reads:
            self.hidden.clear()
        field = "senderWalletId" if role == "sender" else "recipientWalletId"
        found = [
            {"transferId": row["transferId"], "completedSortKey": row["completedSortKey"]}
            for row in self.rows.values()
            if row[field] == wallet_id
            and row["transferId"] not in self.hidden
            and (
                row["completedSortKey"] <= upper_key
                if inclusive
                else row["completedSortKey"] < upper_key
            )
        ]
        return sorted(found, key=lambda item: item["completedSortKey"], reverse=True)[:limit]

    def get_canonical(self, transfer_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {tid: dict(self.rows[tid]) for tid in transfer_ids if tid in self.rows}


def ledger_with(count: int, *, same_stamp: bool = False) -> tuple[FakeIndex, dict[str, Any]]:
    """`count` transfers alternating sent/received for WALLET; ordinals 1..count."""
    index = FakeIndex()
    last_key = ""
    for ordinal in range(1, count + 1):
        # wallet-b is the counterparty of every transfer, so its ordinal matches WALLET's.
        stamp = (
            "2026-11-16T08:00:00.000Z"
            if same_stamp
            else f"2026-11-16T08:{ordinal // 60:02d}:{ordinal % 60:02d}.000Z"
        )
        transfer_id = f"00000000-0000-4000-8000-{ordinal:012d}"
        if ordinal % 2:
            index.add(transfer_id, WALLET, "wallet-b", stamp, (ordinal, ordinal))
        else:
            index.add(transfer_id, "wallet-b", WALLET, stamp, (ordinal, ordinal))
        last_key = f"{stamp}#{transfer_id}"
    wallet = {"wallet_id": WALLET, "transfer_sequence": count, "last_completed_sort_key": last_key}
    return index, wallet


def walk(reader: HistoryReader, wallet: dict[str, Any], limit: int) -> list[list[str]]:
    pages: list[list[str]] = []
    result = reader.page(wallet, limit, None, NOW)
    pages.append([row["transfer_id"] for row in result.rows])
    while result.next_position is not None:
        result = reader.page(wallet, limit, result.next_position, NOW)
        pages.append([row["transfer_id"] for row in result.rows])
    return pages


def test_sent_and_received_merge_newest_first_with_ordinal_contiguity() -> None:
    index, wallet = ledger_with(7)
    pages = walk(HistoryReader(index, sleep=lambda _: None), wallet, 3)
    assert [len(page) for page in pages] == [3, 3, 1]
    flat = [tid for page in pages for tid in page]
    assert flat == sorted(index.rows, reverse=True)


def test_equal_timestamps_order_by_transfer_id_descending() -> None:
    index, wallet = ledger_with(5, same_stamp=True)
    pages = walk(HistoryReader(index, sleep=lambda _: None), wallet, 2)
    assert [tid for page in pages for tid in page] == sorted(index.rows, reverse=True)


def test_new_transfers_after_page_one_do_not_shift_the_chain() -> None:
    index, wallet = ledger_with(6)
    reader = HistoryReader(index, sleep=lambda _: None)
    first = reader.page(wallet, 4, None, NOW)
    index.add(
        "00000000-0000-4000-8000-000000000099",
        "wallet-b",
        WALLET,
        "2026-11-16T09:00:00.000Z",
        (99, 7),
    )
    assert first.next_position is not None
    second = reader.page(wallet, 4, first.next_position, NOW)
    seen = [row["transfer_id"] for row in first.rows + second.rows]
    assert len(seen) == len(set(seen)) == 6
    assert "00000000-0000-4000-8000-000000000099" not in seen
    assert second.next_position is None


def test_page_position_binds_snapshot_limit_and_expiry() -> None:
    index, wallet = ledger_with(5)
    first = HistoryReader(index, sleep=lambda _: None).page(wallet, 2, None, NOW)
    assert first.next_position == HistoryPosition(
        expires_at=NOW + CURSOR_LIFETIME_SECONDS,
        limit=2,
        snapshot_sequence=5,
        snapshot_key=wallet["last_completed_sort_key"],
        next_ordinal=3,
        last_key=index.rows["00000000-0000-4000-8000-000000000004"]["completedSortKey"],
    )


def test_empty_wallet_has_no_rows_and_no_cursor() -> None:
    result = HistoryReader(FakeIndex(), sleep=lambda _: None).page(
        {"wallet_id": WALLET, "transfer_sequence": 0}, 20, None, NOW
    )
    assert result.rows == [] and result.next_position is None


def test_lagging_index_is_reread_once_then_served_complete() -> None:
    index, wallet = ledger_with(4)
    index.hidden = {"00000000-0000-4000-8000-000000000004"}
    index.reveal_after_reads = 2
    sleeps: list[float] = []
    result = HistoryReader(index, sleep=sleeps.append).page(wallet, 20, None, NOW)
    assert [row["transfer_id"] for row in result.rows] == sorted(index.rows, reverse=True)
    assert len(sleeps) == 1


def test_persistent_gap_fails_safely_instead_of_returning_a_partial_page() -> None:
    index, wallet = ledger_with(4)
    index.hidden = {"00000000-0000-4000-8000-000000000002"}
    with pytest.raises(HistoryIntegrityError):
        HistoryReader(index, sleep=lambda _: None).page(wallet, 20, None, NOW)


def test_ordinal_mismatch_or_foreign_row_is_an_integrity_failure() -> None:
    index, wallet = ledger_with(3)
    index.rows["00000000-0000-4000-8000-000000000003"]["senderOrdinal"] = 9
    with pytest.raises(HistoryIntegrityError):
        HistoryReader(index, sleep=lambda _: None).page(wallet, 20, None, NOW)
    index, wallet = ledger_with(3)
    index.rows["00000000-0000-4000-8000-000000000003"]["senderWalletId"] = "wallet-z"
    with pytest.raises(HistoryIntegrityError):
        HistoryReader(index, sleep=lambda _: None).page(wallet, 20, None, NOW)


def test_missing_canonical_row_is_an_integrity_failure() -> None:
    index, wallet = ledger_with(2)
    original = index.get_canonical
    index.get_canonical = lambda ids: {
        k: v for k, v in original(ids).items() if not k.endswith("2")
    }  # type: ignore[method-assign]
    with pytest.raises(HistoryIntegrityError):
        HistoryReader(index, sleep=lambda _: None).page(wallet, 20, None, NOW)


def test_corrupt_canonical_amount_is_an_integrity_failure() -> None:
    index, wallet = ledger_with(2)
    index.rows["00000000-0000-4000-8000-000000000002"]["amountHalalas"] = 1.5
    with pytest.raises(HistoryIntegrityError):
        HistoryReader(index, sleep=lambda _: None).page(wallet, 20, None, NOW)


def test_unmigrated_wallet_history_fails_closed() -> None:
    with pytest.raises(HistoryIntegrityError):
        HistoryReader(FakeIndex(), sleep=lambda _: None).page({"wallet_id": WALLET}, 20, None, NOW)
    with pytest.raises(HistoryIntegrityError):
        HistoryReader(FakeIndex(), sleep=lambda _: None).page(
            {"wallet_id": WALLET, "transfer_sequence": 3}, 20, None, NOW
        )


@pytest.mark.parametrize("ordinal", [2.5, None, "x", True])
def test_non_integer_ordinal_is_an_integrity_failure(ordinal: object) -> None:
    index, wallet = ledger_with(2)
    index.rows["00000000-0000-4000-8000-000000000002"]["recipientOrdinal"] = ordinal
    with pytest.raises(HistoryIntegrityError):
        HistoryReader(index, sleep=lambda _: None).page(wallet, 20, None, NOW)


def test_rows_report_direction_inputs_and_amounts_as_integers() -> None:
    index, wallet = ledger_with(2)
    rows = HistoryReader(index, sleep=lambda _: None).page(wallet, 20, None, NOW).rows
    assert rows[0]["recipient_wallet_id"] == WALLET and rows[1]["sender_wallet_id"] == WALLET
    assert all(isinstance(row["amount_halalas"], int) for row in rows)
