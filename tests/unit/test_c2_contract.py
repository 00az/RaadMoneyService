"""C2 pin (test-after): the pinned copy is intact, and the consumer and its rule match it.

C2 (`c2-identity-events`) is owned by U4 in `Raad`. This repo reads only the byte-for-byte
copy named in `contracts/pins.yaml`, never a branch of the owning repo.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from openapi_spec_validator.readers import read_from_filename

from raad_money import consumer
from raad_money.infrastructure import ENVIRONMENTS, MoneyServiceStack

ROOT = Path(__file__).parents[2]
CONTRACT_ID = "c2-identity-events"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def read_yaml(path: Path) -> dict[str, Any]:
    document, _ = read_from_filename(str(path))
    return dict(document)


def c2_pin() -> dict[str, Any]:
    pins = read_yaml(ROOT / "contracts" / "pins.yaml")["pins"]
    matching = [pin for pin in pins if pin["contract"] == CONTRACT_ID]
    assert len(matching) == 1, f"contracts/pins.yaml must pin {CONTRACT_ID} exactly once"
    return dict(matching[0])


def pinned_contract() -> dict[str, Any]:
    return read_yaml(ROOT / c2_pin()["copy"])


def resolve(document: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Follow local `$ref`s such as `#/components/messages/UserConfirmed`."""
    while "$ref" in node:
        target: Any = document
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node


def user_confirmed_channel(document: dict[str, Any]) -> dict[str, Any]:
    channels = [
        channel
        for channel in document["channels"].values()
        if "UserConfirmed" in channel.get("messages", {})
    ]
    assert len(channels) == 1, "the pinned C2 copy must carry UserConfirmed on one channel"
    return dict(channels[0])


def payload_schema(document: dict[str, Any]) -> dict[str, Any]:
    message = resolve(document, user_confirmed_channel(document)["messages"]["UserConfirmed"])
    return resolve(document, message["payload"])


def valid_detail() -> dict[str, Any]:
    return {
        "specVersion": "1.0",
        "eventId": "0b6f6a9e-6d1e-4c2f-9a55-3f1d2e7c8b90",
        "occurredAt": "2026-11-16T11:59:00Z",
        "environment": "dev",
        "userId": "user-123",
        "email": "Person@Example.test",
        "displayName": "Person",
    }


def envelope(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": consumer.EVENT_SOURCE,
        "detail-type": consumer.EVENT_DETAIL_TYPE,
        "detail": detail,
    }


def test_pinned_copy_matches_the_sha256_in_pins_yaml() -> None:
    pin = c2_pin()
    assert pin["owner"] == "00az/Raad"
    assert pin["file"] == "contracts/c2-identity-events-v1.asyncapi.yaml"
    assert re.fullmatch(r"[0-9a-f]{40}", pin["commit"])
    assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])
    copy = ROOT / pin["copy"]
    assert copy.is_relative_to(ROOT / "contracts" / "pinned")
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == pin["sha256"]


def test_pinned_copy_is_major_version_one() -> None:
    # The consumer accepts every 1.x specVersion; a 2.x contract is a new file and address.
    assert str(pinned_contract()["info"]["version"]).split(".")[0] == "1"


def test_consumer_constants_match_the_pinned_eventbridge_mapping() -> None:
    mapping = user_confirmed_channel(pinned_contract())["x-eventbridge"]
    assert consumer.EVENT_SOURCE == mapping["source"]
    assert consumer.EVENT_DETAIL_TYPE == mapping["detail-type"]
    assert mapping["payload-location"] == "detail"  # the consumer reads envelope["detail"]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_stack_rule_matches_the_pinned_eventbridge_mapping(environment: str) -> None:
    mapping = user_confirmed_channel(pinned_contract())["x-eventbridge"]
    built = Template.from_stack(
        MoneyServiceStack(
            cdk.App(),
            "Service",
            environment=environment,
            verifier_parameter=f"/raad/{environment}/verifier",
            correlation_secret_arn=f"arn:aws:secretsmanager:us-east-1:111111111111:secret:raad/{environment}/correlation-hmac-abc",
            edge_role_arn=f"arn:aws:iam::111111111111:role/raad-{environment}-edge",
            event_bus_arn="arn:aws:events:us-east-1:111111111111:event-bus/raad-platform",
            alarm_topic_arn="arn:aws:sns:us-east-1:111111111111:raad-alarms",
        )
    )
    rules = built.find_resources("AWS::Events::Rule")
    assert len(rules) == 1
    assert next(iter(rules.values()))["Properties"]["EventPattern"] == {
        "source": [mapping["source"]],
        "detail-type": [mapping["detail-type"]],
        "detail": {"environment": [environment]},
    }


def test_consumer_requires_exactly_the_pinned_payload_fields() -> None:
    required = payload_schema(pinned_contract())["required"]
    assert len(required) == len(set(required))
    assert set(consumer.REQUIRED_PAYLOAD_FIELDS) == set(required)
    assert len(consumer.REQUIRED_PAYLOAD_FIELDS) == len(required)


def test_valid_detail_fixture_follows_the_pinned_schema() -> None:
    schema = payload_schema(pinned_contract())
    detail = valid_detail()
    assert set(detail) == set(schema["required"])
    properties = schema["properties"]
    assert re.fullmatch(properties["specVersion"]["pattern"], detail["specVersion"])
    assert detail["environment"] in properties["environment"]["enum"]
    parsed = consumer.parse_event(envelope(detail), environment="dev", now=NOW)
    assert parsed.user_id == "user-123"


def test_consumer_refuses_an_event_missing_any_pinned_required_field() -> None:
    for name in payload_schema(pinned_contract())["required"]:
        detail = valid_detail()
        del detail[name]
        with pytest.raises(consumer.InvalidEvent):
            consumer.parse_event(envelope(detail), environment="dev", now=NOW)
