"""Provision two dev users from runtime secrets and initialize wallets via C1."""

from __future__ import annotations

import json
import os

import boto3
import requests
from botocore.exceptions import ClientError
from botocore.config import Config


def main() -> None:
    secret = boto3.client("secretsmanager", config=Config(retries={"max_attempts": 2})).get_secret_value(SecretId=os.environ["TEST_USERS_SECRET_ARN"])
    users = json.loads(secret["SecretString"])["users"]
    pool_id, client_id = os.environ["USER_POOL_ID"], os.environ["USER_POOL_CLIENT_ID"]
    cognito = boto3.client("cognito-idp", config=Config(retries={"max_attempts": 2}))
    api_url = os.environ["MONEY_API_BASE_URL"].rstrip("/")
    if not api_url.startswith("https://"):
        raise ValueError("MONEY_API_BASE_URL must use HTTPS")
    for user in users:
        username, password = user["email"], user["password"]
        try:
            cognito.admin_create_user(UserPoolId=pool_id, Username=username, MessageAction="SUPPRESS", UserAttributes=[{"Name": "email", "Value": username}, {"Name": "email_verified", "Value": "true"}, {"Name": "name", "Value": user["displayName"]}])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "UsernameExistsException":
                raise
        cognito.admin_set_user_password(UserPoolId=pool_id, Username=username, Password=password, Permanent=True)
        auth = cognito.admin_initiate_auth(UserPoolId=pool_id, ClientId=client_id, AuthFlow="ADMIN_USER_PASSWORD_AUTH", AuthParameters={"USERNAME": username, "PASSWORD": password})
        token = auth["AuthenticationResult"]["AccessToken"]
        response = requests.put(f"{api_url}/money/v1/wallet", headers={"Authorization": f"Bearer {token}"}, timeout=(2.0, 8.0))
        if response.status_code not in (200, 201):
            raise RuntimeError("C1 wallet initialization failed; inspect safe API status and correlation metadata")


if __name__ == "__main__":
    main()
