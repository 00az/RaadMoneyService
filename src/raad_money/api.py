"""C1 HTTP API handler; safe wire errors and caller identity from verified claims only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any
from urllib.parse import parse_qs

import boto3

from raad_money.identity import TokenVerifier
from raad_money.ledger import InsufficientFunds
from raad_money.storage import EmailConflict, LedgerRepository, RecipientMissing, WalletDirectory

_verifier: TokenVerifier | None = None
_ledger: LedgerRepository | None = None
_directory: WalletDirectory | None = None
_secrets: Any | None = None


def _secrets_client() -> Any:
    """Create the AWS client on demand so local imports do not resolve credentials."""
    global _secrets
    if _secrets is None:
        _secrets = boto3.client("secretsmanager")
    return _secrets


_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _dependencies() -> tuple[TokenVerifier, LedgerRepository, WalletDirectory]:
    global _verifier, _ledger, _directory
    if _verifier is None:
        _verifier = TokenVerifier.from_environment()
        _ledger = LedgerRepository(os.environ["LEDGER_TABLE"])
        _directory = WalletDirectory(os.environ["DIRECTORY_TABLE"])
    assert _ledger is not None and _directory is not None
    return _verifier, _ledger, _directory


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


def _error(status: int, code: str, message: str, correlation: str) -> dict[str, Any]:
    extra = {"retry-after": "1"} if status in {429, 503} else None
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


def _handle_request(event: dict[str, Any], _context: object) -> dict[str, Any]:
    """Dispatch C1 walking-skeleton paths with strict caller and payload handling."""
    headers = event.get("headers") or {}
    correlation = next(
        (value for key, value in headers.items() if key.lower() == "x-correlation-id"), ""
    )
    if not _CORRELATION_PATTERN.fullmatch(correlation):
        correlation = __import__("uuid").uuid4().hex
    authorization = next(
        (value for key, value in headers.items() if key.lower() == "authorization"), ""
    )
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
        secret_value = json.loads(
            _secrets_client().get_secret_value(SecretId=os.environ["CORRELATION_SECRET_ARN"])[
                "SecretString"
            ]
        )
        secret_version = str(secret_value.get("version", "1"))
        hmac_key = secret_value.get("key") or secret_value.get("material", "")
        if not hmac_key:
            raise ValueError("correlation key unavailable")
        protected_correlation = hmac.new(
            hmac_key.encode(), correlation.encode(), hashlib.sha256
        ).hexdigest()
        print(
            json.dumps(
                {
                    "component": "money-api",
                    "operation": "request",
                    "result": "authenticated",
                    "correlationKeyVersion": secret_version,
                    "correlationKey": protected_correlation,
                }
            )
        )
    except Exception:  # noqa: BLE001 - Secret boundary must return safe errors without SDK payloads.
        return _error(
            503, "SERVICE_UNAVAILABLE", "The service is temporarily unavailable.", correlation
        )

    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    path = event.get("rawPath", "/")
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _error(400, "VALIDATION_ERROR", "The request is invalid.", correlation)
    if not isinstance(body, dict):
        return _error(400, "VALIDATION_ERROR", "The request is invalid.", correlation)
    if method in {"GET", "PUT"} and body:
        return _error(400, "VALIDATION_ERROR", "The request is invalid.", correlation)
    try:
        wallet: dict[str, Any] | None
        if path == "/money/v1/wallet" and method == "PUT":
            if body:
                return _error(400, "VALIDATION_ERROR", "The request is invalid.", correlation)
            wallet, created = ledger.ensure_wallet(caller.holder_id)
            directory.ensure_holder(caller.holder_id, caller.email, caller.display_name)
            return _response(201 if created else 200, _wallet_response(wallet), correlation)
        wallet = ledger.get_wallet(caller.holder_id)
        if path == "/money/v1/wallet" and method == "GET":
            return (
                _response(200, _wallet_response(wallet), correlation)
                if wallet
                else _error(404, "WALLET_NOT_FOUND", "Set up your wallet to continue.", correlation)
            )
        if path == "/money/v1/recipients/resolve" and method == "POST":
            if set(body) != {"email"}:
                return _error(400, "VALIDATION_ERROR", "The request is invalid.", correlation)
            recipient = directory.find_by_email(body.get("email", ""))
            if recipient["holder_id"] == caller.holder_id:
                return _error(409, "SELF_TRANSFER", "Choose a different recipient.", correlation)
            return _response(
                200,
                {"email": recipient["email_normalized"], "displayName": recipient["display_name"]},
                correlation,
            )
        if path == "/money/v1/transfers" and method == "POST":
            if set(body) != {"recipientEmail", "amountHalalas"}:
                return _error(400, "VALIDATION_ERROR", "The request is invalid.", correlation)
            idempotency_key = next(
                (value for key, value in headers.items() if key.lower() == "idempotency-key"), ""
            )
            if (
                len(idempotency_key) < 16
                or len(idempotency_key) > 128
                or not re.fullmatch(r"[A-Za-z0-9._~-]+", idempotency_key)
            ):
                return _error(400, "VALIDATION_ERROR", "The request is invalid.", correlation)
            if not wallet:
                return _error(
                    404, "WALLET_NOT_FOUND", "Set up your wallet to continue.", correlation
                )
            recipient = directory.find_by_email(body.get("recipientEmail", ""))
            if recipient["holder_id"] == caller.holder_id:
                return _error(409, "SELF_TRANSFER", "Choose a different recipient.", correlation)
            recipient_wallet = ledger.get_wallet(recipient["holder_id"])
            if not recipient_wallet:
                return _error(
                    404, "RECIPIENT_NOT_FOUND", "The recipient could not be resolved.", correlation
                )
            transfer = ledger.transfer(wallet, recipient_wallet, body["amountHalalas"])
            result = {
                "transferId": transfer["transfer_id"],
                "reference": transfer["reference"],
                "amount": _money(transfer["amount_halalas"]),
                "balanceAfter": _money(transfer["sender_balance_halalas"]),
                "recipient": {
                    "email": recipient["email_normalized"],
                    "displayName": recipient["display_name"],
                },
                "completedAt": transfer["completed_at"],
            }
            return _response(201, result, correlation)
        if path == "/money/v1/transfers" and method == "GET":
            if not wallet:
                return _error(
                    404, "WALLET_NOT_FOUND", "Set up your wallet to continue.", correlation
                )
            query = parse_qs(event.get("rawQueryString", ""))
            if set(query) - {"limit", "cursor"}:
                return _error(400, "VALIDATION_ERROR", "The request is invalid.", correlation)
            if "cursor" in query:
                return _error(
                    400,
                    "CURSOR_INVALID_OR_EXPIRED",
                    "Reload transfer history to continue.",
                    correlation,
                )
            try:
                limit = int(query.get("limit", ["20"])[0])
            except ValueError:
                return _error(400, "VALIDATION_ERROR", "The request is invalid.", correlation)
            if not 1 <= limit <= 100:
                return _error(400, "VALIDATION_ERROR", "The request is invalid.", correlation)
            entries = []
            records = ledger.history(wallet["wallet_id"], limit)
            sent_flags = [record["sender_wallet_id"] == wallet["wallet_id"] for record in records]
            counterparty_wallet_ids = [
                record["recipient_wallet_id"] if sent else record["sender_wallet_id"]
                for record, sent in zip(records, sent_flags, strict=True)
            ]
            counterparty_wallets = ledger.get_wallets_by_ids(counterparty_wallet_ids)
            holders = directory.get_many(
                [item["owner_id"] for item in counterparty_wallets.values()]
            )
            for record, sent, other_wallet_id in zip(
                records, sent_flags, counterparty_wallet_ids, strict=True
            ):
                other_wallet = counterparty_wallets.get(other_wallet_id, {})
                other = holders.get(other_wallet.get("owner_id", ""))
                if not other:
                    raise RuntimeError("history counterparty profile is unavailable")
                entries.append(
                    {
                        "transferId": record["transfer_id"],
                        "reference": record["reference"],
                        "amount": _money(record["amount_halalas"]),
                        "direction": "sent" if sent else "received",
                        "otherParty": {
                            "displayName": other["display_name"],
                            "email": other["email_normalized"],
                        },
                        "completedAt": record["completed_at"],
                    }
                )
            truncated = getattr(records, "has_more", False) is True
            return _response(
                200,
                {"items": entries, "nextCursor": None, "hasMore": truncated},
                correlation,
                {"x-u1-history-truncated": "true" if truncated else "false"},
            )
        return _error(500, "INTERNAL_ERROR", "The request could not be completed.", correlation)
    except RecipientMissing:
        return _error(
            404, "RECIPIENT_NOT_FOUND", "The recipient could not be resolved.", correlation
        )
    except InsufficientFunds:
        return _error(
            422, "INSUFFICIENT_FUNDS", "There is not enough balance for this transfer.", correlation
        )
    except EmailConflict:
        return _error(409, "VALIDATION_ERROR", "Wallet setup could not be completed.", correlation)
    except (ValueError, TypeError):
        return _error(400, "VALIDATION_ERROR", "The request is invalid.", correlation)
    except Exception:  # noqa: BLE001 - Storage boundary must disclose no SDK or personal data.
        return _error(
            503, "SERVICE_UNAVAILABLE", "The service is temporarily unavailable.", correlation
        )


def _wallet_response(wallet: dict[str, Any] | None) -> dict[str, Any]:
    assert wallet is not None
    return {"balance": _money(wallet["balance_halalas"])}


def _money(amount_halalas: int) -> dict[str, Any]:
    return {"amountHalalas": amount_halalas, "currency": "SAR"}


def handle(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Instrument one request using bounded dimensions and UTC-independent elapsed time."""
    started = time.monotonic()
    if os.environ.get("CANONICAL_WRITER_ACTIVE") == "false":
        return _error(
            503,
            "SERVICE_UNAVAILABLE",
            "The service is temporarily unavailable.",
            __import__("uuid").uuid4().hex,
        )
    try:
        event = _restore_forwarded(event)
    except (ValueError, TypeError, KeyError, PermissionError):
        return _error(
            401, "AUTHENTICATION_REQUIRED", "Sign in to continue.", __import__("uuid").uuid4().hex
        )
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "").upper()
    try:
        response = _handle_request(event, context)
    except Exception:  # noqa: BLE001 - Final HTTP boundary always returns a safe C1 response.
        correlation = next(
            (
                value
                for key, value in (event.get("headers") or {}).items()
                if key.lower() == "x-correlation-id"
            ),
            "unknown",
        )
        response = _error(
            503,
            "SERVICE_UNAVAILABLE",
            "The service is temporarily unavailable.",
            correlation
            if _CORRELATION_PATTERN.fullmatch(correlation)
            else __import__("uuid").uuid4().hex,
        )
    status = int(response.get("statusCode", 500))
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
    metrics = [
        {"Name": name, "Unit": "Milliseconds" if name.endswith("Ms") else "Count"}
        for name in values
    ]
    dimensions = ["Environment", "Operation", "Result"]
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "Raad/MoneyApi",
                            "Dimensions": [dimensions],
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
    return response
