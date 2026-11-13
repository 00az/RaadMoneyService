"""C1 boundary integration cases against DynamoDB Local and a verified-caller fixture."""

import json
from unittest.mock import Mock

import pytest

from raad_money import api
from raad_money.identity import Caller
from raad_money.storage import LedgerRepository, WalletDirectory


@pytest.fixture
def local_api(monkeypatch: pytest.MonkeyPatch, ledger_table: str, directory_table: str) -> None:
    ledger, directory = LedgerRepository(ledger_table), WalletDirectory(directory_table)
    verifier = Mock()
    verifier.authenticate.return_value = Caller("c1-user", "c1@example.test", "C1 User")
    monkeypatch.setattr(api, "_dependencies", lambda: (verifier, ledger, directory))
    monkeypatch.setattr(api, "_secrets", Mock(get_secret_value=Mock(return_value={"SecretString": '{"version":"test","key":"integration-test-key"}'})))


def test_c1_put_wallet_returns_201_then_200(local_api: None) -> None:
    request = {"headers": {"Authorization": "Bearer verified", "X-Correlation-Id": "c1-corr"}, "requestContext": {"http": {"method": "PUT"}}, "rawPath": "/money/v1/wallet"}
    first, second = api.handle(request, None), api.handle(request, None)
    assert (first["statusCode"], second["statusCode"]) == (201, 200)
    assert json.loads(second["body"])["balance"]["amountHalalas"] == 100_000


def test_c1_balance_does_not_accept_wallet_identity_override(local_api: None) -> None:
    request = {"headers": {"Authorization": "Bearer verified"}, "requestContext": {"http": {"method": "GET"}}, "rawPath": "/money/v1/wallet", "body": '{"walletId":"another"}'}
    assert api.handle(request, None)["statusCode"] == 404


def test_c1_response_always_echoes_correlation_header(local_api: None) -> None:
    request = {"headers": {"Authorization": "Bearer verified", "X-Correlation-Id": "c1-corr"}, "requestContext": {"http": {"method": "PUT"}}, "rawPath": "/money/v1/wallet"}
    assert api.handle(request, None)["headers"]["x-correlation-id"] == "c1-corr"
