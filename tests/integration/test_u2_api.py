"""U2 C1 provider tests (test-after): every status MoneyApi produces, against real stores.

Each response body is validated against the committed OpenAPI document, and provider
emission is refused if it carries a field the contract does not declare.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from openapi_schema_validator import OAS31Validator
from openapi_spec_validator.readers import read_from_filename

from raad_money import api
from raad_money.cursor import CursorKeys
from raad_money.identity import Caller
from raad_money.storage import LedgerRepository, WalletDirectory

SPEC, _ = read_from_filename(str(Path(__file__).parents[2] / "openapi" / "raad-money-v1.yaml"))
KEYS = CursorKeys.from_secret(json.dumps({"version": "1", "key": "c" * 48}))
PEOPLE = {
    "alice": Caller("alice", "Alice@Example.test", "Alice"),
    "bob": Caller("bob", "bob@example.test", "Bob"),
    "carol": Caller("carol", "carol@example.test", "Carol"),
    "newbie": Caller("newbie", "newbie@example.test", "Newbie"),
}


def schema_for(status: int, body: dict[str, Any], path: str, method: str) -> str:
    if status >= 400:
        return "Error"
    return {
        ("/wallet", "put"): "Wallet",
        ("/wallet", "get"): "Wallet",
        ("/recipients/resolve", "post"): "Recipient",
        ("/transfers", "post"): "TransferResult",
        ("/transfers", "get"): "TransferPage",
    }[(path, method)]


def undeclared(instance: Any, schema: dict[str, Any]) -> list[str]:
    if "$ref" in schema:
        schema = SPEC["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    found: list[str] = []
    if isinstance(instance, dict) and "properties" in schema:
        found += [key for key in instance if key not in schema["properties"]]
        for key, value in instance.items():
            if key in schema["properties"]:
                found += undeclared(value, schema["properties"][key])
    if isinstance(instance, list) and "items" in schema:
        for item in instance:
            found += undeclared(item, schema["items"])
    return found


def conforms(response: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    body = json.loads(response["body"])
    name = schema_for(response["statusCode"], body, path, method)
    OAS31Validator(
        {"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]}
    ).validate(body)
    assert undeclared(body, {"$ref": f"#/components/schemas/{name}"}) == []
    declared = SPEC["paths"][path][method]["responses"]
    assert str(response["statusCode"]) in declared
    return body


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, ledger_table: str, directory_table: str):
    ledger, directory = LedgerRepository(ledger_table), WalletDirectory(directory_table)
    verifier = Mock()

    def authenticate(token: str) -> Caller:
        if token not in PEOPLE:
            raise PermissionError("invalid token")
        return PEOPLE[token]

    verifier.authenticate.side_effect = authenticate
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setattr(api, "_dependencies", lambda: (verifier, ledger, directory))
    monkeypatch.setattr(api, "_cursor_keys", lambda: KEYS)
    monkeypatch.setattr(
        api,
        "_secrets",
        Mock(get_secret_value=Mock(return_value={"SecretString": '{"version":"t","key":"k"}'})),
    )

    def call(
        who: str,
        method: str,
        path: str,
        body: Any = None,
        key: str | None = None,
        query: str = "",
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {who}", "X-Correlation-Id": "u2-api-test"}
        if key is not None:
            headers["Idempotency-Key"] = key
        event = {
            "headers": headers,
            "requestContext": {"http": {"method": method}},
            "rawPath": f"/money/v1{path}",
            "rawQueryString": query,
            "body": body if isinstance(body, str) or body is None else json.dumps(body),
        }
        return api.handle(event, None)

    for name in ("alice", "bob", "carol"):
        assert call(name, "PUT", "/wallet")["statusCode"] == 201
    call.ledger = ledger  # type: ignore[attr-defined]
    return call


def send(client, who: str, to: str, amount: Any, key: str) -> dict[str, Any]:
    return client(who, "POST", "/transfers", {"recipientEmail": to, "amountHalalas": amount}, key)


def test_transfer_success_and_replay_are_identical_and_conform(client) -> None:
    first = send(client, "alice", " BOB@example.test ", 2_500, "api-key-success-01")
    again = send(client, "alice", "bob@example.test", 2_500, "api-key-success-01")
    assert (first["statusCode"], again["statusCode"]) == (201, 201)
    assert first["headers"]["idempotency-replayed"] == "false"
    assert again["headers"]["idempotency-replayed"] == "true"
    body = conforms(first, "/transfers", "post")
    assert conforms(again, "/transfers", "post") == body
    assert body["recipient"] == {"email": "bob@example.test", "displayName": "Bob"}
    assert body["balanceAfter"] == {"amountHalalas": 97_500, "currency": "SAR"}
    assert body["reference"].startswith("R-")
    wallet = conforms(client("alice", "GET", "/wallet"), "/wallet", "get")
    assert wallet["balance"]["amountHalalas"] == 97_500


def test_key_reuse_with_different_details_is_409_and_keeps_first(client) -> None:
    send(client, "alice", "bob@example.test", 2_500, "api-key-reused-001")
    reused = send(client, "alice", "carol@example.test", 2_500, "api-key-reused-001")
    assert reused["statusCode"] == 409
    assert reused["headers"]["idempotency-replayed"] == "false"
    assert conforms(reused, "/transfers", "post")["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert client.ledger.get_wallet("carol")["balance_halalas"] == 100_000


@pytest.mark.parametrize(
    "to,amount,status,code",
    [
        ("nobody@example.test", 2_500, 404, "RECIPIENT_NOT_FOUND"),
        ("nobody@example.test", 99, 404, "RECIPIENT_NOT_FOUND"),
        ("bob@example.test", 99, 422, "AMOUNT_OUT_OF_RANGE"),
        ("bob@example.test", 50_001, 422, "AMOUNT_OUT_OF_RANGE"),
        ("bob@example.test", 0, 422, "AMOUNT_OUT_OF_RANGE"),
        ("bob@example.test", -500, 422, "AMOUNT_OUT_OF_RANGE"),
        ("alice@example.test", 99, 422, "AMOUNT_OUT_OF_RANGE"),
        ("ALICE@example.test ", 2_500, 409, "SELF_TRANSFER"),
    ],
)
def test_business_refusals_follow_contract_order_and_replay(
    client, to, amount, status, code
) -> None:
    first = send(client, "alice", to, amount, "api-key-refusal-01")
    again = send(client, "alice", to, amount, "api-key-refusal-01")
    assert (first["statusCode"], again["statusCode"]) == (status, status)
    assert conforms(first, "/transfers", "post")["code"] == code
    assert (first["headers"]["idempotency-replayed"], again["headers"]["idempotency-replayed"]) == (
        "false",
        "true",
    )
    assert client.ledger.get_wallet("alice")["balance_halalas"] == 100_000


def test_low_balance_refusal_is_saved_even_after_money_arrives(client) -> None:
    for index in range(2):
        send(client, "alice", "bob@example.test", 50_000, f"api-key-drain-{index:04d}")
    low = send(client, "alice", "bob@example.test", 100, "api-key-lowbal-001")
    assert low["statusCode"] == 422 and json.loads(low["body"])["code"] == "INSUFFICIENT_FUNDS"
    send(client, "carol", "alice@example.test", 5_000, "api-key-refill-001")
    again = send(client, "alice", "bob@example.test", 100, "api-key-lowbal-001")
    assert again["statusCode"] == 422 and again["headers"]["idempotency-replayed"] == "true"


@pytest.mark.parametrize(
    "body,key",
    [
        ({"recipientEmail": "bob@example.test", "amountHalalas": 100.5}, "api-key-invalid-01"),
        ({"recipientEmail": "bob@example.test", "amountHalalas": "100"}, "api-key-invalid-01"),
        ({"recipientEmail": "bob@example.test", "amountHalalas": True}, "api-key-invalid-01"),
        ({"recipientEmail": "bob@example.test", "amountHalalas": 2**63}, "api-key-invalid-01"),
        ({"recipientEmail": "not-an-email", "amountHalalas": 100}, "api-key-invalid-01"),
        ({"recipientEmail": "bob@example.test"}, "api-key-invalid-01"),
        (
            {"recipientEmail": "bob@example.test", "amountHalalas": 100, "from": "x"},
            "api-key-invalid-01",
        ),
        ({"recipientEmail": "bob@example.test", "amountHalalas": 100}, "short"),
        ({"recipientEmail": "bob@example.test", "amountHalalas": 100}, "has spaces in key!"),
        ({"recipientEmail": "bob@example.test", "amountHalalas": 100}, None),
        ('{"recipientEmail": "bob@example.test", "amountHalalas": 1e2}', "api-key-invalid-01"),
        ("x" * 9_000, "api-key-invalid-01"),
    ],
)
def test_schema_failures_are_400_and_consume_no_key(client, body, key) -> None:
    result = client("alice", "POST", "/transfers", body, key)
    assert result["statusCode"] == 400
    assert "idempotency-replayed" not in result["headers"]
    assert conforms(result, "/transfers", "post")["code"] == "VALIDATION_ERROR"
    fixed = send(client, "alice", "bob@example.test", 100, "api-key-invalid-01")
    assert fixed["statusCode"] == 201 and fixed["headers"]["idempotency-replayed"] == "false"


def test_missing_wallet_consumes_no_key_and_same_key_works_after_setup(client) -> None:
    missing = send(client, "newbie", "bob@example.test", 100, "api-key-newbie-001")
    assert missing["statusCode"] == 404 and "idempotency-replayed" not in missing["headers"]
    assert conforms(missing, "/transfers", "post")["code"] == "WALLET_NOT_FOUND"
    assert client("newbie", "PUT", "/wallet")["statusCode"] == 201
    assert (
        send(client, "newbie", "bob@example.test", 100, "api-key-newbie-001")["statusCode"] == 201
    )


def test_unauthenticated_and_unknown_callers_see_nothing(client) -> None:
    for who in ("intruder",):
        result = client(who, "GET", "/transfers")
        assert result["statusCode"] == 401
        assert conforms(result, "/transfers", "get")["code"] == "AUTHENTICATION_REQUIRED"
    no_bearer = api.handle(
        {
            "headers": {},
            "requestContext": {"http": {"method": "POST"}},
            "rawPath": "/money/v1/transfers",
        },
        None,
    )
    assert no_bearer["statusCode"] == 401


def test_caller_cannot_reach_another_users_wallet_or_history(client) -> None:
    send(client, "bob", "carol@example.test", 700, "api-key-private-01")
    alice_wallet = client("alice", "GET", "/wallet", {"walletId": "anything"})
    assert alice_wallet["statusCode"] == 400
    history = conforms(client("alice", "GET", "/transfers"), "/transfers", "get")
    assert history["items"] == [] and history["nextCursor"] is None
    spoof = client(
        "alice",
        "POST",
        "/transfers",
        {
            "recipientEmail": "carol@example.test",
            "amountHalalas": 100,
            "senderEmail": "bob@example.test",
        },
        "api-key-spoof-0001",
    )
    assert spoof["statusCode"] == 400


def test_history_pages_with_cursor_to_a_null_end_and_conform(client) -> None:
    for index in range(25):
        who, to = ("alice", "bob@example.test") if index % 2 else ("bob", "alice@example.test")
        send(client, who, to, 100, f"api-key-page-{index:05d}")
    seen: list[str] = []
    page = conforms(client("alice", "GET", "/transfers", query="limit=10"), "/transfers", "get")
    seen += [item["transferId"] for item in page["items"]]
    assert page["hasMore"] is True and page["nextCursor"]
    while page["nextCursor"]:
        result = client("alice", "GET", "/transfers", query=f"limit=10&cursor={page['nextCursor']}")
        page = conforms(result, "/transfers", "get")
        seen += [item["transferId"] for item in page["items"]]
    assert len(seen) == len(set(seen)) == 25
    assert page["hasMore"] is False and page["nextCursor"] is None
    directions = {
        item["direction"]
        for item in conforms(client("alice", "GET", "/transfers"), "/transfers", "get")["items"]
    }
    assert directions == {"sent", "received"}


def test_default_page_size_is_twenty(client) -> None:
    for index in range(21):
        send(client, "alice", "bob@example.test", 100, f"api-key-default-{index:04d}")
    page = json.loads(client("alice", "GET", "/transfers")["body"])
    assert len(page["items"]) == 20 and page["nextCursor"]


@pytest.mark.parametrize(
    "query_for",
    [
        lambda cursor: f"limit=5&cursor={cursor}",
        lambda cursor: f"limit=2&cursor={cursor[:-3]}abc",
        lambda cursor: "limit=2&cursor=not-a-cursor",
    ],
)
def test_wrong_limit_or_tampered_cursor_requires_reload(client, query_for) -> None:
    for index in range(5):
        send(client, "alice", "bob@example.test", 100, f"api-key-tamper-{index:04d}")
    cursor = json.loads(client("alice", "GET", "/transfers", query="limit=2")["body"])["nextCursor"]
    result = client("alice", "GET", "/transfers", query=query_for(cursor))
    assert result["statusCode"] == 400
    assert conforms(result, "/transfers", "get")["code"] == "CURSOR_INVALID_OR_EXPIRED"


def test_another_users_cursor_is_refused(client) -> None:
    for index in range(5):
        send(client, "alice", "bob@example.test", 100, f"api-key-foreign-{index:04d}")
    cursor = json.loads(client("bob", "GET", "/transfers", query="limit=2")["body"])["nextCursor"]
    result = client("alice", "GET", "/transfers", query=f"limit=2&cursor={cursor}")
    assert json.loads(result["body"])["code"] == "CURSOR_INVALID_OR_EXPIRED"


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=101", "limit=abc", "limit=05", "limit=2&limit=3", "page=2", "cursor="],
)
def test_invalid_history_queries_are_validation_errors(client, query: str) -> None:
    result = client("alice", "GET", "/transfers", query=query)
    assert result["statusCode"] == 400
    assert conforms(result, "/transfers", "get")["code"] == "VALIDATION_ERROR"


def test_recipient_resolution_returns_confirmed_spelling(client) -> None:
    found = client("bob", "POST", "/recipients/resolve", {"email": " alice@EXAMPLE.test"})
    assert conforms(found, "/recipients/resolve", "post") == {
        "email": "Alice@Example.test",
        "displayName": "Alice",
    }
    missing = client("bob", "POST", "/recipients/resolve", {"email": "ghost@example.test"})
    assert conforms(missing, "/recipients/resolve", "post")["code"] == "RECIPIENT_NOT_FOUND"
    invalid = client("bob", "POST", "/recipients/resolve", {"email": "a@b"})
    assert conforms(invalid, "/recipients/resolve", "post")["code"] == "VALIDATION_ERROR"


def test_setup_and_balance_conform(client) -> None:
    again = client("alice", "PUT", "/wallet")
    assert again["statusCode"] == 200
    assert conforms(again, "/wallet", "put") == {
        "balance": {"amountHalalas": 100_000, "currency": "SAR"}
    }


def test_missing_profile_version_fails_replay_safely(client) -> None:
    first = json.loads(send(client, "alice", "bob@example.test", 300, "api-key-version-01")["body"])
    outcome = client.ledger.get_outcome("alice", "api-key-version-01")
    directory = api._dependencies()[2]
    directory.table.delete_item(
        Key={"pk": f"PROFILE_VERSION#{outcome.recipient_profile_version_id}", "sk": "VERSION"}
    )
    replay = send(client, "alice", "bob@example.test", 300, "api-key-version-01")
    assert replay["statusCode"] == 503 and first["transferId"] not in replay["body"]
    assert client.ledger.get_wallet("alice")["balance_halalas"] == 99_700


def test_request_logs_hold_no_personal_data_keys_or_amounts(client, capsys) -> None:
    capsys.readouterr()
    send(client, "alice", "bob@example.test", 4_321, "api-key-logcheck-1")
    client("alice", "GET", "/transfers")
    out = capsys.readouterr().out
    for secret in ("bob@example.test", "Bob", "api-key-logcheck-1", "4321", "alice"):
        assert secret not in out
