"""Independent Cognito access-token validation and profile lookup."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.request import HTTPRedirectHandler, build_opener

import boto3
import jwt
from jwt import PyJWKClient


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


urlopen = build_opener(_NoRedirect()).open


@dataclass(frozen=True, slots=True)
class Caller:
    holder_id: str
    email: str
    display_name: str


class TokenVerifier:
    """Validates issuer, audience, token use and subject against immutable env config."""

    def __init__(self, issuer: str, audience: str, profile_url: str) -> None:
        if (
            not issuer.startswith("https://")
            or not audience
            or not profile_url.startswith("https://")
        ):
            raise ValueError("invalid verifier configuration")
        self.issuer, self.audience, self.profile_url = issuer.rstrip("/"), audience, profile_url
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
        request = __import__("urllib.request", fromlist=["Request"]).Request(
            self.profile_url, headers={"Authorization": f"Bearer {token}"}
        )
        with urlopen(request, timeout=1.0) as response:
            profile = json.loads(response.read())
        if profile.get("sub") != claims["sub"]:
            raise PermissionError("profile subject mismatch")
        email, display_name = profile.get("email"), profile.get("name")
        if (
            not isinstance(email, str)
            or not isinstance(display_name, str)
            or not display_name.strip()
        ):
            raise PermissionError("profile incomplete")
        return Caller(claims["sub"], email, display_name.strip())
