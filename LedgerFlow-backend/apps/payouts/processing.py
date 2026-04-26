"""
Payout processing service.

Handles the full lifecycle of a payout after creation:
- state transitions (strict machine)
- simulated bank outcome
- atomic ledger refund on failure
- retry/timeout logic

Kept separate from services.py (creation) for clarity.
"""

import logging
import random
from django.db import transaction
from django.utils import timezone

from apps.ledger.models import LedgerEntry
from apps.payouts.models import Payout

logger = logging.getLogger(__name__)

# Simulated bank outcomes
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_PENDING = "pending"   # bank delayed, stay in PROCESSING

MAX_ATTEMPTS = 3
STUCK_THRESHOLD_SECONDS = 30


def _simulate_bank_outcome() -> str:
    """
    Simulate external bank API response.
    70% success / 20% failure / 10% delayed (stay processing)
    """
    roll = random.random()
    if roll < 0.70:
        return OUTCOME_SUCCESS
    elif roll < 0.90:
        return OUTCOME_FAILURE
    else:
        return OUTCOME_PENDING


def _issue_refund(payout: Payout) -> None:
    """
    Create a CREDIT ledger entry to return held funds.
    Must be called inside an active transaction.atomic() block.
    """
    # Guard: never double-refund
    already_refunded = LedgerEntry.objects.filter(
        reference_type="PAYOUT_REFUND",
        reference_id=str(payout.id),
    ).exists()

    if already_refunded:
        logger.warning("Refund already exists for payout %s — skipping.", payout.id)
        return

    LedgerEntry.objects.create(
        merchant=payout.merchant,
        type=LedgerEntry.EntryType.CREDIT,
        amount_paise=payout.amount_paise,
        reference_type="PAYOUT_REFUND",
        reference_id=str(payout.id),
    )
    logger.info("Refund issued for payout %s — %sp returned.", payout.id, payout.amount_paise)


def process_payout_logic(payout_id: str) -> str:
    """
    Core processing logic for a single payout.
    Called by the Celery task.

    Returns the final status string.
    """
    with transaction.atomic():
        # Lock the payout row — prevents concurrent workers racing on same payout
        try:
            payout = Payout.objects.select_for_update().get(id=payout_id)
        except Payout.DoesNotExist:
            logger.error("Payout %s not found.", payout_id)
            return "NOT_FOUND"

        # Idempotent: already in a terminal state, nothing to do
        if payout.status in (Payout.Status.COMPLETED, Payout.Status.FAILED):
            logger.info("Payout %s already terminal (%s) — skipping.", payout_id, payout.status)
            return payout.status

        # Move PENDING → PROCESSING (validate transition)
        if payout.status == Payout.Status.PENDING:
            payout.status = Payout.Status.PROCESSING
            payout.attempts += 1
            payout.save(update_fields=["status", "attempts", "updated_at"])
            logger.info("Payout %s → PROCESSING (attempt %s)", payout_id, payout.attempts)

        # Simulate bank call
        outcome = _simulate_bank_outcome()
        logger.info("Payout %s bank outcome: %s", payout_id, outcome)

        if outcome == OUTCOME_SUCCESS:
            # PROCESSING → COMPLETED
            # Ledger already has the DEBIT hold — no new entry needed
            payout.status = Payout.Status.COMPLETED
            payout.save(update_fields=["status", "updated_at"])
            logger.info("Payout %s COMPLETED.", payout_id)
            return Payout.Status.COMPLETED

        elif outcome == OUTCOME_FAILURE:
            # PROCESSING → FAILED + atomic refund
            payout.status = Payout.Status.FAILED
            payout.save(update_fields=["status", "updated_at"])
            _issue_refund(payout)
            logger.info("Payout %s FAILED — funds refunded.", payout_id)
            return Payout.Status.FAILED

        else:
            # Bank delayed — stay in PROCESSING, task will be retried
            logger.info("Payout %s still PROCESSING — bank delayed.", payout_id)
            return Payout.Status.PROCESSING


def handle_stuck_payouts() -> None:
    """
    Called by the periodic Celery beat task.
    Finds PROCESSING payouts stuck longer than STUCK_THRESHOLD_SECONDS.

    - attempts <= MAX_ATTEMPTS → re-queue for processing
    - attempts > MAX_ATTEMPTS  → force FAILED + refund
    """
    from apps.payouts.tasks import process_payout  # local import avoids circular

    cutoff = timezone.now() - timezone.timedelta(seconds=STUCK_THRESHOLD_SECONDS)

    with transaction.atomic():
        stuck = Payout.objects.filter(
            status=Payout.Status.PROCESSING,
            updated_at__lt=cutoff,
        ).select_for_update(skip_locked=True)

        for payout in stuck:
            if payout.attempts <= MAX_ATTEMPTS:
                logger.info(
                    "Retrying stuck payout %s (attempt %s/%s)",
                    payout.id, payout.attempts, MAX_ATTEMPTS,
                )
                payout.attempts += 1
                payout.save(update_fields=["attempts", "updated_at"])
                process_payout.delay(str(payout.id))
            else:
                logger.warning(
                    "Payout %s exceeded max attempts — forcing FAILED + refund.",
                    payout.id,
                )
                payout.status = Payout.Status.FAILED
                payout.save(update_fields=["status", "updated_at"])
                _issue_refund(payout)
