"""Signals from the Ledger store that the transfer service must interpret, never guess past."""

from __future__ import annotations


class TransientStoreError(RuntimeError):
    """Nothing was confirmed; the caller gets a safe 503 and may retry with the same key."""


class OutcomeAlreadyRecorded(Exception):
    """The (sender, key) pair already holds a final outcome; reread and replay it."""


class WalletChanged(Exception):
    """A wallet's balance or sequence moved, or a concurrent transaction won; re-evaluate."""


class ReferenceCollision(Exception):
    """The generated user-facing reference is already taken; generate another."""


class CommitUncertain(Exception):
    """The commit may or may not have applied; resolve only by a strong outcome reread."""
