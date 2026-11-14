"""Token verifier tests for environment/client binding and profile-subject matching."""

import json
from unittest.mock import Mock

import jwt
import pytest

from raad_money.identity import TokenVerifier


def verifier() -> TokenVerifier:
    return TokenVerifier(
        "https://issuer.example.test/pool",
        "client-123",
        "https://issuer.example.test/oauth2/userInfo",
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
        },
    )
    profile_response = Mock()
    profile_response.__enter__ = Mock(
        return_value=Mock(
            read=Mock(
                return_value=json.dumps(
                    {"sub": "subject-b", "email": "a@example.test", "name": "A"}
                ).encode()
            )
        )
    )
    profile_response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("raad_money.identity.urlopen", lambda *args, **kwargs: profile_response)
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
        },
    )
    profile_response = Mock()
    profile_response.__enter__ = Mock(
        return_value=Mock(
            read=Mock(return_value=b'{"sub":"subject-a","email":"a@example.test","name":" A "}')
        )
    )
    profile_response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("raad_money.identity.urlopen", lambda *args, **kwargs: profile_response)
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
        "issuer": "https://issuer.example.test/pool",
        "audience": "client",
        "audienceClaim": "client_id",
        "tokenTypeMarker": {"location": "claim", "name": "token_use", "expectedValue": "access"},
        "jwksUri": "https://issuer.example.test/pool/.well-known/jwks.json",
        "acceptedAlgorithms": ["RS256"],
        "profileAttributeSource": "user-profile-endpoint",
        "userProfileUri": "https://issuer.example.test/userInfo",
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
