"""U2 test-first money core: fingerprints, immutable outcomes, replay and completion order."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from raad_money.ledger import (
    MAX_TRANSFER_HALALAS,
    MIN_TRANSFER_HALALAS,
    REFUSAL_STATUS,
    CompletionClockBehind,
    IdempotencyKeyReused,
    InsufficientFunds,
    LedgerRules,
    TransferOutcome,
    TransferRequest,
    Wallet,
    business_refusal,
    completion_sort_key,
    match_existing_outcome,
    plan_completion,
)

TRANSFER_ID = "6f1c2c7e-6a4b-4d55-9a57-1f2d3c4b5a69"
LATER_ID = "ffffffff-ffff-4fff-bfff-ffffffffffff"
EARLIER_ID = "00000000-0000-4000-8000-000000000000"


def completed(fingerprint: str = "a" * 64, **overrides: object) -> TransferOutcome:
    values: dict[str, object] = {
        "fingerprint": fingerprint,
        "transfer_id": TRANSFER_ID,
        "reference": "R-23456789ABCD",
        "amount_halalas": 2_500,
        "balance_after_halalas": 97_500,
        "completed_at": "2026-11-16T10:00:00.000Z",
        "recipient_profile_version_id": "c0ffee00-0000-4000-8000-000000000001",
    }
    values.update(overrides)
    return TransferOutcome.completed(**values)  # type: ignore[arg-type]


def test_fingerprint_is_shared_by_normalized_equivalents() -> None:
    plain = TransferRequest("a@example.test", 100).fingerprint("sub-1")
    decorated = TransferRequest("  A@EXAMPLE.test ", 100).fingerprint("sub-1")
    assert plain == decorated


def test_fingerprint_separates_amount_recipient_and_sender_without_raw_email() -> None:
    base = TransferRequest("a@example.test", 100)
    digest = base.fingerprint("sub-1")
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert "example" not in digest
    assert digest != TransferRequest("a@example.test", 101).fingerprint("sub-1")
    assert digest != TransferRequest("b@example.test", 100).fingerprint("sub-1")
    assert digest != base.fingerprint("sub-2")


@pytest.mark.parametrize(
    "code,status",
    [
        ("RECIPIENT_NOT_FOUND", 404),
        ("SELF_TRANSFER", 409),
        ("AMOUNT_OUT_OF_RANGE", 422),
        ("INSUFFICIENT_FUNDS", 422),
    ],
)
def test_refusal_outcome_has_contract_status_and_no_money_fields(code: str, status: int) -> None:
    outcome = TransferOutcome.refused("b" * 64, code)
    assert outcome.status == status
    assert REFUSAL_STATUS[code] == status
    assert outcome.transfer_id is None and outcome.balance_after_halalas is None


def test_unknown_refusal_code_is_rejected() -> None:
    with pytest.raises(ValueError):
        TransferOutcome.refused("b" * 64, "IDEMPOTENCY_KEY_REUSED")


def test_completed_outcome_round_trips_without_personal_fields() -> None:
    outcome = completed()
    item = outcome.as_item()
    assert outcome.status == 201
    assert not {"email", "recipientEmail", "displayName", "recipient"} & set(item)
    restored = TransferOutcome.from_item(
        {key: Decimal(value) if isinstance(value, int) else value for key, value in item.items()}
    )
    assert restored == outcome
    assert isinstance(restored.amount_halalas, int)


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount_halalas", MIN_TRANSFER_HALALAS - 1),
        ("amount_halalas", MAX_TRANSFER_HALALAS + 1),
        ("balance_after_halalas", -1),
        ("reference", "TXN-LEGACY"),
        ("transfer_id", "not-a-uuid"),
        ("completed_at", "2026-11-16T10:00:00+03:00"),
        ("recipient_profile_version_id", ""),
        ("fingerprint", "short"),
    ],
)
def test_completed_outcome_rejects_invalid_business_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        completed(**{field: value})


def test_refused_outcome_cannot_carry_success_fields() -> None:
    with pytest.raises(ValueError):
        TransferOutcome(fingerprint="b" * 64, code="SELF_TRANSFER", transfer_id=TRANSFER_ID)


def test_outcome_is_immutable() -> None:
    outcome = completed()
    with pytest.raises(FrozenInstanceError):
        outcome.balance_after_halalas = 1  # type: ignore[misc]


def test_matching_fingerprint_replays_the_first_outcome_unchanged() -> None:
    first = TransferOutcome.refused("c" * 64, "INSUFFICIENT_FUNDS")
    assert match_existing_outcome(first, "c" * 64) is first


def test_different_fingerprint_is_key_reuse_and_never_replaces_the_first() -> None:
    first = completed("d" * 64)
    with pytest.raises(IdempotencyKeyReused):
        match_existing_outcome(first, "e" * 64)
    assert first.fingerprint == "d" * 64


def test_completion_after_both_wallets_produces_millisecond_utc_key() -> None:
    now = datetime(2026, 11, 16, 10, 0, 0, 123456, tzinfo=UTC)
    completed_at, sort_key = plan_completion(
        now, TRANSFER_ID, ["2026-11-16T09:59:59.999Z#" + LATER_ID, ""]
    )
    assert completed_at == "2026-11-16T10:00:00.123Z"
    assert sort_key == completion_sort_key(completed_at, TRANSFER_ID)


def test_same_millisecond_completion_uses_transfer_id_tiebreak() -> None:
    now = datetime(2026, 11, 16, 10, 0, 0, tzinfo=UTC)
    prior = completion_sort_key("2026-11-16T10:00:00.000Z", EARLIER_ID)
    _, sort_key = plan_completion(now, LATER_ID, [prior])
    assert sort_key > prior
    with pytest.raises(CompletionClockBehind) as behind:
        plan_completion(
            now, EARLIER_ID, [completion_sort_key("2026-11-16T10:00:00.000Z", LATER_ID)]
        )
    assert behind.value.wait_ms >= 1


def test_prior_completion_ahead_of_clock_is_never_overtaken_by_invention() -> None:
    now = datetime(2026, 11, 16, 10, 0, 0, tzinfo=UTC)
    ahead = completion_sort_key(
        (now + timedelta(milliseconds=30))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        EARLIER_ID,
    )
    with pytest.raises(CompletionClockBehind) as behind:
        plan_completion(now, LATER_ID, [ahead])
    assert behind.value.wait_ms >= 30


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError):
        plan_completion(datetime(2026, 11, 16), TRANSFER_ID, [])


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=2),
            st.integers(min_value=0, max_value=2),
            st.integers(min_value=-1_000, max_value=60_000),
        ),
        max_size=60,
    )
)
def test_any_transfer_sequence_conserves_money_and_never_goes_negative(
    steps: list[tuple[int, int, int]],
) -> None:
    wallets = [Wallet(f"w{index}", 100_000) for index in range(3)]
    total = sum(wallet.balance_halalas for wallet in wallets)
    for sender_index, recipient_index, amount in steps:
        request = TransferRequest("someone@example.test", amount)
        sender, recipient = wallets[sender_index], wallets[recipient_index]
        refusal = business_refusal(
            request,
            recipient_exists=True,
            own_wallet=sender_index == recipient_index,
            balance=sender.balance_halalas,
        )
        if refusal is None:
            decision = LedgerRules.transfer(sender, recipient, request.amount_halalas)
            wallets[sender_index], wallets[recipient_index] = decision.sender, decision.recipient
        else:
            assert refusal in REFUSAL_STATUS
        assert sum(wallet.balance_halalas for wallet in wallets) == total
        assert min(wallet.balance_halalas for wallet in wallets) >= 0


def test_refused_outcome_round_trips_from_store() -> None:
    refused = TransferOutcome.refused("f" * 64, "AMOUNT_OUT_OF_RANGE")
    assert refused.as_item() == {
        "fingerprint": "f" * 64,
        "outcome": "refused",
        "code": "AMOUNT_OUT_OF_RANGE",
    }
    assert TransferOutcome.from_item(refused.as_item()) == refused


@pytest.mark.parametrize(
    "change",
    [
        {"outcome": "pending"},
        {"amountHalalas": Decimal("2500.5")},
        {"amountHalalas": True},
        {"balanceAfterHalalas": "97500"},
    ],
)
def test_corrupt_stored_outcome_fails_closed(change: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        TransferOutcome.from_item({**completed().as_item(), **change})


def test_non_text_recipient_and_missing_sender_are_invalid() -> None:
    with pytest.raises(ValueError):
        TransferRequest(12345, 100)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TransferRequest("a@example.test", 100).fingerprint("")


def test_insufficient_funds_rule_still_guards_arithmetic_directly() -> None:
    with pytest.raises(InsufficientFunds):
        LedgerRules.transfer(Wallet("s", 99), Wallet("r", 0), 100)
