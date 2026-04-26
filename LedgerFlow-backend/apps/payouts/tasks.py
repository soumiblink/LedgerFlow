"""
Celery tasks for payout processing.
"""

import logging
from celery import shared_task

from apps.payouts.processing import process_payout_logic, handle_stuck_payouts

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def process_payout(self, payout_id: str):
    """
    Background task: process a single payout through its lifecycle.
    Idempotent — safe to call multiple times on the same payout.
    """
    try:
        final_status = process_payout_logic(payout_id)
        logger.info("process_payout task completed for %s — status: %s", payout_id, final_status)
        return final_status
    except Exception as exc:
        logger.exception("process_payout task failed for %s", payout_id)
        raise self.retry(exc=exc)


@shared_task
def retry_stuck_payouts():
    """
    Periodic task (Celery beat): find PROCESSING payouts stuck too long.
    - Retry if attempts <= MAX_ATTEMPTS
    - Force fail + refund if attempts > MAX_ATTEMPTS
    """
    logger.info("Running retry_stuck_payouts periodic task...")
    handle_stuck_payouts()
    logger.info("retry_stuck_payouts completed.")
