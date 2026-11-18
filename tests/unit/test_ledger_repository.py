"""Ledger repository transactional behavior with a DynamoDB-shaped fake."""

from decimal import Decimal
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from raad_money.storage import LedgerRepository


def repository() -> tuple[LedgerRepository, Mock]:
    table = Mock()
    table.name = "ledger-test"
    table.meta.client.transact_write_items.return_value = {}
    return LedgerRepository("ledger-test", resource=Mock(Table=Mock(return_value=table))), table


def test_opening_wallet_writes_owner_and_wallet_in_one_transaction() -> None:
    repo, table = repository()
    table.get_item.side_effect = [{}, {}]
    item, created = repo.ensure_wallet("holder-1")
    assert created is True
    assert item["balance_halalas"] == item["opening_balance_halalas"] == 100_000
    assert len(table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]) == 2


def test_existing_wallet_is_returned_without_crediting_again() -> None:
    repo, table = repository()
    existing = {"wallet_id": "w1", "balance_halalas": 12}
    repo.get_wallet = Mock(return_value=existing)  # type: ignore[method-assign]
    assert repo.ensure_wallet("holder-1") == (existing, False)
    table.meta.client.transact_write_items.assert_not_called()


def commit(amount: int = 100) -> object:
    from raad_money.ledger import TransferOutcome
    from raad_money.transfers import TransferCommit, WalletState

    return TransferCommit(
        owner_id="holder-s",
        idempotency_key="key-0000000000001",
        sender=WalletState("s", "holder-s", 300, 2, "2026-11-14T00:00:00.000Z#a"),
        recipient=WalletState("r", "holder-r", 20, 7, ""),
        sender_balance_after=300 - amount,
        recipient_balance_after=20 + amount,
        sort_key="2026-11-16T00:00:00.000Z#2bc440be-a85a-4cf8-9bf1-128fc7214c60",
        outcome=TransferOutcome.completed(
            fingerprint="a" * 64,
            transfer_id="2bc440be-a85a-4cf8-9bf1-128fc7214c60",
            reference="R-23456789ABCD",
            amount_halalas=amount,
            balance_after_halalas=300 - amount,
            completed_at="2026-11-16T00:00:00.000Z",
            recipient_profile_version_id="version-r",
        ),
    )


def test_commit_is_one_seven_item_transaction_with_ordinals_and_no_personal_data() -> None:
    repo, table = repository()
    repo.commit_transfer(commit())  # type: ignore[arg-type]
    writes = table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(writes) == 7
    sender, recipient = writes[0]["Update"], writes[1]["Update"]
    assert sender["ExpressionAttributeValues"][":sequence"] == 2
    assert recipient["ExpressionAttributeValues"][":next"] == 8
    canonical = writes[2]["Put"]["Item"]
    assert (canonical["senderOrdinal"], canonical["recipientOrdinal"]) == (3, 8)
    assert canonical["completedSortKey"].startswith("2026-11-16T00:00:00.000Z#")
    assert all(set(item["Put"]["Item"]) == {"pk", "sk", "transferId"} for item in writes[3:5])
    outcome = writes[5]["Put"]["Item"]
    assert outcome["pk"] == "REQUEST#holder-s" and outcome["outcome"] == "completed"
    assert writes[6]["Put"]["Item"]["pk"] == "REFERENCE#R-23456789ABCD"
    assert "@" not in repr(writes)


def test_refused_outcome_cannot_be_committed_as_a_transfer() -> None:
    from dataclasses import replace

    from raad_money.ledger import TransferOutcome

    repo, table = repository()
    refused = replace(commit(), outcome=TransferOutcome.refused("a" * 64, "SELF_TRANSFER"))  # type: ignore[type-var]
    with pytest.raises(ValueError):
        repo.commit_transfer(refused)
    table.meta.client.transact_write_items.assert_not_called()


def cancelled(reasons: list[str], *, in_message: bool = False) -> ClientError:
    response: dict = {"Error": {"Code": "TransactionCanceledException"}}
    if in_message:
        response["Error"]["Message"] = (
            "Transaction cancelled, please refer cancellation reasons for specific reasons "
            f"[{', '.join(reasons)}]"
        )
    else:
        response["CancellationReasons"] = [{"Code": code} for code in reasons]
    return ClientError(response, "TransactWriteItems")


@pytest.mark.parametrize("in_message", [False, True])
@pytest.mark.parametrize(
    "reasons,expected",
    [
        (["None"] * 5 + ["ConditionalCheckFailed", "None"], "OutcomeAlreadyRecorded"),
        (
            ["ConditionalCheckFailed"] + ["None"] * 4 + ["ConditionalCheckFailed", "None"],
            "OutcomeAlreadyRecorded",
        ),
        (["None"] * 6 + ["ConditionalCheckFailed"], "ReferenceCollision"),
        (["None", "ConditionalCheckFailed"] + ["None"] * 5, "WalletChanged"),
        (["TransactionConflict"] + ["None"] * 6, "WalletChanged"),
        (["None", "None", "ConditionalCheckFailed"] + ["None"] * 4, "TransientStoreError"),
    ],
)
def test_cancellation_reasons_map_to_service_decisions(
    reasons: list[str], expected: str, in_message: bool
) -> None:
    from raad_money import store_errors

    repo, table = repository()
    table.meta.client.transact_write_items.side_effect = cancelled(reasons, in_message=in_message)
    with pytest.raises(getattr(store_errors, expected)):
        repo.commit_transfer(commit())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "failure,expected",
    [
        (
            ClientError({"Error": {"Code": "InternalServerError"}}, "TransactWriteItems"),
            "CommitUncertain",
        ),
        (
            ClientError({"Error": {"Code": "ThrottlingException"}}, "TransactWriteItems"),
            "TransientStoreError",
        ),
        (
            ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems"),
            "TransientStoreError",
        ),
    ],
)
def test_service_errors_are_uncertain_or_transient(failure: Exception, expected: str) -> None:
    from raad_money import store_errors

    repo, table = repository()
    table.meta.client.transact_write_items.side_effect = failure
    with pytest.raises(getattr(store_errors, expected)):
        repo.commit_transfer(commit())  # type: ignore[arg-type]


def test_lost_connection_during_commit_is_uncertain_not_failed() -> None:
    from botocore.exceptions import ReadTimeoutError

    from raad_money.store_errors import CommitUncertain

    repo, table = repository()
    table.meta.client.transact_write_items.side_effect = ReadTimeoutError(endpoint_url="local")
    with pytest.raises(CommitUncertain):
        repo.commit_transfer(commit())  # type: ignore[arg-type]


def test_refusal_is_guarded_only_when_balance_decided_it() -> None:
    from raad_money.ledger import TransferOutcome
    from raad_money.transfers import WalletState

    repo, table = repository()
    refusal = TransferOutcome.refused("b" * 64, "INSUFFICIENT_FUNDS")
    repo.record_refusal("holder-s", "key-0000000000001", refusal, balance_guard=None)
    assert len(table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]) == 1
    guard = WalletState("s", "holder-s", 50, 3, "")
    repo.record_refusal("holder-s", "key-0000000000002", refusal, balance_guard=guard)
    items = table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
    check = items[1]["ConditionCheck"]["ExpressionAttributeValues"]
    assert check == {":balance": 50, ":sequence": 3}


def test_outcome_reads_are_strong_and_keyed_by_sender_and_key() -> None:
    from raad_money.ledger import TransferOutcome

    repo, table = repository()
    table.get_item.return_value = {
        "Item": TransferOutcome.refused("c" * 64, "SELF_TRANSFER").as_item()
    }
    assert repo.get_outcome("holder-s", "key-0000000000001").code == "SELF_TRANSFER"
    kwargs = table.get_item.call_args.kwargs
    assert kwargs["ConsistentRead"] is True
    assert kwargs["Key"] == {"pk": "REQUEST#holder-s", "sk": "KEY#key-0000000000001"}
    table.get_item.return_value = {}
    assert repo.get_outcome("holder-s", "key-0000000000003") is None
    with pytest.raises(ValueError):
        repo.get_outcome("", "key")


def test_history_is_strongly_consistent_and_newest_first() -> None:
    repo, table = repository()
    table.query.return_value = {"Items": []}
    repo.history("w1", 20)
    kwargs = table.query.call_args.kwargs
    assert kwargs["ConsistentRead"] is True
    assert kwargs["ScanIndexForward"] is False
    assert kwargs["Limit"] == 21


def test_history_page_size_is_bounded() -> None:
    repo, table = repository()
    table.query.return_value = {"Items": []}
    repo.history("w1", 1000)
    assert table.query.call_args.kwargs["Limit"] == 101


def test_wallet_reads_follow_owner_pointer_and_handle_missing_records() -> None:
    repo, table = repository()
    table.get_item.side_effect = [
        {"Item": {"wallet_pk": "WALLET#w1"}},
        {"Item": {"wallet_id": "w1", "balance_halalas": Decimal(12)}},
        {},
    ]
    assert repo.get_wallet("h1")["balance_halalas"] == 12
    assert repo.get_wallet("h2") is None
    table.get_item.side_effect = [{"Item": {"wallet_pk": "WALLET#missing"}}, {}]
    assert repo.get_wallet("h3") is None


def test_wallet_by_id_and_deduplicated_batch_lookup() -> None:
    repo, table = repository()
    table.get_item.return_value = {"Item": {"wallet_id": "w1", "balance_halalas": Decimal(2)}}
    assert repo.get_wallet_by_id("w1")["balance_halalas"] == 2
    table.get_item.return_value = {}
    assert repo.get_wallet_by_id("missing") is None
    repo._batch_get = Mock(return_value=[{"wallet_id": "w1", "balance_halalas": 2}])  # type: ignore[method-assign]
    assert list(repo.get_wallets_by_ids(["w1", "w1"])) == ["w1"]
    assert len(repo._batch_get.call_args.args[1]) == 1


def test_wallet_creation_recovers_from_transaction_race() -> None:
    repo, table = repository()
    repo.get_wallet = Mock(side_effect=[None, {"wallet_id": "winner", "balance_halalas": 100}])  # type: ignore[method-assign]
    table.meta.client.transact_write_items.side_effect = ClientError(
        {"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems"
    )
    assert repo.ensure_wallet("h1") == ({"wallet_id": "winner", "balance_halalas": 100}, False)


def test_wallet_creation_reraises_non_race_and_unresolved_race() -> None:
    repo, table = repository()
    repo.get_wallet = Mock(side_effect=[None, None])  # type: ignore[method-assign]
    table.meta.client.transact_write_items.side_effect = ClientError(
        {"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems"
    )
    with pytest.raises(ClientError):
        repo.ensure_wallet("h1")
    table.meta.client.transact_write_items.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "TransactWriteItems"
    )
    repo.get_wallet = Mock(return_value=None)  # type: ignore[method-assign]
    with pytest.raises(ClientError):
        repo.ensure_wallet("h2")


def test_batch_lookup_retries_unprocessed_items_and_normalizes() -> None:
    repo, table = repository()
    encoded = {"wallet_id": "w1", "balance_halalas": Decimal(3)}
    table.meta.client.batch_get_item.side_effect = [
        {
            "Responses": {table.name: []},
            "UnprocessedKeys": {table.name: {"Keys": [{"pk": "WALLET#w1", "sk": "WALLET"}]}},
        },
        {"Responses": {table.name: [encoded]}, "UnprocessedKeys": {}},
    ]
    result = repo._batch_get(table, [{"pk": "WALLET#w1", "sk": "WALLET"}])
    assert result[0]["balance_halalas"] == 3
    assert table.meta.client.batch_get_item.call_count == 2


def test_batch_lookup_stops_after_bounded_retries() -> None:
    repo, table = repository()
    key = {"pk": "WALLET#w1", "sk": "WALLET"}
    table.meta.client.batch_get_item.return_value = {
        "UnprocessedKeys": {table.name: {"Keys": [key]}}
    }
    with pytest.raises(RuntimeError, match="bounded batch lookup"):
        repo._batch_get(table, [{"pk": "WALLET#w1", "sk": "WALLET"}])
    assert table.meta.client.batch_get_item.call_count == 3


def test_batch_lookup_chunks_requests_at_one_hundred_keys() -> None:
    repo, table = repository()
    keys = [{"pk": f"WALLET#w{i}", "sk": "WALLET"} for i in range(101)]
    table.meta.client.batch_get_item.return_value = {"Responses": {table.name: []}}
    assert repo._batch_get(table, keys) == []
    assert table.meta.client.batch_get_item.call_count == 2


def test_history_normalizes_money_values() -> None:
    repo, table = repository()
    identity = "2bc440be-a85a-4cf8-9bf1-128fc7214c60"
    stamp = "2026-11-14T00:00:00.000Z"
    table.query.return_value = {
        "Items": [{"pk": "WALLET#w1", "sk": f"TRANSFER#{stamp}#{identity}", "transferId": identity}]
    }
    repo._batch_get = Mock(
        return_value=[
            {
                "transferId": identity,
                "reference": "TXN-ONE",
                "senderWalletId": "w1",
                "recipientWalletId": "w2",
                "amountHalalas": Decimal(7),
                "currency": "SAR",
                "completedAt": stamp,
            }
        ]
    )  # type: ignore[method-assign]
    assert repo.history("w1")[0]["amount_halalas"] == 7


def test_history_missing_canonical_row_is_safe_failure() -> None:
    from raad_money.storage import HistoryIntegrityError

    repo, table = repository()
    table.query.return_value = {"Items": [{"transferId": "missing", "sk": "TRANSFER#missing"}]}
    repo._batch_get = Mock(return_value=[])  # type: ignore[method-assign]
    with pytest.raises(HistoryIntegrityError):
        repo.history("w1")
    table.query.return_value = {"Items": [{"amount_halalas": 7}]}
    with pytest.raises(HistoryIntegrityError):
        repo.history("w1")


def test_history_reorders_batches_without_fetching_lookahead() -> None:
    from raad_money.ledger import CompletedTransfer

    repo, table = repository()
    ids = [
        "00000000-0000-4000-8000-000000000003",
        "00000000-0000-4000-8000-000000000002",
        "00000000-0000-4000-8000-000000000001",
    ]
    stamp = "2026-11-14T00:00:00.000Z"
    table.query.return_value = {
        "Items": [{"transferId": i, "sk": f"TRANSFER#{stamp}#{i}"} for i in ids]
    }
    rows = [CompletedTransfer(i, "TXN-" + i, "w1", "w2", 7, stamp).as_item() for i in ids[:2]]
    repo._batch_get = Mock(return_value=rows[::-1])  # type: ignore[method-assign]
    page = repo.history("w1", 2)
    assert [r["transfer_id"] for r in page] == ids[:2]
    assert page.has_more
    assert len(repo._batch_get.call_args.args[1]) == 2


def test_history_refuses_wrong_wallet_or_sort_key() -> None:
    from raad_money.ledger import CompletedTransfer
    from raad_money.storage import HistoryIntegrityError

    repo, table = repository()
    identity, stamp = "2bc440be-a85a-4cf8-9bf1-128fc7214c60", "2026-11-14T00:00:00.000Z"
    table.query.return_value = {
        "Items": [{"transferId": identity, "sk": f"TRANSFER#{stamp}#{identity}"}]
    }
    repo._batch_get = Mock(
        return_value=[CompletedTransfer(identity, "TXN-ONE", "w2", "w3", 1, stamp).as_item()]
    )  # type: ignore[method-assign]
    with pytest.raises(HistoryIntegrityError):
        repo.history("w1")


@pytest.mark.parametrize("value", [True, Decimal("1.5"), 1.5, "1"])
def test_money_item_rejects_non_integer_halalas(value: object) -> None:
    from raad_money.storage import _normalize_money_item

    with pytest.raises((ValueError, TypeError)):
        _normalize_money_item({"balance_halalas": value})
