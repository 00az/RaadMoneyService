"""The dev smoke script proves replay and range refusal without leaking fixture credentials."""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest


def load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "verify_dev", Path(__file__).parents[2] / "scripts" / "verify_dev.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeC1:
    """Minimal U2-shaped C1 for the smoke flow; `replay_moves_money` simulates a defect."""

    def __init__(self, replay_moves_money: bool = False) -> None:
        self.balances = {"token-a": 100_000, "token-b": 100_000}
        self.outcomes: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.replay_moves_money = replay_moves_money

    def caller(self, headers: dict[str, str]) -> str:
        return headers["Authorization"].removeprefix("Bearer ")

    def put(self, url: str, headers: dict[str, str], timeout: Any) -> Mock:
        return Mock(status_code=200)

    def get(self, url: str, headers: dict[str, str], timeout: Any) -> Mock:
        who = self.caller(headers)
        if url.endswith("/wallet"):
            payload = {"balance": {"amountHalalas": self.balances[who], "currency": "SAR"}}
        else:
            items = [
                {**entry, "direction": "sent" if entry["from"] == who else "received"}
                for entry in self.history
            ]
            payload = {"items": items, "nextCursor": None, "hasMore": False}
        return Mock(status_code=200, json=lambda: payload)

    def post(self, url: str, headers: dict[str, str], json: dict[str, Any], timeout: Any) -> Mock:
        if url.endswith("/recipients/resolve"):
            return Mock(status_code=200)
        key = headers["Idempotency-Key"]
        if json["amountHalalas"] < 100:
            return Mock(status_code=422, json=lambda: {"code": "AMOUNT_OUT_OF_RANGE"})
        if key in self.outcomes and not self.replay_moves_money:
            saved = self.outcomes[key]
            return Mock(
                status_code=201, headers={"Idempotency-Replayed": "true"}, json=lambda: saved
            )
        transfer = {"transferId": str(uuid.uuid4()), "from": "token-a"}
        self.balances["token-a"] -= json["amountHalalas"]
        self.balances["token-b"] += json["amountHalalas"]
        self.history.append(transfer)
        self.outcomes[key] = transfer
        return Mock(
            status_code=201, headers={"Idempotency-Replayed": "false"}, json=lambda: transfer
        )


def prepare(monkeypatch: pytest.MonkeyPatch, fake: FakeC1) -> Any:
    smoke = load()
    secrets, cognito = Mock(), Mock()
    secrets.get_secret_value.return_value = {
        "SecretString": json.dumps(
            {
                "journeys": {
                    "api": [
                        {"email": "a+u1@example.test", "password": "do-not-log-password"},
                        {"email": "b+u1@example.test", "password": "do-not-log-password"},
                    ]
                }
            }
        )
    }
    tokens = iter(["token-a", "token-b"])
    cognito.admin_initiate_auth.side_effect = lambda **_: {
        "AuthenticationResult": {"AccessToken": next(tokens)}
    }
    monkeypatch.setattr(
        smoke.boto3, "client", lambda name, **_: secrets if name == "secretsmanager" else cognito
    )
    monkeypatch.setattr(smoke, "requests", Mock(put=fake.put, get=fake.get, post=fake.post))
    for key, value in {
        "TEST_USERS_SECRET_ARN": "fixture-secret",
        "USER_POOL_ID": "pool",
        "USER_POOL_CLIENT_ID": "client",
        "MONEY_API_BASE_URL": "https://api.example.test",
    }.items():
        monkeypatch.setenv(key, value)
    return smoke


def test_smoke_passes_on_replayed_transfer_and_prints_no_credentials(monkeypatch, capsys) -> None:
    fake = FakeC1()
    prepare(monkeypatch, fake).main()
    out = capsys.readouterr().out
    assert json.loads(out)["replay"] is True
    assert fake.balances == {"token-a": 90_000, "token-b": 110_000}
    assert "password" not in out and "token" not in out and "@" not in out


def test_smoke_fails_when_a_replay_moves_money_again(monkeypatch) -> None:
    with pytest.raises(RuntimeError, match="replay smoke failed"):
        prepare(monkeypatch, FakeC1(replay_moves_money=True)).main()
