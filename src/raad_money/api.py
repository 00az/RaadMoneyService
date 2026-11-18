"""C1 HTTP API handler; safe wire errors and caller identity from verified claims only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from typing import Any
from urllib.parse import parse_qs

import boto3

from raad_money.cursor import CursorInvalid, CursorKeys, issue_cursor, read_cursor
from raad_money.email_address import normalize_email
from raad_money.history import HistoryReader, TransactionsIndex
from raad_money.identity import Caller, TokenVerifier
from raad_money.ledger import IdempotencyKeyReused, TransferOutcome, TransferRequest
from raad_money.storage import (
    EmailConflict,
    LedgerRepository,
    ProfileVersionMissing,
    RecipientMissing,
    WalletDirectory,
    WalletMissing,
    display_email,
)
from raad_money.transfers import TransferResponse, TransferService
from raad_money.wallet_setup import ensure_wallet_and_holder

_verifier: TokenVerifier | None = None
_ledger: LedgerRepository | None = None
_directory: WalletDirectory | None = None
_secrets: Any | None = None

_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._~-]{16,128}")
_LIMIT_PATTERN = re.compile(r"[1-9][0-9]{0,2}")
_MAX_BODY_CHARACTERS = 8_192
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100


class _Refusal(Exception):
    """A safe C1 error response decided inside a route."""

    def __init__(self, status: int, code: str, message: str, replayed: bool | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.message, self.replayed = status, code, message, replayed


_INVALID = (400, "VALIDATION_ERROR", "The request is invalid.")
_NO_WALLET = (404, "WALLET_NOT_FOUND", "Set up your wallet to continue.")
_REFUSAL_MESSAGES = {
    "RECIPIENT_NOT_FOUND": "The recipient could not be resolved.",
    "SELF_TRANSFER": "Choose a different recipient.",
    "AMOUNT_OUT_OF_RANGE": "Transfers must be from 1.00 to 500.00 SAR.",
    "INSUFFICIENT_FUNDS": "There is not enough balance for this transfer.",
}


def _secrets_client() -> Any:
    """Create the AWS client on demand so local imports do not resolve credentials."""
    global _secrets
    if _secrets is None:
        _secrets = boto3.client("secretsmanager")
    return _secrets


def _dependencies() -> tuple[TokenVerifier, LedgerRepository, WalletDirectory]:
    global _verifier, _ledger, _directory
    if _verifier is None:
        _verifier = TokenVerifier.from_environment()
        _ledger = LedgerRepository(os.environ["LEDGER_TABLE"])
        _directory = WalletDirectory(os.environ["DIRECTORY_TABLE"])
    assert _ledger is not None and _directory is not None
    return _verifier, _ledger, _directory


def _transfer_service(ledger: LedgerRepository, directory: WalletDirectory) -> TransferService:
    return TransferService(ledger, directory)


def _history_reader(ledger: LedgerRepository) -> HistoryReader:
    return HistoryReader(TransactionsIndex(ledger.transactions))


def _cursor_keys() -> CursorKeys:
    secret = _secrets_client().get_secret_value(SecretId=os.environ["CURSOR_SECRET_ARN"])
    return CursorKeys.from_secret(secret["SecretString"])


def _response(
    status: int, payload: dict[str, Any], correlation: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    result_headers = {
        "content-type": "application/json",
        "cache-control": "no-store",
        "x-correlation-id": correlation,
    }
    if headers:
        result_headers.update(headers)
    return {
        "statusCode": status,
        "headers": result_headers,
        "body": json.dumps(payload, separators=(",", ":")),
    }


def _error(
    status: int, code: str, message: str, correlation: str, replayed: bool | None = None
) -> dict[str, Any]:
    extra = {"retry-after": "1"} if status in {429, 503} else {}
    if replayed is not None:
        extra["idempotency-replayed"] = "true" if replayed else "false"
    return _response(
        status, {"code": code, "message": message, "correlationId": correlation}, correlation, extra
    )


def _restore_forwarded(event: dict[str, Any]) -> dict[str, Any]:
    """Only an IAM-authorized Function URL event may carry the edge envelope."""
    iam = event.get("requestContext", {}).get("authorizer", {}).get("iam", {})
    if not iam.get("userArn"):
        if os.environ.get("REQUIRE_FORWARDING_ENVELOPE") == "true":
            raise PermissionError("IAM forwarding required")
        return event
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body, validate=True).decode()
    envelope = json.loads(body)
    if set(envelope) != {"version", "request"} or envelope["version"] != 1:
        raise ValueError("invalid forwarding envelope")
    request = envelope["request"]
    if set(request) != {"method", "path", "query", "headers", "body"}:
        raise ValueError("invalid forwarded request")
    if not isinstance(request["headers"], dict) or not isinstance(
        request["body"], (str, type(None))
    ):
        raise TypeError("invalid forwarded payload")
    return {
        "rawPath": request["path"],
        "rawQueryString": request["query"],
        "headers": request["headers"],
        "body": request["body"],
        "requestContext": {"http": {"method": request["method"]}},
    }


def _header(headers: dict[str, Any], name: str) -> str:
    value = next((value for key, value in headers.items() if key.lower() == name), "")
    return value if isinstance(value, str) else ""


def _log_protected_correlation(correlation: str) -> None:
    secret_value = json.loads(
        _secrets_client().get_secret_value(SecretId=os.environ["CORRELATION_SECRET_ARN"])[
            "SecretString"
        ]
    )
    secret_version = str(secret_value.get("version", "1"))
    hmac_key = secret_value.get("key") or secret_value.get("material", "")
    if not hmac_key:
        raise ValueError("correlation key unavailable")
    protected = hmac.new(hmac_key.encode(), correlation.encode(), hashlib.sha256).hexdigest()
    print(
        json.dumps(
            {
                "component": "money-api",
                "operation": "request",
                "result": "authenticated",
                "correlationKeyVersion": secret_version,
                "correlationKey": protected,
            }
        )
    )


def _handle_request(event: dict[str, Any], _context: object) -> dict[str, Any]:
    """Dispatch C1 paths with strict caller and payload handling."""
    headers = event.get("headers") or {}
    correlation = _header(headers, "x-correlation-id")
    if not _CORRELATION_PATTERN.fullmatch(correlation):
        correlation = uuid.uuid4().hex
    authorization = _header(headers, "authorization")
    if not authorization.startswith("Bearer "):
        return _error(401, "AUTHENTICATION_REQUIRED", "Sign in to continue.", correlation)
    try:
        verifier, ledger, directory = _dependencies()
        caller = verifier.authenticate(authorization[7:])
    except PermissionError:
        return _error(401, "AUTHENTICATION_REQUIRED", "Sign in to continue.", correlation)
    except Exception:  # noqa: BLE001 - Authentication boundary must disclose no provider details.
        return _error(
            503, "TOKEN_VERIFIER_UNAVAILABLE", "Sign in is temporarily unavailable.", correlation
        )
    try:
        _log_protected_correlation(correlation)
    except Exception:  # noqa: BLE001 - Secret boundary must return safe errors without SDK payloads.
        return _error(
            503, "SERVICE_UNAVAILABLE", "The service is temporarily unavailable.", correlation
        )

    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    path = event.get("rawPath", "/")
    raw_body = event.get("body") or "{}"
    try:
        if not isinstance(raw_body, str) or len(raw_body) > _MAX_BODY_CHARACTERS:
            raise ValueError("body too large")
        body = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _error(*_INVALID, correlation)
    if not isinstance(body, dict) or (method in {"GET", "PUT"} and body):
        return _error(*_INVALID, correlation)
    try:
        route = _ROUTES.get((method, path))
        if route is None:
            return _error(500, "INTERNAL_ERROR", "The request could not be completed.", correlation)
        return route(_Request(caller, ledger, directory, body, event, correlation))
    except _Refusal as refusal:
        return _error(refusal.status, refusal.code, refusal.message, correlation, refusal.replayed)
    except EmailConflict:
        return _error(409, "VALIDATION_ERROR", "Wallet setup could not be completed.", correlation)
    except Exception:  # noqa: BLE001 - Storage boundary must disclose no SDK or personal data.
        return _error(
            503, "SERVICE_UNAVAILABLE", "The service is temporarily unavailable.", correlation
        )


class _Request:
    def __init__(
        self,
        caller: Caller,
        ledger: LedgerRepository,
        directory: WalletDirectory,
        body: dict[str, Any],
        event: dict[str, Any],
        correlation: str,
    ) -> None:
        self.caller, self.ledger, self.directory = caller, ledger, directory
        self.body, self.event, self.correlation = body, event, correlation


def _put_wallet(request: _Request) -> dict[str, Any]:
    caller = request.caller
    wallet, created = ensure_wallet_and_holder(
        request.ledger, request.directory, caller.holder_id, caller.email, caller.display_name
    )
    return _response(201 if created else 200, _wallet_response(wallet), request.correlation)


def _get_wallet(request: _Request) -> dict[str, Any]:
    wallet = request.ledger.get_wallet(request.caller.holder_id)
    if not wallet:
        raise _Refusal(*_NO_WALLET)
    return _response(200, _wallet_response(wallet), request.correlation)


def _resolve_recipient(request: _Request) -> dict[str, Any]:
    if set(request.body) != {"email"}:
        raise _Refusal(*_INVALID)
    try:
        email = normalize_email(request.body["email"])
    except (TypeError, ValueError):
        raise _Refusal(*_INVALID) from None
    try:
        recipient = request.directory.find_by_email(email)
    except RecipientMissing:
        raise _Refusal(
            404, "RECIPIENT_NOT_FOUND", _REFUSAL_MESSAGES["RECIPIENT_NOT_FOUND"]
        ) from None
    if recipient["holder_id"] == request.caller.holder_id:
        raise _Refusal(409, "SELF_TRANSFER", _REFUSAL_MESSAGES["SELF_TRANSFER"])
    return _response(
        200,
        {"email": display_email(recipient), "displayName": recipient["display_name"]},
        request.correlation,
    )


def _create_transfer(request: _Request) -> dict[str, Any]:
    if set(request.body) != {"recipientEmail", "amountHalalas"}:
        raise _Refusal(*_INVALID)
    key = _header(request.event.get("headers") or {}, "idempotency-key")
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise _Refusal(*_INVALID)
    try:
        transfer = TransferRequest(request.body["recipientEmail"], request.body["amountHalalas"])
    except ValueError:
        raise _Refusal(*_INVALID) from None
    service = _transfer_service(request.ledger, request.directory)
    try:
        result = service.submit(request.caller.holder_id, key, transfer)
    except WalletMissing:
        raise _Refusal(*_NO_WALLET) from None
    except IdempotencyKeyReused:
        raise _Refusal(
            409,
            "IDEMPOTENCY_KEY_REUSED",
            "This transfer key was already used for a different transfer.",
            replayed=False,
        ) from None
    return _transfer_response(result, request)


def _transfer_response(result: TransferResponse, request: _Request) -> dict[str, Any]:
    outcome: TransferOutcome = result.outcome
    if outcome.code is not None:
        raise _Refusal(
            outcome.status, outcome.code, _REFUSAL_MESSAGES[outcome.code], result.replayed
        )
    assert outcome.recipient_profile_version_id is not None
    version = request.directory.get_profile_version(outcome.recipient_profile_version_id)
    if version is None:
        # Never rebuild a saved result from today's profile; investigate instead.
        raise ProfileVersionMissing("saved outcome references a missing profile version")
    payload = {
        "transferId": outcome.transfer_id,
        "reference": outcome.reference,
        "amount": _money(int(outcome.amount_halalas or 0)),
        "balanceAfter": _money(int(outcome.balance_after_halalas or 0)),
        "recipient": {"email": version["email"], "displayName": version["display_name"]},
        "completedAt": outcome.completed_at,
    }
    replayed = {"idempotency-replayed": "true" if result.replayed else "false"}
    return _response(201, payload, request.correlation, replayed)


def _list_transfers(request: _Request) -> dict[str, Any]:
    limit, cursor = _history_query(str(request.event.get("rawQueryString") or ""))
    wallet = request.ledger.get_wallet(request.caller.holder_id)
    if not wallet:
        raise _Refusal(*_NO_WALLET)
    environment, caller_id, now = (
        os.environ["ENVIRONMENT"],
        request.caller.holder_id,
        int(time.time()),
    )
    keys: CursorKeys | None = None
    position = None
    if cursor is not None:
        keys = _cursor_keys()
        try:
            position = read_cursor(
                cursor, keys=keys, environment=environment, caller=caller_id, limit=limit, now=now
            )
        except CursorInvalid:
            raise _Refusal(
                400, "CURSOR_INVALID_OR_EXPIRED", "Reload transfer history to continue."
            ) from None
    page = _history_reader(request.ledger).page(wallet, limit, position, now)
    next_cursor = None
    if page.next_position is not None:
        keys = keys or _cursor_keys()
        next_cursor = issue_cursor(
            page.next_position, keys=keys, environment=environment, caller=caller_id
        )
    entries = _history_entries(request, wallet["wallet_id"], page.rows)
    return _response(
        200,
        {"items": entries, "nextCursor": next_cursor, "hasMore": next_cursor is not None},
        request.correlation,
    )


def _history_query(raw: str) -> tuple[int, str | None]:
    try:
        query = parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw))
    except ValueError:
        raise _Refusal(*_INVALID) from None
    if set(query) - {"limit", "cursor"} or any(len(values) != 1 for values in query.values()):
        raise _Refusal(*_INVALID)
    raw_limit = query.get("limit", [str(_DEFAULT_PAGE_SIZE)])[0]
    if not _LIMIT_PATTERN.fullmatch(raw_limit) or int(raw_limit) > _MAX_PAGE_SIZE:
        raise _Refusal(*_INVALID)
    cursor = query.get("cursor", [None])[0]
    if cursor is not None and not 1 <= len(cursor) <= 2048:
        raise _Refusal(*_INVALID)
    return int(raw_limit), cursor


def _history_entries(
    request: _Request, wallet_id: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    sent_flags = [row["sender_wallet_id"] == wallet_id for row in rows]
    counterparty_ids = [
        row["recipient_wallet_id"] if sent else row["sender_wallet_id"]
        for row, sent in zip(rows, sent_flags, strict=True)
    ]
    wallets = request.ledger.get_wallets_by_ids(counterparty_ids) if rows else {}
    holders = (
        request.directory.get_many([item["owner_id"] for item in wallets.values()]) if rows else {}
    )
    entries = []
    for row, sent, other_wallet_id in zip(rows, sent_flags, counterparty_ids, strict=True):
        other = holders.get(wallets.get(other_wallet_id, {}).get("owner_id", ""))
        if not other:
            raise RuntimeError("history counterparty profile is unavailable")
        entries.append(
            {
                "transferId": row["transfer_id"],
                "reference": row["reference"],
                "amount": _money(row["amount_halalas"]),
                "direction": "sent" if sent else "received",
                "otherParty": {"displayName": other["display_name"], "email": display_email(other)},
                "completedAt": row["completed_at"],
            }
        )
    return entries


_ROUTES = {
    ("PUT", "/money/v1/wallet"): _put_wallet,
    ("GET", "/money/v1/wallet"): _get_wallet,
    ("POST", "/money/v1/recipients/resolve"): _resolve_recipient,
    ("POST", "/money/v1/transfers"): _create_transfer,
    ("GET", "/money/v1/transfers"): _list_transfers,
}


def _wallet_response(wallet: dict[str, Any]) -> dict[str, Any]:
    return {"balance": _money(wallet["balance_halalas"])}


def _money(amount_halalas: int) -> dict[str, Any]:
    return {"amountHalalas": amount_halalas, "currency": "SAR"}


def handle(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Instrument one request using bounded dimensions and UTC-independent elapsed time."""
    started = time.monotonic()
    if os.environ.get("CANONICAL_WRITER_ACTIVE") == "false":
        return _error(
            503, "SERVICE_UNAVAILABLE", "The service is temporarily unavailable.", uuid.uuid4().hex
        )
    try:
        event = _restore_forwarded(event)
    except (ValueError, TypeError, KeyError, PermissionError):
        return _error(401, "AUTHENTICATION_REQUIRED", "Sign in to continue.", uuid.uuid4().hex)
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "").upper()
    try:
        response = _handle_request(event, context)
    except Exception:  # noqa: BLE001 - Final HTTP boundary always returns a safe C1 response.
        correlation = _header(event.get("headers") or {}, "x-correlation-id")
        response = _error(
            503,
            "SERVICE_UNAVAILABLE",
            "The service is temporarily unavailable.",
            correlation if _CORRELATION_PATTERN.fullmatch(correlation) else uuid.uuid4().hex,
        )
    _emit_metrics(method, path, int(response.get("statusCode", 500)), started)
    return response


def _emit_metrics(method: str, path: str, status: int, started: float) -> None:
    operation = {
        ("PUT", "/money/v1/wallet"): "wallet_setup",
        ("GET", "/money/v1/wallet"): "balance",
        ("POST", "/money/v1/recipients/resolve"): "recipient_lookup",
        ("POST", "/money/v1/transfers"): "transfer",
        ("GET", "/money/v1/transfers"): "history",
    }.get((method, path), "other")
    result = (
        "success"
        if 200 <= status < 300
        else "business_refusal"
        if status in {404, 409, 422}
        else "technical_failure"
        if status >= 500
        else "rejected"
    )
    values: dict[str, int | float] = {
        "MoneyApiRequests": 1,
        "MoneyApiDurationMs": max(0, int((time.monotonic() - started) * 1000)),
    }
    if operation == "transfer" and result == "success":
        values["MoneyApiTransferSuccesses"] = 1
    if operation == "transfer" and result == "technical_failure":
        values["MoneyApiTechnicalTransferFailures"] = 1
    if result == "technical_failure":
        values["MoneyApiTechnicalFailures"] = 1
    metrics = [
        {"Name": name, "Unit": "Milliseconds" if name.endswith("Ms") else "Count"}
        for name in values
    ]
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "Raad/MoneyApi",
                            "Dimensions": [["Environment", "Operation", "Result"], ["Environment"]],
                            "Metrics": metrics,
                        }
                    ],
                },
                "Environment": os.environ.get("ENVIRONMENT", "unknown"),
                "Operation": operation,
                "Result": result,
                **values,
            }
        )
    )
