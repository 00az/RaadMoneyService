"""WalletDirectory normalization, uniqueness writes and safe lookup behavior."""

from unittest.mock import Mock

import pytest

from raad_money.storage import RecipientMissing, WalletDirectory, normalize_email


def directory() -> tuple[WalletDirectory, Mock]:
    table = Mock()
    table.name = "directory-test"
    return WalletDirectory("directory-test", resource=Mock(Table=Mock(return_value=table))), table


def test_email_is_trimmed_and_lowercased() -> None:
    assert normalize_email("  Person@Example.COM ") == "person@example.com"


def test_invalid_email_is_rejected_at_boundary() -> None:
    with pytest.raises(ValueError):
        normalize_email("not-an-email")


def test_registration_conditionally_writes_profile_unique_email_key_and_version() -> None:
    repo, table = directory()
    repo.register("holder-1", "A@EXAMPLE.COM", "Alex")
    writes = table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(writes) == 3
    assert all(w["Put"]["ConditionExpression"] == "attribute_not_exists(pk)" for w in writes)
    profile, email_key, version = (w["Put"]["Item"] for w in writes)
    assert email_key["pk"] == "EMAIL#a@example.com"
    assert profile["profile_version_id"] == version["profile_version_id"]
    assert (profile["email"], version["email"]) == ("A@EXAMPLE.COM", "A@EXAMPLE.COM")


def test_recipient_lookup_uses_exact_normalized_key() -> None:
    repo, table = directory()
    table.get_item.side_effect = [
        {"Item": {"holder_id": "h1"}},
        {"Item": {"holder_id": "h1", "display_name": "Alex"}},
    ]
    assert repo.find_by_email(" A@EXAMPLE.COM ")["display_name"] == "Alex"
    assert table.get_item.call_args_list[0].kwargs["Key"]["pk"] == "EMAIL#a@example.com"


def test_unknown_recipient_raises_safe_domain_error() -> None:
    repo, table = directory()
    table.get_item.return_value = {}
    with pytest.raises(RecipientMissing):
        repo.find_by_email("missing@example.com")


def test_ensure_holder_does_not_overwrite_existing_profile() -> None:
    repo, table = directory()
    table.get_item.return_value = {"Item": {"holder_id": "h1"}}
    repo.ensure_holder("h1", "new@example.com", "New")
    table.meta.client.transact_write_items.assert_not_called()
