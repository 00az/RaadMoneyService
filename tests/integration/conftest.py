"""Disposable isolated DynamoDB Local tables shared by boundary tests."""

import os
from uuid import uuid4

import boto3
import pytest


@pytest.fixture
def dynamodb_resource():
    return boto3.resource("dynamodb", endpoint_url=os.environ.get("DYNAMODB_ENDPOINT", "http://127.0.0.1:8000"), region_name="us-east-1", aws_access_key_id="local", aws_secret_access_key="local")


@pytest.fixture
def ledger_table(dynamodb_resource):
    yield from _table(dynamodb_resource, "ledger")


@pytest.fixture
def directory_table(dynamodb_resource):
    yield from _table(dynamodb_resource, "directory")


def _table(resource, prefix: str):
    name = f"{prefix}-{uuid4().hex}"
    resource.create_table(TableName=name, KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}], AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST").wait_until_exists()
    try:
        yield name
    finally:
        resource.Table(name).delete()
