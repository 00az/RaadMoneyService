"""C1 adapter tests: verified identity, safe errors, response and spoofing."""

import json
from unittest.mock import Mock

import pytest

from raad_money import api
from raad_money.identity import Caller
from raad_money.storage import RecipientMissing


@pytest.fixture(autouse=True)
def dependencies(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock, Mock]:
    verifier, ledger, directory = Mock(), Mock(), Mock()
    verifier.authenticate.return_value = Caller("sub-1", "a@example.com", "A")
    api._verifier, api._ledger, api._directory = verifier, ledger, directory
    monkeypatch.setenv("CORRELATION_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:111111111111:secret:test")
    monkeypatch.setattr(api, "_dependencies", lambda: (verifier, ledger, directory))
    monkeypatch.setattr(api, "_secrets", Mock(get_secret_value=Mock(return_value={"SecretString": '{"version":"test","key":"unit-test-key"}'})))
    return verifier, ledger, directory


def event(method: str = "GET", path: str = "/money/v1/wallet", token: str = "valid") -> dict:
    return {"headers": {"Authorization": f"Bearer {token}", "X-Correlation-Id": "corr-1"}, "requestContext": {"http": {"method": method}}, "rawPath": path, "body": "{}"}


def test_missing_bearer_token_returns_401() -> None:
    result = api.handle({"headers": {}, "requestContext": {"http": {"method": "GET"}}, "rawPath": "/money/v1/wallet"}, None)
    assert result["statusCode"] == 401


def test_invalid_token_returns_401_without_reading_wallet(dependencies: tuple[Mock, Mock, Mock]) -> None:
    verifier, ledger, _ = dependencies
    verifier.authenticate.side_effect = PermissionError
    result = api.handle(event(), None)
    assert result["statusCode"] == 401
    ledger.get_wallet.assert_not_called()


def test_caller_identity_comes_from_token_not_body(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, _ = dependencies
    ledger.get_wallet.return_value = {"wallet_id": "w1", "balance_halalas": 900}
    request = event()
    request["body"] = json.dumps({"holderId": "attacker", "walletId": "other"})
    result = api.handle(request, None)
    assert result["statusCode"] == 200
    ledger.get_wallet.assert_called_once_with("sub-1")


def test_wallet_put_is_idempotent_and_returns_wallet(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, directory = dependencies
    ledger.ensure_wallet.return_value = ({"wallet_id": "w1", "balance_halalas": 100_000}, True)
    result = api.handle(event("PUT"), None)
    assert result["statusCode"] == 201
    assert json.loads(result["body"])["balance"]["amountHalalas"] == 100_000
    directory.ensure_holder.assert_called_once_with("sub-1", "a@example.com", "A")


def test_recipient_unknown_uses_non_disclosing_error(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, _, directory = dependencies
    directory.find_by_email.side_effect = RecipientMissing
    request = event("POST", "/money/v1/recipients/resolve")
    request["body"] = json.dumps({"email": "unknown@example.com"})
    result = api.handle(request, None)
    assert result["statusCode"] == 404
    assert "not registered" not in result["body"].lower()


def test_malformed_json_returns_safe_validation_error() -> None:
    request = event("POST", "/money/v1/recipients/resolve")
    request["body"] = "{" 
    assert api.handle(request, None)["statusCode"] == 400


def test_first_history_page_uses_batch_counterparty_lookup(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, directory = dependencies
    ledger.get_wallet.return_value = {"wallet_id": "w1", "balance_halalas": 5}
    ledger.history.return_value = [{"transfer_id": "t1", "reference": "r1", "amount_halalas": 4, "sender_wallet_id": "w1", "recipient_wallet_id": "w2", "completed_at": "2026-11-13T10:00:00Z"}]
    ledger.get_wallets_by_ids.return_value = {"w2": {"wallet_id": "w2", "owner_id": "holder-2"}}
    directory.get_many.return_value = {"holder-2": {"display_name": "B", "email_normalized": "b@example.com"}}
    result = api.handle(event("GET", "/money/v1/transfers"), None)
    payload = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert payload["items"][0]["otherParty"]["displayName"] == "B"
    directory.get_many.assert_called_once_with(["holder-2"])


def test_first_page_rejects_cursor_until_pagination_unit(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, _ = dependencies
    ledger.get_wallet.return_value = {"wallet_id": "w1", "balance_halalas": 5}
    request = event("GET", "/money/v1/transfers")
    request["rawQueryString"] = "cursor=opaque"
    assert api.handle(request, None)["statusCode"] == 400
    ledger.history.assert_not_called()



def test_dependency_failure_is_safe(dependencies: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_dependencies", Mock(side_effect=RuntimeError("offline")))
    assert api.handle(event(), None)["statusCode"] == 503


def test_secret_failure_is_safe(dependencies: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_secrets", Mock(get_secret_value=Mock(side_effect=RuntimeError("offline"))))
    assert api.handle(event(), None)["statusCode"] == 503


@pytest.mark.parametrize("secret", ['{}', '{"key":""}', 'not-json'])
def test_invalid_correlation_secret_is_safe(secret: str, dependencies: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_secrets", Mock(get_secret_value=Mock(return_value={"SecretString": secret})))
    assert api.handle(event(), None)["statusCode"] == 503


@pytest.mark.parametrize("body", ["[]", "null", "42"])
def test_non_object_json_body_is_rejected(body: str) -> None:
    request = event("POST", "/money/v1/recipients/resolve")
    request["body"] = body
    assert api.handle(request, None)["statusCode"] == 400


def test_wallet_get_without_wallet_returns_404(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, _ = dependencies
    ledger.get_wallet.return_value = None
    assert api.handle(event(), None)["statusCode"] == 404


def test_wallet_put_existing_wallet_returns_200(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, _ = dependencies
    ledger.ensure_wallet.return_value = ({"wallet_id": "w1", "balance_halalas": 10}, False)
    assert api.handle(event("PUT"), None)["statusCode"] == 200


def test_recipient_resolve_rejects_wrong_fields(dependencies: tuple[Mock, Mock, Mock]) -> None:
    request = event("POST", "/money/v1/recipients/resolve")
    request["body"] = json.dumps({"email": "b@example.com", "extra": True})
    assert api.handle(request, None)["statusCode"] == 400


def test_recipient_resolve_rejects_self(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, _, directory = dependencies
    directory.find_by_email.return_value = {"holder_id": "sub-1", "email_normalized": "a@example.com", "display_name": "A"}
    request = event("POST", "/money/v1/recipients/resolve")
    request["body"] = json.dumps({"email": "a@example.com"})
    assert api.handle(request, None)["statusCode"] == 409


def test_transfer_validates_fields_idempotency_and_wallet(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, _ = dependencies
    request = event("POST", "/money/v1/transfers")
    request["body"] = json.dumps({"recipientEmail": "b@example.com", "amountHalalas": 1})
    assert api.handle(request, None)["statusCode"] == 400
    request["headers"]["Idempotency-Key"] = "short"
    request["body"] = json.dumps({"recipientEmail": "b@example.com", "amountHalalas": 1, "extra": 2})
    assert api.handle(request, None)["statusCode"] == 400
    request["body"] = json.dumps({"recipientEmail": "b@example.com", "amountHalalas": 1})
    request["headers"]["Idempotency-Key"] = "valid-idempotency-123"
    ledger.get_wallet.return_value = None
    assert api.handle(request, None)["statusCode"] == 404


def test_transfer_self_and_missing_recipient_wallet_are_refused(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, directory = dependencies
    ledger.get_wallet.return_value = {"wallet_id": "w1", "balance_halalas": 100}
    request = event("POST", "/money/v1/transfers")
    request["headers"]["Idempotency-Key"] = "valid-idempotency-123"
    request["body"] = json.dumps({"recipientEmail": "a@example.com", "amountHalalas": 1})
    directory.find_by_email.return_value = {"holder_id": "sub-1"}
    assert api.handle(request, None)["statusCode"] == 409
    directory.find_by_email.return_value = {"holder_id": "sub-2", "email_normalized": "b@example.com", "display_name": "B"}
    ledger.get_wallet.side_effect = [{"wallet_id": "w1", "balance_halalas": 100}, None]
    assert api.handle(request, None)["statusCode"] == 404


def test_transfer_success_returns_wire_shape(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, directory = dependencies
    ledger.get_wallet.side_effect = [{"wallet_id": "w1", "balance_halalas": 100}, {"wallet_id": "w2", "balance_halalas": 0}]
    directory.find_by_email.return_value = {"holder_id": "sub-2", "email_normalized": "b@example.com", "display_name": "B"}
    ledger.transfer.return_value = {"transfer_id": "t1", "reference": "r1", "amount_halalas": 25, "sender_balance_halalas": 75, "completed_at": "now"}
    request = event("POST", "/money/v1/transfers")
    request["headers"]["Idempotency-Key"] = "valid-idempotency-123"
    request["body"] = json.dumps({"recipientEmail": "b@example.com", "amountHalalas": 25})
    result = api.handle(request, None)
    assert result["statusCode"] == 201
    assert result["headers"]["idempotency-replayed"] == "false"
    assert json.loads(result["body"])["balanceAfter"]["amountHalalas"] == 75


def test_transfer_domain_errors_map_to_safe_responses(dependencies: tuple[Mock, Mock, Mock]) -> None:
    from raad_money.ledger import InsufficientFunds
    _, ledger, directory = dependencies
    ledger.get_wallet.side_effect = [{"wallet_id": "w1", "balance_halalas": 100}, {"wallet_id": "w2", "balance_halalas": 0}]
    directory.find_by_email.return_value = {"holder_id": "sub-2"}
    ledger.transfer.side_effect = InsufficientFunds
    request = event("POST", "/money/v1/transfers")
    request["headers"]["Idempotency-Key"] = "valid-idempotency-123"
    request["body"] = json.dumps({"recipientEmail": "b@example.com", "amountHalalas": 25})
    assert api.handle(request, None)["statusCode"] == 422
    ledger.transfer.side_effect = ValueError("invalid")
    ledger.get_wallet.side_effect = [{"wallet_id": "w1", "balance_halalas": 100}, {"wallet_id": "w2", "balance_halalas": 0}]
    assert api.handle(request, None)["statusCode"] == 400


@pytest.mark.parametrize("query", ["limit=nope", "limit=0", "limit=101"])
def test_history_rejects_invalid_limits(query: str, dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, _ = dependencies
    ledger.get_wallet.return_value = {"wallet_id": "w1", "balance_halalas": 1}
    request = event("GET", "/money/v1/transfers")
    request["rawQueryString"] = query
    assert api.handle(request, None)["statusCode"] == 400
    ledger.history.assert_not_called()


def test_history_requires_wallet_and_profile(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, directory = dependencies
    ledger.get_wallet.return_value = None
    assert api.handle(event("GET", "/money/v1/transfers"), None)["statusCode"] == 404
    ledger.get_wallet.return_value = {"wallet_id": "w1", "balance_halalas": 1}
    ledger.history.return_value = [{"transfer_id": "t1", "reference": "r1", "amount_halalas": 1, "sender_wallet_id": "w2", "recipient_wallet_id": "w1", "completed_at": "now"}]
    ledger.get_wallets_by_ids.return_value = {"w2": {"owner_id": "gone"}}
    directory.get_many.return_value = {}
    assert api.handle(event("GET", "/money/v1/transfers"), None)["statusCode"] == 503


def test_unknown_route_and_api_headers(dependencies: tuple[Mock, Mock, Mock]) -> None:
    result = api.handle(event("GET", "/unknown"), None)
    assert result["statusCode"] == 500
    assert result["headers"]["cache-control"] == "no-store"
    assert result["headers"]["x-correlation-id"] == "corr-1"


def test_bad_correlation_id_is_replaced(dependencies: tuple[Mock, Mock, Mock]) -> None:
    _, ledger, _ = dependencies
    ledger.get_wallet.return_value = {"wallet_id": "w1", "balance_halalas": 5}
    request = event()
    request["headers"]["X-Correlation-Id"] = "bad\nvalue"
    result = api.handle(request, None)
    assert result["statusCode"] == 200
    assert result["headers"]["x-correlation-id"] != "bad\nvalue"
