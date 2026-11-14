"""Provision two dev users from runtime secrets and initialize wallets via C1."""

from __future__ import annotations

import json
import os
import uuid

import boto3
import requests
from botocore.config import Config


def main() -> None:
    secrets = boto3.client(
        "secretsmanager",
        config=Config(connect_timeout=0.25, read_timeout=1, retries={"total_max_attempts": 1}),
    )
    secret = secrets.get_secret_value(SecretId=os.environ["TEST_USERS_SECRET_ARN"])
    fixture = json.loads(secret["SecretString"])
    templates = fixture["users"]
    if len(templates) < 2:
        raise ValueError("two test-only credential templates required")
    run_id = uuid.uuid4().hex[:12]
    journeys = {}
    pool_id, client_id = os.environ["USER_POOL_ID"], os.environ["USER_POOL_CLIENT_ID"]
    cognito = boto3.client(
        "cognito-idp",
        config=Config(connect_timeout=0.25, read_timeout=2, retries={"total_max_attempts": 1}),
    )
    api_url = os.environ["MONEY_API_BASE_URL"].rstrip("/")
    if not api_url.startswith("https://"):
        raise ValueError("MONEY_API_BASE_URL must use HTTPS")
    users = []
    for phase in ("api", "ios", "android"):
        pair = []
        for template in templates[:2]:
            local, domain = template["email"].rsplit("@", 1)
            pair.append(
                {
                    "email": f"{local.split('+')[0]}+u1-{run_id}-{phase}@{domain}",
                    "password": template["password"],
                    "displayName": f"RAAD {phase} test",
                }
            )
        journeys[phase] = pair
        users.extend(pair)
    for user in users:
        username, password = user["email"], user["password"]
        # A collision fails; never reset an existing user's password or balance.
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=username,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": username},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": user["displayName"]},
            ],
        )
        cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=username, Password=password, Permanent=True
        )
        auth = cognito.admin_initiate_auth(
            UserPoolId=pool_id,
            ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        token = auth["AuthenticationResult"]["AccessToken"]
        response = requests.put(
            f"{api_url}/money/v1/wallet",
            headers={"Authorization": f"Bearer {token}"},
            timeout=(2.0, 8.0),
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(
                "C1 wallet initialization failed; inspect safe API status and correlation metadata"
            )
        if response.json()["balance"]["amountHalalas"] != 100_000:
            raise RuntimeError("fresh fixture opening balance is not ready")
    # Runtime secret holds the fresh pairs; C4 and command arguments contain no credentials.
    secrets.put_secret_value(
        SecretId=os.environ["TEST_USERS_SECRET_ARN"],
        SecretString=json.dumps({**fixture, "runId": run_id, "journeys": journeys}),
    )
    print(json.dumps({"prepared": 6, "runId": run_id}))


if __name__ == "__main__":
    main()
