"""C2 consumer tests (test-after): validation, dedup, quarantine, retry window and privacy."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest

from raad_money import consumer
from raad_money.consumer import EVENT_DETAIL_TYPE, EVENT_SOURCE, UserConfirmedConsumer
from raad_money.storage import EmailConflict

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
EVENT_ID = "0b6f6a9e-6d1e-4c2f-9a55-3f1d2e7c8b90"


def detail(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "specVersion": "1.0",
        "eventId": EVENT_ID,
        "occurredAt": "2026-11-16T11:59:00Z",
        "environment": "dev",
        "userId": "user-123",
        "email": "Person@Example.test",
        "displayName": "Person",
    }
    values.update(overrides)
    return values


def record(body: Any, message_id: str = "m-1") -> dict[str, Any]:
    if not isinstance(body, str):
        body = json.dumps(
            {"source": EVENT_SOURCE, "detail-type": EVENT_DETAIL_TYPE, "detail": body}
        )
    return {"messageId": message_id, "body": body}


def build(**ledger_values: Any) -> tuple[UserConfirmedConsumer, Mock, Mock, Mock, list[dict]]:
    ledger, directory, quarantine = Mock(), Mock(), Mock()
    ledger.get_event_receipt.return_value = None
    ledger.get_user_receipt.return_value = None
    ledger.ensure_wallet.return_value = ({"wallet_id": "w"}, True)
    ledger.record_receipt.return_value = "recorded"
    for name, value in ledger_values.items():
        setattr(ledger, name, value)
    logs: list[dict] = []
    worker = UserConfirmedConsumer(
        ledger,
        directory,
        environment="dev",
        quarantine=quarantine,
        clock=lambda: NOW,
        log=logs.append,
    )
    return worker, ledger, directory, quarantine, logs


def test_valid_event_runs_wallet_setup_then_records_receipt() -> None:
    worker, ledger, directory, quarantine, _ = build()
    assert worker.handle_record(record(detail())) == "recorded"
    ledger.ensure_wallet.assert_called_once_with("user-123")
    directory.ensure_holder.assert_called_once_with("user-123", "Person@Example.test", "Person")
    ledger.record_receipt.assert_called_once_with(EVENT_ID, "user-123", "dev")
    quarantine.assert_not_called()


def test_unknown_fields_and_later_minor_versions_are_accepted() -> None:
    worker, *_ = build()
    assert (
        worker.handle_record(record(detail(specVersion="1.7", futureField={"x": 1}))) == "recorded"
    )


def test_duplicate_delivery_is_a_no_op() -> None:
    worker, ledger, directory, quarantine, _ = build()
    ledger.get_event_receipt.return_value = {"user_id": "user-123"}
    assert worker.handle_record(record(detail())) == "duplicate"
    ledger.ensure_wallet.assert_not_called()
    directory.ensure_holder.assert_not_called()
    quarantine.assert_not_called()


def test_event_id_reused_for_another_user_is_quarantined() -> None:
    worker, ledger, _, quarantine, _ = build()
    ledger.get_event_receipt.return_value = {"user_id": "someone-else"}
    assert worker.handle_record(record(detail())) == "quarantined-conflict"
    ledger.ensure_wallet.assert_not_called()
    assert quarantine.call_args.args[1] == "conflict"


def test_later_distinct_event_for_initialized_user_is_a_no_op() -> None:
    worker, ledger, _, _, _ = build()
    ledger.get_user_receipt.return_value = {"event_id": "earlier"}
    assert worker.handle_record(record(detail())) == "already-initialized"
    ledger.ensure_wallet.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        detail(environment="prd"),
        detail(environment="qa"),
        detail(specVersion="2.0"),
        detail(eventId="not-a-uuid"),
        detail(eventId="0B6F6A9E6D1E4C2F9A553F1D2E7C8B90"),
        detail(occurredAt="2026-11-16T11:59:00"),
        detail(occurredAt="2026-11-16T13:00:00Z"),
        detail(occurredAt=17),
        detail(occurredAt="yesterday"),
        detail(userId=""),
        detail(userId=" padded "),
        detail(email="not-an-email"),
        detail(email=None),
        detail(displayName="   "),
        detail(displayName="x" * 201),
        {k: v for k, v in detail().items() if k != "userId"},
        "not json",
        json.dumps({"source": "other", "detail-type": EVENT_DETAIL_TYPE, "detail": detail()}),
        json.dumps({"source": EVENT_SOURCE, "detail-type": "Other", "detail": detail()}),
        json.dumps({"source": EVENT_SOURCE, "detail-type": EVENT_DETAIL_TYPE, "detail": "x"}),
        json.dumps([1, 2]),
    ],
)
def test_invalid_or_foreign_events_are_quarantined_immediately(body: Any) -> None:
    worker, ledger, _, quarantine, _ = build()
    assert worker.handle_record(record(body)) == "quarantined-invalid"
    ledger.ensure_wallet.assert_not_called()
    quarantine.assert_called_once()


def test_transient_failure_inside_window_is_retried_through_the_queue() -> None:
    worker, ledger, _, quarantine, logs = build()
    ledger.ensure_wallet.side_effect = TimeoutError("store busy")
    result = worker.handle_batch({"Records": [record(detail(), "m-9")]})
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-9"}]}
    quarantine.assert_not_called()
    assert logs[-1]["result"] == "retry"


def test_failure_after_the_24_hour_window_is_copied_to_dlq_and_acknowledged() -> None:
    worker, ledger, _, quarantine, _ = build()
    ledger.ensure_wallet.side_effect = TimeoutError("store busy")
    old = detail(occurredAt=(NOW - timedelta(hours=24, seconds=1)).isoformat())
    assert worker.handle_batch({"Records": [record(old)]}) == {"batchItemFailures": []}
    assert quarantine.call_args.args[1] == "expired"


def test_failed_quarantine_copy_is_retried_not_dropped() -> None:
    worker, _, _, quarantine, _ = build()
    quarantine.side_effect = RuntimeError("dlq unavailable")
    assert worker.handle_batch({"Records": [record("garbage", "m-2")]}) == {
        "batchItemFailures": [{"itemIdentifier": "m-2"}]
    }


def test_email_owned_by_another_user_is_quarantined_without_merge() -> None:
    worker, ledger, directory, quarantine, _ = build()
    directory.ensure_holder.side_effect = EmailConflict("conflict")
    assert worker.handle_record(record(detail())) == "quarantined-email-conflict"
    ledger.record_receipt.assert_not_called()
    assert quarantine.call_args.args[1] == "email-conflict"


def test_receipt_race_conflict_is_quarantined_and_duplicate_is_acknowledged() -> None:
    worker, ledger, _, quarantine, _ = build()
    ledger.record_receipt.return_value = "conflict"
    assert worker.handle_record(record(detail())) == "quarantined-conflict"
    ledger.record_receipt.return_value = "duplicate"
    assert worker.handle_record(record(detail())) == "duplicate"
    assert quarantine.call_count == 1


def test_logs_carry_event_id_and_result_but_no_personal_data() -> None:
    worker, *_, logs = build()
    worker.handle_record(record(detail()))
    worker.handle_record(record(detail(environment="prd")))
    rendered = json.dumps(logs)
    assert EVENT_ID in rendered
    for personal in ("user-123", "Person@Example.test", "person@example.test", '"Person"'):
        assert personal not in rendered


def test_lambda_entry_wires_environment_and_dlq(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "LEDGER_TABLE": "ledger",
        "DIRECTORY_TABLE": "directory",
        "TRANSACTIONS_TABLE": "transactions",
        "ENVIRONMENT": "dev",
        "PROCESSING_DLQ_URL": "https://sqs.example/processing-dlq",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    sqs = Mock()
    monkeypatch.setattr(consumer.boto3, "client", lambda name, **_: sqs)
    monkeypatch.setattr(consumer, "LedgerRepository", Mock())
    monkeypatch.setattr(consumer, "WalletDirectory", Mock())
    monkeypatch.setattr(consumer, "_consumer", None)
    assert consumer.handle({"Records": [record("garbage")]}, None) == {"batchItemFailures": []}
    kwargs = sqs.send_message.call_args.kwargs
    assert kwargs["QueueUrl"] == "https://sqs.example/processing-dlq"
    assert kwargs["MessageAttributes"]["reason"]["StringValue"] == "invalid"
