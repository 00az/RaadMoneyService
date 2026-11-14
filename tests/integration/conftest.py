"""Disposable isolated DynamoDB Local tables shared by boundary tests."""

import os
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
import pytest


@pytest.fixture(autouse=True)
def dynamodb_resource(monkeypatch):
    endpoint = os.environ.get("DYNAMODB_ENDPOINT", "http://127.0.0.1:8000")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("integration fixtures require a loopback DynamoDB Local endpoint")
    monkeypatch.setenv("DYNAMODB_ENDPOINT", endpoint)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "local")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "local")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CORRELATION_SECRET_ARN", "local-test-secret")
    return boto3.resource(
        "dynamodb",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )


@pytest.fixture(autouse=True)
def transactions_table(dynamodb_resource, monkeypatch):
    name = f"transactions-{uuid4().hex}"
    dynamodb_resource.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "transferId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "transferId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    ).wait_until_exists()
    monkeypatch.setenv("TRANSACTIONS_TABLE", name)
    try:
        yield name
    finally:
        dynamodb_resource.Table(name).delete()


@pytest.fixture
def ledger_table(dynamodb_resource):
    yield from _table(dynamodb_resource, "ledger")


@pytest.fixture
def directory_table(dynamodb_resource):
    yield from _table(dynamodb_resource, "directory")


def _table(resource, prefix: str):
    name = f"{prefix}-{uuid4().hex}"
    resource.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    ).wait_until_exists()
    try:
        yield name
    finally:
        resource.Table(name).delete()
