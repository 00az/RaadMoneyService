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


def test_transfer_uses_single_four_item_transaction() -> None:
    repo, table = repository()
    result = repo.transfer({"wallet_id": "s", "balance_halalas": 300}, {"wallet_id": "r", "balance_halalas": 20}, 100)
    assert result["sender_balance_halalas"] == 200
    assert len(table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]) == 4


def test_insufficient_funds_fails_before_persistence() -> None:
    repo, table = repository()
    with pytest.raises(ValueError, match="insufficient funds"):
        repo.transfer({"wallet_id": "s", "balance_halalas": 1}, {"wallet_id": "r", "balance_halalas": 2}, 3)
    table.meta.client.transact_write_items.assert_not_called()


def test_history_is_strongly_consistent_and_newest_first() -> None:
    repo, table = repository()
    table.query.return_value = {"Items": []}
    repo.history("w1", 20)
    kwargs = table.query.call_args.kwargs
    assert kwargs["ConsistentRead"] is True
    assert kwargs["ScanIndexForward"] is False
    assert kwargs["Limit"] == 20


def test_history_page_size_is_bounded() -> None:
    repo, table = repository()
    table.query.return_value = {"Items": []}
    repo.history("w1", 1000)
    assert table.query.call_args.kwargs["Limit"] == 100




def test_wallet_reads_follow_owner_pointer_and_handle_missing_records() -> None:
    repo, table = repository()
    table.get_item.side_effect = [{"Item": {"wallet_pk": "WALLET#w1"}}, {"Item": {"wallet_id": "w1", "balance_halalas": Decimal(12)}}, {}]
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
    table.meta.client.transact_write_items.side_effect = ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
    assert repo.ensure_wallet("h1") == ({"wallet_id": "winner", "balance_halalas": 100}, False)


def test_wallet_creation_reraises_non_race_and_unresolved_race() -> None:
    repo, table = repository()
    repo.get_wallet = Mock(side_effect=[None, None])  # type: ignore[method-assign]
    table.meta.client.transact_write_items.side_effect = ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
    with pytest.raises(ClientError):
        repo.ensure_wallet("h1")
    table.meta.client.transact_write_items.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "TransactWriteItems")
    repo.get_wallet = Mock(return_value=None)  # type: ignore[method-assign]
    with pytest.raises(ClientError):
        repo.ensure_wallet("h2")


def test_batch_lookup_retries_unprocessed_items_and_normalizes() -> None:
    repo, table = repository()
    from boto3.dynamodb.types import TypeSerializer
    encoded = {"wallet_id": TypeSerializer().serialize("w1"), "balance_halalas": TypeSerializer().serialize(Decimal(3))}
    table.meta.client.batch_get_item.side_effect = [
        {"Responses": {table.name: []}, "UnprocessedKeys": {table.name: {"Keys": [{"pk": TypeSerializer().serialize("WALLET#w1"), "sk": TypeSerializer().serialize("WALLET")}]}}},
        {"Responses": {table.name: [encoded]}, "UnprocessedKeys": {}},
    ]
    result = repo._batch_get(table, [{"pk": "WALLET#w1", "sk": "WALLET"}])
    assert result[0]["balance_halalas"] == 3
    assert table.meta.client.batch_get_item.call_count == 2


def test_batch_lookup_stops_after_bounded_retries() -> None:
    repo, table = repository()
    from boto3.dynamodb.types import TypeSerializer
    key = {"pk": TypeSerializer().serialize("WALLET#w1"), "sk": TypeSerializer().serialize("WALLET")}
    table.meta.client.batch_get_item.return_value = {"UnprocessedKeys": {table.name: {"Keys": [key]}}}
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
    table.query.return_value = {"Items": [{"amount_halalas": Decimal(7)}]}
    assert repo.history("w1")[0]["amount_halalas"] == 7


@pytest.mark.parametrize("value", [True, Decimal("1.5"), 1.5, "1"])

def test_money_item_rejects_non_integer_halalas(value: object) -> None:
    from raad_money.storage import _normalize_money_item
    with pytest.raises(ValueError):
        _normalize_money_item({"balance_halalas": value})
