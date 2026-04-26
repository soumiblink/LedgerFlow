
from django.db.models import Q, Sum
from django.db.models.functions import Cast
from django.db.models import CharField

from apps.ledger.models import LedgerEntry
from apps.payouts.models import Payout


def get_merchant_balance(merchant_id) -> int:
    """
    Total balance in paise = SUM(CREDITS) - SUM(DEBITS).
    Single aggregation query. Returns 0 if no entries exist.
    """
    result = LedgerEntry.objects.filter(merchant_id=merchant_id).aggregate(
        total_credits=Sum(
            "amount_paise",
            filter=Q(type=LedgerEntry.EntryType.CREDIT),
            default=0,
        ),
        total_debits=Sum(
            "amount_paise",
            filter=Q(type=LedgerEntry.EntryType.DEBIT),
            default=0,
        ),
    )
    return result["total_credits"] - result["total_debits"]


def get_merchant_held_balance(merchant_id) -> int:
   
    non_terminal_statuses = [Payout.Status.PENDING, Payout.Status.PROCESSING]

    pending_payout_ids = (
        Payout.objects.filter(
            merchant_id=merchant_id,
            status__in=non_terminal_statuses,
        )
        .annotate(id_str=Cast("id", output_field=CharField()))
        .values_list("id_str", flat=True)
    )

    result = LedgerEntry.objects.filter(
        merchant_id=merchant_id,
        type=LedgerEntry.EntryType.DEBIT,
        reference_type="PAYOUT",
        reference_id__in=pending_payout_ids,
    ).aggregate(
        held=Sum("amount_paise", default=0)
    )

    return result["held"]


def get_available_balance(merchant_id) -> int:
    
    return get_merchant_balance(merchant_id) - get_merchant_held_balance(merchant_id)
