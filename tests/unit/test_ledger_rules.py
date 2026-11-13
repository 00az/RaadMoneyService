"""Test-first money rules: integer-only arithmetic and atomic transfer invariants."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from raad_money.ledger import InsufficientFunds, LedgerRules, Wallet, validate_amount


def test_whole_halalas_only_and_positive_amounts() -> None:
    assert validate_amount(1) == 1
    for invalid in (0, -1, True, 1.0, "100"):
        with pytest.raises(ValueError):
            validate_amount(invalid)  # type: ignore[arg-type]


def test_exact_balance_debit_is_accepted() -> None:
    sender, recipient = Wallet("s", 900), Wallet("r", 0)
    result = LedgerRules.transfer(sender, recipient, 900)
    assert (result.sender.balance_halalas, result.recipient.balance_halalas) == (0, 900)


def test_insufficient_balance_changes_neither_wallet() -> None:
    sender, recipient = Wallet("s", 99), Wallet("r", 7)
    with pytest.raises(InsufficientFunds):
        LedgerRules.transfer(sender, recipient, 100)
    assert (sender.balance_halalas, recipient.balance_halalas) == (99, 7)


def test_opening_credit_is_exactly_one_hundred_thousand_halalas() -> None:
    assert LedgerRules.opening_wallet("owner").balance_halalas == 100_000


def test_transfer_result_is_all_or_nothing() -> None:
    sender, recipient = Wallet("s", 700), Wallet("r", 300)
    result = LedgerRules.transfer(sender, recipient, 125)
    assert result.sender.balance_halalas + result.recipient.balance_halalas == 1_000


@given(st.integers(min_value=1, max_value=1_000_000), st.integers(min_value=0, max_value=1_000_000))
def test_conservation_and_nonnegative_balances(balance: int, amount: int) -> None:
    sender, recipient = Wallet("s", balance), Wallet("r", 17)
    before = sender.balance_halalas + recipient.balance_halalas
    if amount == 0:
        with pytest.raises(ValueError):
            LedgerRules.transfer(sender, recipient, amount)
    elif amount > balance:
        with pytest.raises(InsufficientFunds):
            LedgerRules.transfer(sender, recipient, amount)
    else:
        result = LedgerRules.transfer(sender, recipient, amount)
        assert result.sender.balance_halalas + result.recipient.balance_halalas == before
        assert min(result.sender.balance_halalas, result.recipient.balance_halalas) >= 0


@pytest.mark.parametrize("wallet_id,balance", [("", 1), ("w1", -1), ("w1", True), ("w1", 1.5)])
def test_wallet_rejects_invalid_identity_or_balance(wallet_id: str, balance: object) -> None:
    with pytest.raises(ValueError, match="wallet requires an id"):
        Wallet(wallet_id, balance)  # type: ignore[arg-type]


def test_opening_wallet_requires_owner_id() -> None:
    with pytest.raises(ValueError, match="wallet id is required"):
        LedgerRules.opening_wallet("")


def test_transfer_rejects_same_wallet_as_sender_and_recipient() -> None:
    wallet = Wallet("same", 100)
    with pytest.raises(ValueError, match="wallets must be different"):
        LedgerRules.transfer(wallet, Wallet("same", 0), 1)
