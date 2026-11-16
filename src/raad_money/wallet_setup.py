"""The one idempotent wallet-setup procedure shared by PUT /wallet and the C2 consumer."""

from __future__ import annotations

from typing import Any

from raad_money.storage import LedgerRepository, WalletDirectory


def ensure_wallet_and_holder(
    ledger: LedgerRepository,
    directory: WalletDirectory,
    holder_id: str,
    email: str,
    display_name: str,
) -> tuple[dict[str, Any], bool]:
    """Open the wallet once (100000 halalas), then make the holder ready.

    Wallet commit and Directory readiness are sequential with repair, not one transaction:
    if the Directory step fails, a retry finds the wallet and repeats only that step, so the
    opening credit is never applied twice.
    """
    wallet, created = ledger.ensure_wallet(holder_id)
    directory.ensure_holder(holder_id, email, display_name)
    return wallet, created
