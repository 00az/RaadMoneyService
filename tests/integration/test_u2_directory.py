"""U2 test-first WalletDirectory: unique ownership, exact lookup, versions and no leakage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from raad_money.storage import (
    EmailConflict,
    HolderExists,
    RecipientMissing,
    WalletDirectory,
)


def test_duplicate_normalized_email_is_a_safe_conflict_and_changes_nobody(directory_table) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", "Person@Example.com", "One")
    with pytest.raises(EmailConflict) as conflict:
        directory.register("holder-2", "  PERSON@example.COM ", "Two")
    assert "example" not in str(conflict.value).lower()
    assert directory.get_by_holder_id("holder-2") is None
    assert directory.find_by_email("person@example.com")["holder_id"] == "holder-1"


def test_registering_a_holder_twice_is_refused_without_overwrite(directory_table) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", "one@example.com", "One")
    with pytest.raises(HolderExists):
        directory.register("holder-1", "other@example.com", "Other")
    assert directory.get_by_holder_id("holder-1")["display_name"] == "One"
    with pytest.raises(RecipientMissing):
        directory.find_by_email("other@example.com")


def test_ensure_holder_for_another_users_email_raises_conflict(directory_table) -> None:
    directory = WalletDirectory(directory_table)
    directory.ensure_holder("holder-1", "shared@example.com", "One")
    with pytest.raises(EmailConflict):
        directory.ensure_holder("holder-2", "Shared@Example.com", "Two")
    assert directory.get_by_holder_id("holder-2") is None


def test_simultaneous_setups_for_one_holder_converge(directory_table) -> None:
    directory = WalletDirectory(directory_table)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(
            pool.map(lambda _: directory.ensure_holder("holder-1", "a@example.com", "A"), range(6))
        )
    profile = directory.get_by_holder_id("holder-1")
    assert profile is not None
    assert (
        directory.ensure_profile_version("holder-1")["profile_version_id"]
        == profile["profile_version_id"]
    )


def test_simultaneous_claims_of_one_email_leave_exactly_one_owner(directory_table) -> None:
    directory = WalletDirectory(directory_table)

    def claim(holder: str) -> str:
        try:
            directory.ensure_holder(holder, "race@example.com", holder)
            return "owner"
        except EmailConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, ("holder-1", "holder-2"))) == ["conflict", "owner"]


@pytest.mark.parametrize(
    "typed", ["Person@Example.COM", "  person@example.com\t", "person@example.com"]
)
def test_lookup_is_exact_after_normalization(directory_table, typed: str) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", "person@example.com", "Person")
    assert directory.find_by_email(typed)["holder_id"] == "holder-1"


@pytest.mark.parametrize(
    "typed", ["person@example.co", "erson@example.com", "person@example.com.sa"]
)
def test_partial_or_similar_addresses_never_match(directory_table, typed: str) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", "person@example.com", "Person")
    with pytest.raises(RecipientMissing):
        directory.find_by_email(typed)


def test_composed_and_decomposed_addresses_resolve_to_one_holder(directory_table) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", "Rémy@example.com", "Rémy")
    assert directory.find_by_email(" RéMY@EXAMPLE.com ")["holder_id"] == "holder-1"


def test_dangling_email_pointer_never_reveals_another_profile(directory_table) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", "one@example.com", "One")
    directory.register("holder-2", "two@example.com", "Two")
    directory.table.put_item(
        Item={"pk": "EMAIL#ghost@example.com", "sk": "HOLDER", "holder_id": "gone"}
    )
    with pytest.raises(RecipientMissing):
        directory.find_by_email("ghost@example.com")


def test_confirmed_spelling_is_kept_for_display_beside_the_key(directory_table) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", " Person@Example.COM ", "Person")
    profile = directory.find_by_email("person@example.com")
    assert (profile["email"], profile["email_normalized"]) == (
        "Person@Example.COM",
        "person@example.com",
    )


def test_profile_version_is_immutable_and_stable(directory_table) -> None:
    directory = WalletDirectory(directory_table)
    directory.register("holder-1", "one@example.com", "One")
    first = directory.ensure_profile_version("holder-1")
    directory.table.update_item(
        Key={"pk": "HOLDER#holder-1", "sk": "PROFILE"},
        UpdateExpression="SET display_name = :name",
        ExpressionAttributeValues={":name": "Renamed"},
    )
    again = directory.ensure_profile_version("holder-1")
    assert again == first and again["display_name"] == "One"
    assert directory.get_profile_version(first["profile_version_id"]) == first


def test_u1_holder_gets_exactly_one_version_under_concurrency(directory_table) -> None:
    directory = WalletDirectory(directory_table)
    directory.table.put_item(
        Item={
            "pk": "HOLDER#u1-holder",
            "sk": "PROFILE",
            "holder_id": "u1-holder",
            "email_normalized": "legacy@example.com",
            "display_name": "Legacy",
        }
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        versions = list(pool.map(lambda _: directory.ensure_profile_version("u1-holder"), range(4)))
    assert len({version["profile_version_id"] for version in versions}) == 1
    assert versions[0]["email"] == "legacy@example.com"


def test_missing_holder_or_version_is_never_invented(directory_table) -> None:
    from raad_money.storage import ProfileVersionMissing

    directory = WalletDirectory(directory_table)
    with pytest.raises(RecipientMissing):
        directory.ensure_profile_version("nobody")
    assert directory.get_profile_version("00000000-0000-4000-8000-000000000000") is None
    directory.register("holder-1", "one@example.com", "One")
    version_id = directory.get_by_holder_id("holder-1")["profile_version_id"]
    directory.table.delete_item(Key={"pk": f"PROFILE_VERSION#{version_id}", "sk": "VERSION"})
    with pytest.raises(ProfileVersionMissing):
        directory.ensure_profile_version("holder-1")


def test_directory_operations_print_no_personal_data(directory_table, capsys) -> None:
    directory = WalletDirectory(directory_table)
    directory.ensure_holder("holder-1", "quiet@example.com", "Quiet Person")
    directory.find_by_email("quiet@example.com")
    with pytest.raises(EmailConflict):
        directory.ensure_holder("holder-2", "quiet@example.com", "Other")
    captured = capsys.readouterr()
    assert "quiet" not in (captured.out + captured.err).lower()
