"""C2 UserConfirmed consumer: SQS work queue fed by the environment's EventBridge rule.

Valid events run the same idempotent wallet setup as PUT /wallet, then record the receipt.
Invalid, wrong-environment or conflicting events are quarantined to the processing DLQ at
once. Transient failures are retried until 24 hours after the event occurred, then copied
to the processing DLQ and acknowledged. Logs carry the event ID and result only.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import boto3

from raad_money.email_address import normalize_email
from raad_money.storage import EmailConflict, LedgerRepository, WalletDirectory
from raad_money.wallet_setup import ensure_wallet_and_holder

# Every C2 value this service depends on lives here; the stack's EventBridge rule imports them.
# C2 is owned by U4 in `Raad`; this repo pins a copy (contracts/pins.yaml) and
# tests/unit/test_c2_contract.py fails if these differ from the pinned copy's `x-eventbridge`
# mapping and `UserConfirmedPayload.required` list.
EVENT_SOURCE = "raad.identity"
EVENT_DETAIL_TYPE = "raad.identity.user-confirmed.v1"
REQUIRED_PAYLOAD_FIELDS = (
    "specVersion",
    "eventId",
    "occurredAt",
    "environment",
    "userId",
    "email",
    "displayName",
)
DELIVERY_WINDOW = timedelta(hours=24)
_MAX_FUTURE_SKEW = timedelta(minutes=5)
_SPEC_VERSION = re.compile(r"1\.[0-9]+")
_ENVIRONMENTS = {"dev", "stg", "prd"}


class InvalidEvent(ValueError):
    """The message can never be processed by this consumer; quarantine it."""


@dataclass(frozen=True, slots=True)
class UserConfirmed:
    event_id: str
    occurred_at: datetime
    environment: str
    user_id: str
    email: str
    display_name: str


def parse_event(envelope: Any, *, environment: str, now: datetime) -> UserConfirmed:
    """Validate source, type and environment first, then the C2 payload; ignore unknown fields."""
    if not isinstance(envelope, dict):
        raise InvalidEvent("event envelope must be an object")
    if envelope.get("source") != EVENT_SOURCE or envelope.get("detail-type") != EVENT_DETAIL_TYPE:
        raise InvalidEvent("unexpected event source or type")
    detail = envelope.get("detail")
    if not isinstance(detail, dict):
        raise InvalidEvent("event detail must be an object")
    if detail.get("environment") not in _ENVIRONMENTS or detail["environment"] != environment:
        raise InvalidEvent("event belongs to another environment")
    if any(name not in detail for name in REQUIRED_PAYLOAD_FIELDS):
        raise InvalidEvent("a required C2 field is missing")
    spec_version = detail.get("specVersion")
    if not isinstance(spec_version, str) or not _SPEC_VERSION.fullmatch(spec_version):
        raise InvalidEvent("unsupported specVersion")
    event_id = _uuid(detail.get("eventId"))
    occurred_at = _instant(detail.get("occurredAt"))
    if occurred_at > now + _MAX_FUTURE_SKEW:
        raise InvalidEvent("occurredAt is in the future")
    user_id = detail.get("userId")
    if not isinstance(user_id, str) or not 1 <= len(user_id) <= 128 or user_id != user_id.strip():
        raise InvalidEvent("userId is invalid")
    email = detail.get("email")
    try:
        normalize_email(email)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise InvalidEvent("email is invalid") from error
    display_name = detail.get("displayName")
    if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 200:
        raise InvalidEvent("displayName is invalid")
    assert isinstance(email, str)
    return UserConfirmed(event_id, occurred_at, environment, user_id, email, display_name.strip())


def _uuid(value: object) -> str:
    try:
        parsed = UUID(str(value))
    except ValueError as error:
        raise InvalidEvent("eventId is not a UUID") from error
    if not isinstance(value, str) or str(parsed) != value.lower():
        raise InvalidEvent("eventId is not a canonical UUID")
    return value.lower()


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise InvalidEvent("occurredAt is required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidEvent("occurredAt is not an instant") from error
    if parsed.tzinfo is None:
        raise InvalidEvent("occurredAt needs an offset")
    return parsed.astimezone(UTC)


class UserConfirmedConsumer:
    def __init__(
        self,
        ledger: LedgerRepository,
        directory: WalletDirectory,
        *,
        environment: str,
        quarantine: Callable[[str, str], None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        log: Callable[[dict[str, str]], None] = lambda entry: print(json.dumps(entry)),
    ) -> None:
        self._ledger = ledger
        self._directory = directory
        self._environment = environment
        self._quarantine = quarantine
        self._clock = clock
        self._log = log

    def handle_batch(self, event: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
        """SQS partial-batch response: failed records return to the queue for retry."""
        failures: list[dict[str, str]] = []
        for record in event.get("Records", []):
            try:
                self.handle_record(record)
            except Exception:  # noqa: BLE001 - one record's failure must not ack the others
                failures.append({"itemIdentifier": str(record.get("messageId", ""))})
        return {"batchItemFailures": failures}

    def handle_record(self, record: dict[str, Any]) -> str:
        body = str(record.get("body", ""))
        now = self._clock()
        try:
            event = parse_event(json.loads(body), environment=self._environment, now=now)
        except (InvalidEvent, json.JSONDecodeError):
            return self._park(body, "invalid", None)
        receipt = self._ledger.get_event_receipt(event.event_id)
        if receipt is not None:
            if receipt.get("user_id") != event.user_id:
                return self._park(body, "conflict", event.event_id)
            return self._done("duplicate", event.event_id)
        if self._ledger.get_user_receipt(event.user_id) is not None:
            return self._done("already-initialized", event.event_id)
        try:
            ensure_wallet_and_holder(
                self._ledger, self._directory, event.user_id, event.email, event.display_name
            )
            outcome = self._ledger.record_receipt(event.event_id, event.user_id, self._environment)
        except EmailConflict:
            return self._park(body, "email-conflict", event.event_id)
        except Exception:
            if now >= event.occurred_at + DELIVERY_WINDOW:
                return self._park(body, "expired", event.event_id)
            self._log(self._entry("retry", event.event_id))
            raise
        if outcome == "conflict":
            return self._park(body, "conflict", event.event_id)
        return self._done(outcome, event.event_id)

    def _park(self, body: str, reason: str, event_id: str | None) -> str:
        # Copy durably first; if the copy fails the message is retried, never dropped.
        self._quarantine(body, reason)
        self._log(self._entry(f"quarantined-{reason}", event_id))
        return f"quarantined-{reason}"

    def _done(self, result: str, event_id: str) -> str:
        self._log(self._entry(result, event_id))
        return result

    def _entry(self, result: str, event_id: str | None) -> dict[str, str]:
        entry = {
            "component": "user-confirmed-consumer",
            "environment": self._environment,
            "result": result,
        }
        if event_id is not None:
            entry["eventId"] = event_id
        return entry


_consumer: UserConfirmedConsumer | None = None


def _sqs_quarantine(queue_url: str) -> Callable[[str, str], None]:
    client = boto3.client("sqs")

    def send(body: str, reason: str) -> None:
        client.send_message(
            QueueUrl=queue_url,
            MessageBody=body,
            MessageAttributes={"reason": {"DataType": "String", "StringValue": reason}},
        )

    return send


def handle(event: dict[str, Any], _context: object) -> dict[str, list[dict[str, str]]]:
    global _consumer
    if _consumer is None:
        _consumer = UserConfirmedConsumer(
            LedgerRepository(os.environ["LEDGER_TABLE"]),
            WalletDirectory(os.environ["DIRECTORY_TABLE"]),
            environment=os.environ["ENVIRONMENT"],
            quarantine=_sqs_quarantine(os.environ["PROCESSING_DLQ_URL"]),
        )
    return _consumer.handle_batch(event)
