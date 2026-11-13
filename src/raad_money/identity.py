"""Independent Cognito access-token validation and profile lookup."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.request import urlopen

import jwt
import boto3
from jwt import PyJWKClient


@dataclass(frozen=True, slots=True)
class Caller:
    holder_id: str
    email: str
    display_name: str


class TokenVerifier:
    """Validates issuer, audience, token use and subject against immutable env config."""

    def __init__(self, issuer: str, audience: str, profile_url: str) -> None:
        if not issuer.startswith("https://") or not audience or not profile_url.startswith("https://"):
            raise ValueError("invalid verifier configuration")
        self.issuer, self.audience, self.profile_url = issuer.rstrip("/"), audience, profile_url

    @classmethod
    def from_environment(cls) -> TokenVerifier:
        parameter = boto3.client("ssm").get_parameter(Name=os.environ["TOKEN_VERIFIER_PARAMETER"])["Parameter"]["Value"]
        config = json.loads(parameter)
        profile_url = config.get("profileUrl", f"{config['issuer'].rstrip('/')}/oauth2/userInfo")
        return cls(config["issuer"], config["audience"], profile_url)

    def authenticate(self, token: str) -> Caller:
        key = PyJWKClient(f"{self.issuer}/.well-known/jwks.json", cache_jwk_set=True, lifespan=300).get_signing_key_from_jwt(token).key
        claims: dict[str, Any] = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer, options={"verify_aud": False, "require": ["sub", "exp", "iss", "client_id", "token_use"]})
        if claims.get("token_use") != "access" or claims.get("client_id") != self.audience or not claims.get("sub"):
            raise PermissionError("invalid access token")
        request = __import__("urllib.request", fromlist=["Request"]).Request(self.profile_url, headers={"Authorization": f"Bearer {token}"})
        with urlopen(request, timeout=2.0) as response:  # noqa: S310 - HTTPS URL validated at construction.
            profile = json.loads(response.read())
        if profile.get("sub") != claims["sub"]:
            raise PermissionError("profile subject mismatch")
        email, display_name = profile.get("email"), profile.get("name")
        if not isinstance(email, str) or not isinstance(display_name, str) or not display_name.strip():
            raise PermissionError("profile incomplete")
        return Caller(claims["sub"], email, display_name.strip())
