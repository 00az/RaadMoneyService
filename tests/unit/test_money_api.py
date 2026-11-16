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
    monkeypatch.setenv(
        "CORRELATION_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:111111111111:secret:test"
    )
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setattr(api, "_dependencies", lambda: (verifier, ledger, directory))
    monkeypatch.setattr(
        api,
        "_secrets",
        Mock(
            get_secret_value=Mock(
                return_value={"SecretString": '{"version":"test","key":"unit-test-key"}'}
            )
        ),
    )
    return verifier, ledger, directory


def event(method: str = "GET", path: str = "/money/v1/wallet", token: str = "valid") -> dict:
    return {
        "headers": {"Authorization": f"Bearer {token}", "X-Correlation-Id": "corr-1"},
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "body": "{}",
    }


def test_missing_bearer_token_returns_401() -> None:
    result = api.handle(
        {
            "headers": {},
            "requestContext": {"http": {"method": "GET"}},
            "rawPath": "/money/v1/wallet",
        },
        None,
    )
    assert result["statusCode"] == 401


def test_invalid_token_returns_401_without_reading_wallet(
    dependencies: tuple[Mock, Mock, Mock],
) -> None:
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
    assert result["statusCode"] == 400
    ledger.get_wallet.assert_not_called()


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


def history_page(rows: list[dict], next_position: object = None) -> Mock:
    from raad_money.history import HistoryResult

    reader = Mock()
    reader.page.return_value = HistoryResult(rows, next_position)  # type: ignore[arg-type]
    return reader


def test_first_history_page_uses_batch_counterparty_lookup(
    dependencies: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ledger, directory = dependencies
    ledger.get_wallet.return_value = {"wallet_id": "w1", "balance_halalas": 5}
    reader = history_page(
        [
            {
                "transfer_id": "t1",
                "reference": "r1",
                "amount_halalas": 400,
                "sender_wallet_id": "w1",
                "recipient_wallet_id": "w2",
                "completed_at": "2026-11-13T10:00:00.000Z",
                "completed_sort_key": "2026-11-13T10:00:00.000Z#t1",
            }
        ]
    )
    monkeypatch.setattr(api, "_history_reader", lambda _ledger: reader)
    ledger.get_wallets_by_ids.return_value = {"w2": {"wallet_id": "w2", "owner_id": "holder-2"}}
    directory.get_many.return_value = {
        "holder-2": {"display_name": "B", "email_normalized": "b@example.com"}
    }
    result = api.handle(event("GET", "/money/v1/transfers"), None)
    payload = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert payload["items"][0]["otherParty"]["displayName"] == "B"
    assert payload["nextCursor"] is None and payload["hasMore"] is False
    directory.get_many.assert_called_once_with(["holder-2"])


def test_altered_cursor_is_refused_before_reading_history(
    dependencies: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from raad_money.cursor import CursorKeys

    _, ledger, _ = dependencies
    ledger.get_wallet.return_value = {"wallet_id": "w1", "balance_halalas": 5}
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setattr(
        api,
        "_cursor_keys",
        lambda: CursorKeys.from_secret('{"version":"1","key":"' + "k" * 40 + '"}'),
    )
    reader = history_page([])
    monkeypatch.setattr(api, "_history_reader", lambda _ledger: reader)
    request = event("GET", "/money/v1/transfers")
    request["rawQueryString"] = "cursor=opaque"
    result = api.handle(request, None)
    assert result["statusCode"] == 400
    assert json.loads(result["body"])["code"] == "CURSOR_INVALID_OR_EXPIRED"
    reader.page.assert_not_called()


def test_dependency_failure_is_safe(
    dependencies: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "_dependencies", Mock(side_effect=RuntimeError("offline")))
    assert api.handle(event(), None)["statusCode"] == 503


def test_secret_failure_is_safe(
    dependencies: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        api, "_secrets", Mock(get_secret_value=Mock(side_effect=RuntimeError("offline")))
    )
    assert api.handle(event(), None)["statusCode"] == 503


@pytest.mark.parametrize("secret", ["{}", '{"key":""}', "not-json"])
def test_invalid_correlation_secret_is_safe(
    secret: str, dependencies: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        api, "_secrets", Mock(get_secret_value=Mock(return_value={"SecretString": secret}))
    )
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
    directory.find_by_email.return_value = {
        "holder_id": "sub-1",
        "email_normalized": "a@example.com",
        "display_name": "A",
    }
    request = event("POST", "/money/v1/recipients/resolve")
    request["body"] = json.dumps({"email": "a@example.com"})
    assert api.handle(request, None)["statusCode"] == 409


def test_transfer_validates_fields_idempotency_and_wallet(
    dependencies: tuple[Mock, Mock, Mock],
) -> None:
    _, ledger, _ = dependencies
    request = event("POST", "/money/v1/transfers")
    request["body"] = json.dumps({"recipientEmail": "b@example.com", "amountHalalas": 1})
    assert api.handle(request, None)["statusCode"] == 400
    request["headers"]["Idempotency-Key"] = "short"
    request["body"] = json.dumps(
        {"recipientEmail": "b@example.com", "amountHalalas": 1, "extra": 2}
    )
    assert api.handle(request, None)["statusCode"] == 400
    request["body"] = json.dumps({"recipientEmail": "b@example.com", "amountHalalas": 1})
    request["headers"]["Idempotency-Key"] = "valid-idempotency-123"
    ledger.get_wallet.return_value = None
    assert api.handle(request, None)["statusCode"] == 404


def keyed_transfer(amount: int = 2_500, email: str = "b@example.com") -> dict:
    request = event("POST", "/money/v1/transfers")
    request["headers"]["Idempotency-Key"] = "valid-idempotency-123"
    request["body"] = json.dumps({"recipientEmail": email, "amountHalalas": amount})
    return request


def stub_service(monkeypatch: pytest.MonkeyPatch, **behaviour: object) -> Mock:
    service = Mock(**behaviour)
    monkeypatch.setattr(api, "_transfer_service", lambda _ledger, _directory: service)
    return service


def test_transfer_refusals_carry_first_outcome_and_replay_header(
    dependencies: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from raad_money.ledger import TransferOutcome
    from raad_money.transfers import TransferResponse

    for code, status in (("SELF_TRANSFER", 409), ("RECIPIENT_NOT_FOUND", 404)):
        stub_service(
            monkeypatch,
            **{
                "submit.return_value": TransferResponse(
                    TransferOutcome.refused("a" * 64, code), replayed=False
                )
            },
        )
        result = api.handle(keyed_transfer(), None)
        assert result["statusCode"] == status
        assert json.loads(result["body"])["code"] == code
        assert result["headers"]["idempotency-replayed"] == "false"


def test_transfer_success_returns_wire_shape(
    dependencies: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from raad_money.ledger import TransferOutcome
    from raad_money.transfers import TransferResponse

    _, _, directory = dependencies
    outcome = TransferOutcome.completed(
        fingerprint="a" * 64,
        transfer_id="6f1c2c7e-6a4b-4d55-9a57-1f2d3c4b5a69",
        reference="R-23456789ABCD",
        amount_halalas=2_500,
        balance_after_halalas=7_500,
        completed_at="2026-11-16T10:00:00.000Z",
        recipient_profile_version_id="version-b",
    )
    stub_service(monkeypatch, **{"submit.return_value": TransferResponse(outcome, False)})
    directory.get_profile_version.return_value = {"email": "B@example.com", "display_name": "B"}
    result = api.handle(keyed_transfer(), None)
    assert result["statusCode"] == 201
    assert result["headers"]["idempotency-replayed"] == "false"
    body = json.loads(result["body"])
    assert body["balanceAfter"]["amountHalalas"] == 7_500
    assert body["recipient"] == {"email": "B@example.com", "displayName": "B"}
    directory.get_profile_version.assert_called_once_with("version-b")


def test_transfer_low_balance_and_store_failures_map_to_safe_responses(
    dependencies: tuple[Mock, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from raad_money.ledger import TransferOutcome
    from raad_money.store_errors import TransientStoreError
    from raad_money.transfers import TransferResponse

    stub_service(
        monkeypatch,
        **{
            "submit.return_value": TransferResponse(
                TransferOutcome.refused("a" * 64, "INSUFFICIENT_FUNDS"), replayed=True
            )
        },
    )
    low = api.handle(keyed_transfer(), None)
    assert low["statusCode"] == 422 and low["headers"]["idempotency-replayed"] == "true"
    stub_service(monkeypatch, **{"submit.side_effect": TransientStoreError("busy")})
    unavailable = api.handle(keyed_transfer(), None)
    assert unavailable["statusCode"] == 503
    assert "idempotency-replayed" not in unavailable["headers"]
    stub_service(monkeypatch, **{"submit.side_effect": ValueError("corrupt stored outcome")})
    assert api.handle(keyed_transfer(), None)["statusCode"] == 503


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
    ledger.history.return_value = [
        {
            "transfer_id": "t1",
            "reference": "r1",
            "amount_halalas": 1,
            "sender_wallet_id": "w2",
            "recipient_wallet_id": "w1",
            "completed_at": "now",
        }
    ]
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


def test_directory_failure_after_wallet_commit_repairs_without_another_credit(dependencies) -> None:
    _, ledger, directory = dependencies
    wallet = {"wallet_id": "same", "balance_halalas": 100_000}
    ledger.ensure_wallet.side_effect = [(wallet, True), (wallet, False)]
    directory.ensure_holder.side_effect = [RuntimeError("offline"), None]
    assert api.handle(event("PUT"), None)["statusCode"] == 503
    assert api.handle(event("PUT"), None)["statusCode"] == 200
    assert ledger.ensure_wallet.call_count == 2
    assert wallet["balance_halalas"] == 100_000


def test_iam_forwarding_restores_bearer_and_preserves_key(dependencies, monkeypatch) -> None:
    verifier, ledger, _ = dependencies
    ledger.get_wallet.return_value = {"wallet_id": "w1", "balance_halalas": 1}
    original = event()
    original["headers"]["Idempotency-Key"] = "request-key-123456"
    request = {
        "method": "GET",
        "path": original["rawPath"],
        "query": "",
        "headers": original["headers"],
        "body": "{}",
    }
    wrapped = {
        "headers": {"authorization": "AWS4-HMAC-SHA256 outer"},
        "body": json.dumps({"version": 1, "request": request}),
        "requestContext": {
            "authorizer": {"iam": {"userArn": "arn:aws:iam::111111111111:role/edge"}}
        },
    }
    monkeypatch.setenv("REQUIRE_FORWARDING_ENVELOPE", "true")
    assert api.handle(wrapped, None)["statusCode"] == 200
    verifier.authenticate.assert_called_once_with("valid")
    assert api.handle(original, None)["statusCode"] == 401


def test_error_is_top_level_c1_and_response_contains_no_internal_wallet_id(dependencies) -> None:
    _, ledger, _ = dependencies
    ledger.get_wallet.return_value = {"wallet_id": "private-id", "balance_halalas": 1}
    assert json.loads(api.handle(event(), None)["body"]) == {
        "balance": {"amountHalalas": 1, "currency": "SAR"}
    }
    rejected = api.handle({**event(), "headers": {}}, None)
    assert set(json.loads(rejected["body"])) == {"code", "message", "correlationId"}


def test_quiesced_writer_does_not_authenticate_or_change_data(dependencies, monkeypatch) -> None:
    verifier, ledger, _ = dependencies
    monkeypatch.setenv("CANONICAL_WRITER_ACTIVE", "false")
    assert api.handle(event("POST", "/money/v1/transfers"), None)["statusCode"] == 503
    verifier.authenticate.assert_not_called()
    ledger.transfer.assert_not_called()
