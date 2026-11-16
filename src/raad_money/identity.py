"""Independent Cognito access-token validation and profile lookup."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import boto3
import jwt
from botocore import UNSIGNED
from botocore.config import Config
from jwt import PyJWKClient


@dataclass(frozen=True, slots=True)
class Caller:
    holder_id: str
    email: str
    display_name: str


class TokenVerifier:
    """Validates issuer, audience, token use and subject against immutable env config."""

    def __init__(self, issuer: str, audience: str, profile_url: str) -> None:
        provider = re.fullmatch(
            r"https://cognito-idp\.([a-z0-9-]+)\.amazonaws\.com/([a-z0-9-]+_[A-Za-z0-9]+)",
            issuer.rstrip("/"),
        )
        if provider is None or not audience or profile_url != issuer.rsplit("/", 1)[0] + "/":
            raise ValueError("invalid verifier configuration")
        self.issuer, self.audience, self.profile_url = issuer.rstrip("/"), audience, profile_url
        self.region = provider.group(1)
        self.profile_client: Any = None
        self.jwks = PyJWKClient(
            f"{self.issuer}/.well-known/jwks.json",
            cache_jwk_set=True,
            cache_keys=False,
            lifespan=21600,
            timeout=1,
            cooldown_duration=60,
        )

    @classmethod
    def from_environment(cls) -> TokenVerifier:
        parameter = boto3.client("ssm").get_parameter(Name=os.environ["TOKEN_VERIFIER_PARAMETER"])[
            "Parameter"
        ]["Value"]
        config = json.loads(parameter)
        required = {
            "configVersion",
            "environment",
            "issuer",
            "audience",
            "audienceClaim",
            "tokenTypeMarker",
            "jwksUri",
            "acceptedAlgorithms",
            "profileAttributeSource",
            "userProfileUri",
        }
        if (
            set(config) != required
            or config["configVersion"] != "1.0"
            or config["environment"] != os.environ["ENVIRONMENT"]
        ):
            raise ValueError("invalid verifier configuration schema/environment")
        if (
            config["audienceClaim"] != "client_id"
            or config["acceptedAlgorithms"] != ["RS256"]
            or config["tokenTypeMarker"]
            != {"location": "claim", "name": "token_use", "expectedValue": "access"}
        ):
            raise ValueError("invalid provider verification profile")
        if (
            config["jwksUri"] != config["issuer"].rstrip("/") + "/.well-known/jwks.json"
            or config["profileAttributeSource"] != "user-profile-endpoint"
        ):
            raise ValueError("invalid verifier discovery/profile source")
        return cls(config["issuer"], config["audience"], config["userProfileUri"])

    def authenticate(self, token: str) -> Caller:
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={
                    "verify_aud": False,
                    "require": ["sub", "exp", "iss", "client_id", "token_use"],
                },
            )
        except jwt.PyJWKClientConnectionError as error:
            raise RuntimeError("key discovery unavailable") from error
        except (jwt.InvalidTokenError, jwt.PyJWKClientError) as error:
            raise PermissionError("invalid access token") from error
        if (
            claims.get("token_use") != "access"
            or claims.get("client_id") != self.audience
            or not claims.get("sub")
        ):
            raise PermissionError("invalid access token")
        # SDK authentication supplies this scope. OAuth userInfo requires openid
        # and must not be called with these tokens, even if OAuth is enabled on the client.
        if "aws.cognito.signin.user.admin" not in str(claims.get("scope", "")).split():
            raise PermissionError("profile scope missing")
        if self.profile_client is None:
            self.profile_client = boto3.client(
                "cognito-idp",
                region_name=self.region,
                endpoint_url=self.profile_url,
                config=Config(
                    signature_version=UNSIGNED,
                    connect_timeout=0.25,
                    read_timeout=0.75,
                    retries={"total_max_attempts": 1},
                ),
            )
        try:
            response = self.profile_client.get_user(AccessToken=token)
        except self.profile_client.exceptions.NotAuthorizedException as error:
            raise PermissionError("profile access denied") from error
        attributes = response.get("UserAttributes", [])
        profile = {item["Name"]: item["Value"] for item in attributes}
        if len(profile) != len(attributes):
            raise PermissionError("ambiguous profile")
        if profile.get("sub") != claims["sub"]:
            raise PermissionError("profile subject mismatch")
        email, display_name = profile.get("email"), profile.get("name")
        if (
            not isinstance(email, str)
            or profile.get("email_verified") != "true"
            or not isinstance(display_name, str)
            or not display_name.strip()
        ):
            raise PermissionError("profile incomplete")
        return Caller(claims["sub"], email, display_name.strip())
