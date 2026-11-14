"""Real DynamoDB Local uniqueness and batch directory lookups."""

import pytest
from botocore.exceptions import ClientError

from raad_money.storage import WalletDirectory


def test_directory_resolves_unicode_normalized_email(directory_table: str) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", "  Pe\u0301rson@Example.COM  ", "Person")
    assert directory.find_by_email("PéRSON@example.com")["holder_id"] == "holder-1"


def test_directory_rejects_duplicate_normalized_email(directory_table: str) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", "person@example.com", "One")
    with pytest.raises(ClientError):
        directory.register("holder-2", " PERSON@EXAMPLE.COM ", "Two")


def test_directory_lookup_does_not_match_partial_email(directory_table: str) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", "person@example.com", "Person")
    with pytest.raises(ValueError):
        directory.find_by_email("person@exam")


def test_directory_batch_lookup_returns_profiles(directory_table: str) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", "one@example.com", "One")
    directory.register("holder-2", "two@example.com", "Two")
    result = directory.get_many(["holder-1", "holder-2", "holder-1"])
    assert result["holder-1"]["display_name"] == "One"
    assert set(result) == {"holder-1", "holder-2"}


def test_directory_batch_lookup_is_bounded_to_dynamodb_batch_size(directory_table: str) -> None:
    directory = WalletDirectory(directory_table)
    assert directory.get_many([f"missing-{index}" for index in range(101)]) == {}


def test_directory_returns_not_found_for_missing_holder(directory_table: str) -> None:
    directory = WalletDirectory(directory_table)
    with pytest.raises(LookupError):
        directory.find_by_email("missing@example.com")
