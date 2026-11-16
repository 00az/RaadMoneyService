"""Token verifier tests for environment/client binding and profile-subject matching."""

import json
from unittest.mock import Mock

import jwt
import pytest

from raad_money.identity import TokenVerifier


@pytest.mark.parametrize("flow", ["ADMIN_USER_PASSWORD_AUTH", "USER_SRP_AUTH"])
def test_sdk_scope_token_uses_real_get_user_wire_contract(monkeypatch, flow):
    """Locally signed SDK-profile tokens exercise validation and the real SDK model.

    This is provider-contract evidence, not a claim that Cognito minted the token.
    The deployed capability check must still authenticate with each actual flow.
    """
    import time

    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.stub import Stubber
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    checked = verifier()
    claims = {
        "sub": "subject-a",
        "client_id": "client-123",
        "token_use": "access",
        "scope": "aws.cognito.signin.user.admin",
        "iss": checked.issuer,
        "exp": int(time.time()) + 60,
    }
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": flow})
    checked.jwks = Mock(get_signing_key_from_jwt=Mock(return_value=Mock(key=key.public_key())))
    client = boto3.client(
        "cognito-idp", region_name="us-east-1", config=Config(signature_version=UNSIGNED)
    )
    stub = Stubber(client)
    stub.add_response(
        "get_user",
        {
            "Username": "subject-a",
            "UserAttributes": [
                {"Name": "sub", "Value": "subject-a"},
                {"Name": "email", "Value": "a@example.test"},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": "A"},
            ],
        },
        {"AccessToken": token},
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr("raad_money.identity.boto3.client", factory)
    with stub:
        assert checked.authenticate(token).holder_id == "subject-a"
        stub.assert_no_pending_responses()
    assert (
        factory.call_args.kwargs["endpoint_url"] == "https://cognito-idp.us-east-1.amazonaws.com/"
    )
    config = factory.call_args.kwargs["config"]
    assert config.signature_version == UNSIGNED
    assert config.retries["total_max_attempts"] == 1
    assert config.connect_timeout + config.read_timeout == 1


@pytest.mark.parametrize("scope", ["openid email profile", "", "aws.cognito.signin.user.adminx"])
def test_non_sdk_scope_cannot_trigger_get_user(monkeypatch, scope):
    monkeypatch.setattr(
        "raad_money.identity.PyJWKClient",
        lambda *a, **kw: Mock(get_signing_key_from_jwt=Mock(return_value=Mock(key="key"))),
    )
    monkeypatch.setattr(
        jwt,
        "decode",
        lambda *a, **kw: {
            "sub": "subject-a",
            "client_id": "client-123",
            "token_use": "access",
            "scope": scope,
        },
    )
    factory = Mock(side_effect=AssertionError("must not call provider"))
    monkeypatch.setattr("raad_money.identity.boto3.client", factory)
    with pytest.raises(PermissionError, match="scope missing"):
        verifier().authenticate("token")
    factory.assert_not_called()


@pytest.mark.parametrize(
    "uri",
    [
        "https://other.example/",
        "https://cognito-idp.us-west-2.amazonaws.com/",
        "https://dev.auth.us-east-1.amazoncognito.com/oauth2/userInfo",
    ],
)
def test_profile_uri_is_pinned_to_signed_issuer_region(uri):
    with pytest.raises(ValueError):
        TokenVerifier("https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool", "client", uri)


def verifier() -> TokenVerifier:
    return TokenVerifier(
        "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool",
        "client-123",
        "https://cognito-idp.us-east-1.amazonaws.com/",
    )


def test_verifier_rejects_non_https_configuration() -> None:
    with pytest.raises(ValueError):
        TokenVerifier("http://issuer", "client", "https://issuer/userInfo")


def test_verifier_requires_audience_client_configuration() -> None:
    with pytest.raises(ValueError):
        TokenVerifier("https://issuer", "", "https://issuer/userInfo")


def test_profile_subject_must_match_signed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "raad_money.identity.PyJWKClient",
        lambda *args, **kwargs: Mock(
            get_signing_key_from_jwt=Mock(return_value=Mock(key="public-key"))
        ),
    )
    monkeypatch.setattr(
        jwt,
        "decode",
        lambda *args, **kwargs: {
            "sub": "subject-a",
            "client_id": "client-123",
            "token_use": "access",
            "scope": "aws.cognito.signin.user.admin",
        },
    )
    client = Mock(
        get_user=Mock(
            return_value={
                "UserAttributes": [
                    {"Name": "sub", "Value": "subject-b"},
                    {"Name": "email", "Value": "a@example.test"},
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": "name", "Value": "A"},
                ]
            }
        )
    )
    monkeypatch.setattr("raad_money.identity.boto3.client", lambda *args, **kwargs: client)
    with pytest.raises(PermissionError, match="subject mismatch"):
        verifier().authenticate("token")


def test_profile_returns_verified_subject_and_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "raad_money.identity.PyJWKClient",
        lambda *args, **kwargs: Mock(
            get_signing_key_from_jwt=Mock(return_value=Mock(key="public-key"))
        ),
    )
    monkeypatch.setattr(
        jwt,
        "decode",
        lambda *args, **kwargs: {
            "sub": "subject-a",
            "client_id": "client-123",
            "token_use": "access",
            "scope": "aws.cognito.signin.user.admin",
        },
    )
    client = Mock(
        get_user=Mock(
            return_value={
                "UserAttributes": [
                    {"Name": "sub", "Value": "subject-a"},
                    {"Name": "email", "Value": "a@example.test"},
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": "name", "Value": " A "},
                ]
            }
        )
    )
    monkeypatch.setattr("raad_money.identity.boto3.client", lambda *args, **kwargs: client)
    caller = verifier().authenticate("token")
    assert (caller.holder_id, caller.display_name) == ("subject-a", "A")


def test_non_access_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "raad_money.identity.PyJWKClient",
        lambda *args, **kwargs: Mock(
            get_signing_key_from_jwt=Mock(return_value=Mock(key="public-key"))
        ),
    )
    monkeypatch.setattr(
        jwt,
        "decode",
        lambda *args, **kwargs: {"sub": "subject-a", "client_id": "client-123", "token_use": "id"},
    )
    with pytest.raises(PermissionError, match="invalid access token"):
        verifier().authenticate("token")


def test_wrong_environment_client_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "raad_money.identity.PyJWKClient",
        lambda *args, **kwargs: Mock(
            get_signing_key_from_jwt=Mock(return_value=Mock(key="public-key"))
        ),
    )
    monkeypatch.setattr(
        jwt,
        "decode",
        lambda *args, **kwargs: {"sub": "subject-a", "client_id": "wrong", "token_use": "access"},
    )
    with pytest.raises(PermissionError, match="invalid access token"):
        verifier().authenticate("token")


def test_key_discovery_cache_is_bounded_and_reused(monkeypatch):
    factory = Mock(return_value=Mock())
    monkeypatch.setattr("raad_money.identity.PyJWKClient", factory)
    checked = verifier()
    assert checked.jwks is factory.return_value
    kwargs = factory.call_args.kwargs
    assert kwargs == {
        "cache_jwk_set": True,
        "cache_keys": False,
        "lifespan": 21600,
        "timeout": 1,
        "cooldown_duration": 60,
    }


def test_expired_token_is_authentication_failure_not_outage(monkeypatch):
    monkeypatch.setattr(
        "raad_money.identity.PyJWKClient",
        lambda *args, **kwargs: Mock(get_signing_key_from_jwt=Mock(return_value=Mock(key="key"))),
    )
    monkeypatch.setattr(jwt, "decode", Mock(side_effect=jwt.ExpiredSignatureError()))
    with pytest.raises(PermissionError):
        verifier().authenticate("expired")


def test_discovery_failure_without_cache_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        "raad_money.identity.PyJWKClient",
        lambda *args, **kwargs: Mock(
            get_signing_key_from_jwt=Mock(side_effect=jwt.PyJWKClientConnectionError())
        ),
    )
    with pytest.raises(RuntimeError, match="discovery unavailable"):
        verifier().authenticate("token")


@pytest.mark.parametrize("kind", ["unknown-field", "wrong-environment", "wrong-algorithm"])
def test_config_schema_and_environment_fail_closed(kind, monkeypatch):
    config = {
        "configVersion": "1.0",
        "environment": "dev",
        "issuer": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool",
        "audience": "client",
        "audienceClaim": "client_id",
        "tokenTypeMarker": {"location": "claim", "name": "token_use", "expectedValue": "access"},
        "jwksUri": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool/.well-known/jwks.json",
        "acceptedAlgorithms": ["RS256"],
        "profileAttributeSource": "user-profile-endpoint",
        "userProfileUri": "https://cognito-idp.us-east-1.amazonaws.com/",
    }
    if kind == "unknown-field":
        config["extra"] = "unsafe"
    elif kind == "wrong-environment":
        config["environment"] = "prd"
    else:
        config["acceptedAlgorithms"] = ["HS256"]
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("TOKEN_VERIFIER_PARAMETER", "/raad/dev/verifier")
    monkeypatch.setattr(
        "raad_money.identity.boto3.client",
        lambda *args, **kwargs: Mock(
            get_parameter=Mock(return_value={"Parameter": {"Value": json.dumps(config)}})
        ),
    )
    with pytest.raises(ValueError):
        TokenVerifier.from_environment()
