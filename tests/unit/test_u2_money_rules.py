"""U2 test-first request rules, distinct from historical completed-row validation."""

import re

import pytest

from raad_money.ledger import TransferRequest, business_refusal, new_reference


@pytest.mark.parametrize("amount", [100, 50_000])
def test_inclusive_request_amount_boundaries(amount):
    request = TransferRequest(" Person@Example.test ", amount)
    assert business_refusal(request, recipient_exists=True, own_wallet=False, balance=50_000) is None


@pytest.mark.parametrize("amount", [-1, 0, 99, 50_001])
def test_integer_out_of_range_is_final_business_refusal(amount):
    request = TransferRequest("person@example.test", amount)
    assert business_refusal(request, recipient_exists=True, own_wallet=False, balance=100_000) == "AMOUNT_OUT_OF_RANGE"


@pytest.mark.parametrize("amount", [True, 100.0, "100", 2**63, -(2**63)-1])
def test_invalid_wire_amount_has_no_valid_request(amount):
    with pytest.raises(ValueError):
        TransferRequest("person@example.test", amount)


def test_first_business_refusal_precedence_is_deterministic():
    invalid_range = TransferRequest("person@example.test", 99)
    valid = TransferRequest("person@example.test", 100)
    assert business_refusal(invalid_range, recipient_exists=False, own_wallet=True, balance=0) == "RECIPIENT_NOT_FOUND"
    assert business_refusal(invalid_range, recipient_exists=True, own_wallet=True, balance=0) == "AMOUNT_OUT_OF_RANGE"
    assert business_refusal(valid, recipient_exists=True, own_wallet=True, balance=0) == "SELF_TRANSFER"
    assert business_refusal(valid, recipient_exists=True, own_wallet=False, balance=99) == "INSUFFICIENT_FUNDS"


def test_reference_has_exact_uniform_alphabet_and_no_ambiguous_characters():
    references = {new_reference() for _ in range(500)}
    assert len(references) == 500
    assert all(re.fullmatch(r"R-[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{12}", item) for item in references)


def test_request_normalization_is_shared_with_fingerprint():
    assert TransferRequest("  E\u0301@EXAMPLE.test ", 100).recipient_email == "é@example.test"
    for email in ("", "a", "a@@example.test", "a@example", "a" * 255 + "@example.test"):
        with pytest.raises(ValueError):
            TransferRequest(email, 100)
