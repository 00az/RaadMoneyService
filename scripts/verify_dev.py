"""Explicit dev walking-skeleton smoke; mutates only the named simulated users."""

from __future__ import annotations

import json
import os
import uuid

import boto3
import requests


def main() -> None:
    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["TEST_USERS_SECRET_ARN"]
    )
    fixture = json.loads(secret["SecretString"])
    phase = os.environ.get("RAAD_FIXTURE_PHASE", "api")
    users = fixture.get("journeys", {}).get(phase, [])
    if len(users) < 2:
        raise ValueError("prepare a fresh isolated fixture pair for this journey first")
    pool_id, client_id = os.environ["USER_POOL_ID"], os.environ["USER_POOL_CLIENT_ID"]
    api_url = os.environ["MONEY_API_BASE_URL"].rstrip("/")
    if not api_url.startswith("https://"):
        raise ValueError("MONEY_API_BASE_URL must use HTTPS")
    cognito = boto3.client("cognito-idp")
    tokens = []
    for user in users[:2]:
        auth = cognito.admin_initiate_auth(
            UserPoolId=pool_id,
            ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": user["email"], "PASSWORD": user["password"]},
        )
        tokens.append(auth["AuthenticationResult"]["AccessToken"])
    headers = [{"Authorization": f"Bearer {token}"} for token in tokens]
    for header in headers:
        ready = requests.put(f"{api_url}/money/v1/wallet", headers=header, timeout=(2.0, 8.0))
        if ready.status_code not in {200, 201}:
            raise RuntimeError("both fixture wallets and directory entries must be ready")
    before_a = _get_balance(api_url, headers[0])
    before_b = _get_balance(api_url, headers[1])
    if (before_a, before_b) != (100_000, 100_000):
        raise RuntimeError(
            "fixture pair already used or not ready; prepare new users without resetting balances"
        )
    recipient = requests.post(
        f"{api_url}/money/v1/recipients/resolve",
        headers=headers[0],
        json={"email": users[1]["email"]},
        timeout=(2.0, 8.0),
    )
    if recipient.status_code != 200:
        raise RuntimeError(
            f"recipient readiness failed with safe C1 status {recipient.status_code}"
        )
    transfer_headers = {**headers[0], "Idempotency-Key": uuid.uuid4().hex}
    transfer_body = {"recipientEmail": users[1]["email"], "amountHalalas": 10_000}
    result = requests.post(
        f"{api_url}/money/v1/transfers",
        headers=transfer_headers,
        json=transfer_body,
        timeout=(2.0, 8.0),
    )
    if result.status_code != 201:
        raise RuntimeError(f"transfer smoke failed with safe C1 status {result.status_code}")
    transfer_id = result.json()["transferId"]
    # Replay with the same key and body: the saved first result, never a second movement.
    replay = requests.post(
        f"{api_url}/money/v1/transfers",
        headers=transfer_headers,
        json=transfer_body,
        timeout=(2.0, 8.0),
    )
    if (
        replay.status_code != 201
        or replay.headers.get("Idempotency-Replayed") != "true"
        or replay.json()["transferId"] != transfer_id
    ):
        raise RuntimeError(f"replay smoke failed with safe C1 status {replay.status_code}")
    refused = requests.post(
        f"{api_url}/money/v1/transfers",
        headers={**headers[0], "Idempotency-Key": uuid.uuid4().hex},
        json={"recipientEmail": users[1]["email"], "amountHalalas": 99},
        timeout=(2.0, 8.0),
    )
    if refused.status_code != 422 or refused.json().get("code") != "AMOUNT_OUT_OF_RANGE":
        raise RuntimeError(f"range refusal smoke failed with safe C1 status {refused.status_code}")
    after_a = _get_balance(api_url, headers[0])
    after_b = _get_balance(api_url, headers[1])
    sent = _get_history(api_url, headers[0])
    received = _get_history(api_url, headers[1])
    if not (before_a - after_a == 10_000 and after_b - before_b == 10_000):
        raise RuntimeError("balances did not move exactly once by the smoke amount")
    if not any(item["transferId"] == transfer_id and item["direction"] == "sent" for item in sent):
        raise RuntimeError("sender history is missing the smoke transfer")
    if not any(
        item["transferId"] == transfer_id and item["direction"] == "received" for item in received
    ):
        raise RuntimeError("recipient history is missing the smoke transfer")
    print(
        json.dumps(
            {"result": "passed", "transferId": transfer_id, "amountHalalas": 10_000, "replay": True}
        )
    )


def _get_balance(api_url: str, headers: dict[str, str]) -> int:
    response = requests.get(f"{api_url}/money/v1/wallet", headers=headers, timeout=(2.0, 8.0))
    if response.status_code != 200:
        raise RuntimeError(f"wallet readiness failed with safe C1 status {response.status_code}")
    return int(response.json()["balance"]["amountHalalas"])


def _get_history(api_url: str, headers: dict[str, str]) -> list[dict]:
    response = requests.get(f"{api_url}/money/v1/transfers", headers=headers, timeout=(2.0, 8.0))
    if response.status_code != 200:
        raise RuntimeError(f"history readiness failed with safe C1 status {response.status_code}")
    page = response.json()
    if "nextCursor" not in page:
        raise RuntimeError("history page is missing the U2 nextCursor field")
    return page["items"]


if __name__ == "__main__":
    main()
