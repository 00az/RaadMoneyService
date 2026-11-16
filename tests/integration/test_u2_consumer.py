"""C2 consumer against DynamoDB Local: duplicate and simultaneous deliveries credit once."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from raad_money.consumer import EVENT_DETAIL_TYPE, EVENT_SOURCE, UserConfirmedConsumer
from raad_money.storage import LedgerRepository, WalletDirectory
from raad_money.wallet_setup import ensure_wallet_and_holder

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def message(event_id: str, user_id: str = "user-1", environment: str = "dev") -> dict[str, Any]:
    detail = {
        "specVersion": "1.0",
        "eventId": event_id,
        "occurredAt": "2026-11-16T11:59:00Z",
        "environment": environment,
        "userId": user_id,
        "email": f"{user_id}@example.test",
        "displayName": user_id.title(),
    }
    body = json.dumps({"source": EVENT_SOURCE, "detail-type": EVENT_DETAIL_TYPE, "detail": detail})
    return {"messageId": event_id, "body": body}


def worker(ledger_table: str, directory_table: str, environment: str = "dev"):
    ledger, directory = LedgerRepository(ledger_table), WalletDirectory(directory_table)
    parked: list[tuple[str, str]] = []
    consumer = UserConfirmedConsumer(
        ledger,
        directory,
        environment=environment,
        quarantine=lambda body, reason: parked.append((body, reason)),
        clock=lambda: NOW,
        log=lambda _: None,
    )
    return consumer, ledger, directory, parked


def test_simultaneous_duplicate_deliveries_open_one_wallet_with_one_credit(
    ledger_table: str, directory_table: str
) -> None:
    consumer, ledger, directory, parked = worker(ledger_table, directory_table)
    event = message("6a2f0f5e-0d8c-4c56-8b1e-6e0d7a1f2b3c")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: consumer.handle_batch({"Records": [event]}), range(6)))
    assert all(result == {"batchItemFailures": []} for result in results)
    wallet = ledger.get_wallet("user-1")
    assert wallet is not None and wallet["balance_halalas"] == 100_000
    assert directory.find_by_email("user-1@example.test")["holder_id"] == "user-1"
    assert parked == []
    assert consumer.handle_record(event) == "duplicate"


def test_distinct_events_for_one_user_and_overlapping_put_converge(
    ledger_table: str, directory_table: str
) -> None:
    consumer, ledger, directory, _ = worker(ledger_table, directory_table)
    first = message("11111111-1111-4111-8111-111111111111")
    second = message("22222222-2222-4222-8222-222222222222")

    def deliver(item: dict[str, Any]) -> object:
        if item is None:
            return ensure_wallet_and_holder(
                ledger, directory, "user-1", "user-1@example.test", "User-1"
            )
        return consumer.handle_batch({"Records": [item]})

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(deliver, [first, second, None]))
    wallet = ledger.get_wallet("user-1")
    assert wallet is not None and wallet["balance_halalas"] == 100_000
    assert ledger.get_user_receipt("user-1") is not None
    owner_rows = ledger.table.scan(ConsistentRead=True)["Items"]
    assert sum(1 for row in owner_rows if row.get("entity") == "wallet") == 1


def test_event_from_another_environment_opens_nothing(
    ledger_table: str, directory_table: str
) -> None:
    consumer, ledger, _, parked = worker(ledger_table, directory_table, environment="stg")
    assert consumer.handle_record(message("33333333-3333-4333-8333-333333333333")) == (
        "quarantined-invalid"
    )
    assert ledger.get_wallet("user-1") is None
    assert [reason for _, reason in parked] == ["invalid"]


def test_event_id_reused_for_another_user_is_refused_after_first_pair(
    ledger_table: str, directory_table: str
) -> None:
    consumer, ledger, _, parked = worker(ledger_table, directory_table)
    event_id = "44444444-4444-4444-8444-444444444444"
    assert consumer.handle_record(message(event_id, "user-1")) == "recorded"
    assert consumer.handle_record(message(event_id, "user-2")) == "quarantined-conflict"
    assert ledger.get_wallet("user-2") is None
    assert [reason for _, reason in parked] == ["conflict"]
    assert ledger.record_receipt(event_id, "user-3", "dev") == "conflict"
    assert ledger.record_receipt(event_id, "user-1", "dev") == "duplicate"
