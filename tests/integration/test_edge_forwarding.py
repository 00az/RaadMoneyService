"""Service Function URL forwarding requirements and unknown-result handling."""

import json

import pytest

from raad_money import api


def test_transfer_requires_c1_idempotency_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_dependencies", lambda: (_verified(), _ledger(), _directory()))
    monkeypatch.setattr(api, "_secrets", _secret_client())
    request = {"headers": {"Authorization": "Bearer signed", "X-Correlation-Id": "edge-test"}, "requestContext": {"http": {"method": "POST"}}, "rawPath": "/money/v1/transfers", "body": json.dumps({"recipientEmail": "r@example.test", "amountHalalas": 10})}
    assert api.handle(request, None)["statusCode"] == 400


def test_success_response_matches_c1_money_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier, ledger, directory = _verified(), _ledger(), _directory()
    ledger.get_wallet.side_effect = [{"wallet_id": "sender-wallet", "balance_halalas": 100_000}, {"wallet_id": "recipient-wallet", "balance_halalas": 100_000}]
    ledger.transfer.return_value = {"transfer_id": "11111111-1111-4111-8111-111111111111", "reference": "TXN-123", "amount_halalas": 10_000, "sender_balance_halalas": 90_000, "completed_at": "2026-11-13T10:00:00Z"}
    directory.find_by_email.return_value = {"holder_id": "recipient", "email_normalized": "r@example.test", "display_name": "Receiver"}
    monkeypatch.setattr(api, "_dependencies", lambda: (verifier, ledger, directory))
    monkeypatch.setattr(api, "_secrets", _secret_client())
    request = {"headers": {"Authorization": "Bearer signed", "Idempotency-Key": "request-key-123456"}, "requestContext": {"http": {"method": "POST"}}, "rawPath": "/money/v1/transfers", "body": json.dumps({"recipientEmail": "r@example.test", "amountHalalas": 10_000})}
    result = api.handle(request, None)
    payload = json.loads(result["body"])
    assert result["statusCode"] == 201
    assert payload["amount"] == {"amountHalalas": 10_000, "currency": "SAR"}
    assert payload["balanceAfter"]["amountHalalas"] == 90_000
    assert result["headers"]["idempotency-replayed"] == "false"


def test_self_transfer_returns_409_without_write(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier, ledger, directory = _verified(), _ledger(), _directory()
    ledger.get_wallet.return_value = {"wallet_id": "sender-wallet", "balance_halalas": 100_000}
    directory.find_by_email.return_value = {"holder_id": "sender", "email_normalized": "s@example.test", "display_name": "Sender"}
    monkeypatch.setattr(api, "_dependencies", lambda: (verifier, ledger, directory))
    monkeypatch.setattr(api, "_secrets", _secret_client())
    request = {"headers": {"Authorization": "Bearer signed", "Idempotency-Key": "request-key-123456"}, "requestContext": {"http": {"method": "POST"}}, "rawPath": "/money/v1/transfers", "body": json.dumps({"recipientEmail": "s@example.test", "amountHalalas": 100})}
    assert api.handle(request, None)["statusCode"] == 409
    ledger.transfer.assert_not_called()


def test_unknown_transfer_result_is_safe_and_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier, ledger, directory = _verified(), _ledger(), _directory()
    ledger.get_wallet.side_effect = [{"wallet_id": "sender-wallet", "balance_halalas": 100_000}, {"wallet_id": "recipient-wallet", "balance_halalas": 100_000}]
    ledger.transfer.side_effect = TimeoutError("wire detail")
    directory.find_by_email.return_value = {"holder_id": "recipient", "email_normalized": "r@example.test", "display_name": "Receiver"}
    monkeypatch.setattr(api, "_dependencies", lambda: (verifier, ledger, directory))
    monkeypatch.setattr(api, "_secrets", _secret_client())
    request = {"headers": {"Authorization": "Bearer signed", "Idempotency-Key": "request-key-123456"}, "requestContext": {"http": {"method": "POST"}}, "rawPath": "/money/v1/transfers", "body": json.dumps({"recipientEmail": "r@example.test", "amountHalalas": 100})}
    response = api.handle(request, None)
    assert response["statusCode"] == 503
    assert "wire detail" not in response["body"]
    ledger.transfer.assert_called_once()


def test_invalid_recipient_is_safe_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier, ledger, directory = _verified(), _ledger(), _directory()
    directory.find_by_email.side_effect = __import__("raad_money.storage", fromlist=["RecipientMissing"]).RecipientMissing
    monkeypatch.setattr(api, "_dependencies", lambda: (verifier, ledger, directory))
    monkeypatch.setattr(api, "_secrets", _secret_client())
    request = {"headers": {"Authorization": "Bearer signed"}, "requestContext": {"http": {"method": "POST"}}, "rawPath": "/money/v1/recipients/resolve", "body": json.dumps({"email": "missing@example.test"})}
    assert api.handle(request, None)["statusCode"] == 404


def _verified():
    from unittest.mock import Mock
    from raad_money.identity import Caller
    verifier = Mock()
    verifier.authenticate.return_value = Caller("sender", "s@example.test", "Sender")
    return verifier


def _ledger():
    from unittest.mock import Mock
    return Mock()


def _directory():
    from unittest.mock import Mock
    return Mock()


def _secret_client():
    from unittest.mock import Mock
    return Mock(get_secret_value=Mock(return_value={"SecretString": '{"version":"test","key":"integration-test-key"}'}))
