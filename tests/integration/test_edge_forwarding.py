"""Service Function URL forwarding requirements and unknown-result handling."""

import json

import pytest

from raad_money import api


def test_transfer_requires_c1_idempotency_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_dependencies", lambda: (_verified(), _ledger(), _directory()))
    monkeypatch.setattr(api, "_secrets", _secret_client())
    request = {
        "headers": {"Authorization": "Bearer signed", "X-Correlation-Id": "edge-test"},
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": "/money/v1/transfers",
        "body": json.dumps({"recipientEmail": "r@example.test", "amountHalalas": 10}),
    }
    assert api.handle(request, None)["statusCode"] == 400


@pytest.fixture
def real_stores(monkeypatch: pytest.MonkeyPatch, ledger_table: str, directory_table: str):
    from raad_money.storage import LedgerRepository, WalletDirectory
    from raad_money.wallet_setup import ensure_wallet_and_holder

    ledger, directory = LedgerRepository(ledger_table), WalletDirectory(directory_table)
    ensure_wallet_and_holder(ledger, directory, "sender", "s@example.test", "Sender")
    ensure_wallet_and_holder(ledger, directory, "recipient", "r@example.test", "Receiver")
    monkeypatch.setattr(api, "_dependencies", lambda: (_verified(), ledger, directory))
    monkeypatch.setattr(api, "_secrets", _secret_client())
    return ledger, directory


def _keyed(email: str, amount: int) -> dict:
    return {
        "headers": {"Authorization": "Bearer signed", "Idempotency-Key": "request-key-123456"},
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": "/money/v1/transfers",
        "body": json.dumps({"recipientEmail": email, "amountHalalas": amount}),
    }


def test_success_response_matches_c1_money_shapes(real_stores) -> None:
    result = api.handle(_keyed("r@example.test", 10_000), None)
    payload = json.loads(result["body"])
    assert result["statusCode"] == 201
    assert payload["amount"] == {"amountHalalas": 10_000, "currency": "SAR"}
    assert payload["balanceAfter"]["amountHalalas"] == 90_000
    assert payload["recipient"] == {"email": "r@example.test", "displayName": "Receiver"}
    assert result["headers"]["idempotency-replayed"] == "false"


def test_self_transfer_returns_409_without_write(real_stores) -> None:
    ledger, _ = real_stores
    result = api.handle(_keyed("s@example.test", 100), None)
    assert result["statusCode"] == 409
    assert json.loads(result["body"])["code"] == "SELF_TRANSFER"
    assert ledger.get_wallet("sender")["balance_halalas"] == 100_000
    assert ledger.transactions.scan()["Items"] == []


def test_unknown_transfer_result_is_safe_and_never_retried(
    real_stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    from raad_money.store_errors import CommitUncertain

    ledger, _ = real_stores
    uncertain = Mock(side_effect=CommitUncertain("wire detail"))
    monkeypatch.setattr(ledger, "commit_transfer", uncertain)
    response = api.handle(_keyed("r@example.test", 100), None)
    assert response["statusCode"] == 503
    assert "wire detail" not in response["body"]
    assert "idempotency-replayed" not in response["headers"]
    uncertain.assert_called_once()
    assert ledger.get_wallet("sender")["balance_halalas"] == 100_000


def test_invalid_recipient_is_safe_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier, ledger, directory = _verified(), _ledger(), _directory()
    directory.find_by_email.side_effect = __import__(
        "raad_money.storage", fromlist=["RecipientMissing"]
    ).RecipientMissing
    monkeypatch.setattr(api, "_dependencies", lambda: (verifier, ledger, directory))
    monkeypatch.setattr(api, "_secrets", _secret_client())
    request = {
        "headers": {"Authorization": "Bearer signed"},
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": "/money/v1/recipients/resolve",
        "body": json.dumps({"email": "missing@example.test"}),
    }
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

    return Mock(
        get_secret_value=Mock(
            return_value={"SecretString": '{"version":"test","key":"integration-test-key"}'}
        )
    )
