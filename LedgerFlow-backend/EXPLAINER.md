# LedgerFlow – Explainer

## 1. The Ledger

Balance is never stored as a column. Storing it creates a second source of truth that can drift from the ledger — through bugs, failed transactions, or direct DB edits. Instead, balance is always computed on demand from `LedgerEntry` rows.

Every financial event (incoming payment, payout hold, refund) writes an immutable `LedgerEntry`. The table is append-only: `save()` raises `ValueError` if `pk` already exists, and `delete()` is blocked at the model level.

Balance is computed in a single aggregation query:

```python
result = LedgerEntry.objects.filter(merchant_id=merchant_id).aggregate(
    total_credits=Sum("amount_paise", filter=Q(type="CREDIT"), default=0),
    total_debits=Sum("amount_paise",  filter=Q(type="DEBIT"),  default=0),
)
return result["total_credits"] - result["total_debits"]
```

This means the ledger is the audit trail. Any balance figure can be independently verified by replaying the entries. There is no reconciliation problem because there is nothing to reconcile against.

---

## 2. The Lock

```python
with transaction.atomic():
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)
    available = get_available_balance(merchant.id)
    if available < amount_paise:
        raise InsufficientBalanceError(...)
    payout = Payout.objects.create(...)
    LedgerEntry.objects.create(type=DEBIT, ...)
```

**The problem:** Two concurrent payout requests for the same merchant both read the balance before either writes. Both see sufficient funds. Both succeed. The merchant is overdrafted.

**The fix:** `select_for_update()` acquires a PostgreSQL row-level exclusive lock on the merchant row. The second transaction blocks at that line until the first commits. By the time it proceeds, the balance has already been reduced by the first payout's DEBIT entry, and the check correctly rejects it.

**Why Python-level locking fails:** A `threading.Lock()` only works within a single process. Under gunicorn with multiple workers, or across multiple server instances, there is no shared memory. The lock must live in the database.

---

## 3. The Idempotency

Every payout request requires an `Idempotency-Key` header. The key is stored alongside the payout with a `UniqueConstraint` on `(merchant, idempotency_key)`.

**TTL:** The `IdempotencyKey` model has an `expires_at` field supporting 24-hour TTL. A `purge_expired_idempotency_keys` Celery task exists to clean up expired records. It is not yet wired into the beat schedule — the schema supports it and the task is ready to enable.

**Sequential duplicates:** Before entering the transaction, we check for an existing payout with the same key. If found, we return the original response immediately — no DB write, no lock acquired.

**Concurrent duplicates (race condition):** Two requests with the same key arrive simultaneously. Both pass the pre-check (neither exists yet). Both enter the transaction. One inserts successfully. The other hits the unique constraint and raises `IntegrityError`. We catch it and return the existing payout:

```python
except IntegrityError:
    payout = Payout.objects.get(
        merchant_id=merchant_id,
        idempotency_key=idempotency_key,
    )
    return _build_payout_response(payout)
```

The DB constraint is the final safety net. No application-level coordination is needed.

---

## 4. The State Machine

Valid transitions only:

```
PENDING → PROCESSING → COMPLETED
                     → FAILED
```

`COMPLETED` and `FAILED` are terminal. No transitions out.

Enforcement lives in `Payout.transition_to()`:

```python
VALID_TRANSITIONS = {
    Status.PENDING:    {Status.PROCESSING},
    Status.PROCESSING: {Status.COMPLETED, Status.FAILED},
    Status.COMPLETED:  set(),
    Status.FAILED:     set(),
}

def transition_to(self, new_status: str):
    allowed = self.VALID_TRANSITIONS.get(self.status, set())
    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition: {self.status} → {new_status}. "
            f"Allowed: {allowed or 'none (terminal state)'}"
        )
    self.status = new_status
    self.save(update_fields=["status", "updated_at"])
```

`FAILED → COMPLETED` raises `ValueError` because `VALID_TRANSITIONS[FAILED]` is an empty set. The status is never written to the DB.

---

## 5. The Retry and Refund Logic

Payouts are processed by a Celery task (`process_payout`) triggered via `on_commit()` — ensuring the task is only queued after the payout row is committed to the DB.

**Retry:** The Celery beat task `retry_stuck_payouts` runs every 30 seconds. It finds payouts in `PROCESSING` with `updated_at` older than 30 seconds and re-queues them. After `MAX_ATTEMPTS = 3`, the payout is forced to `FAILED`.

**Refund:** On failure, the status update and the refund CREDIT entry are written in the same `transaction.atomic()` block. The status transition goes through `transition_to()` which validates `PROCESSING → FAILED` before writing:

```python
with transaction.atomic():
    payout.transition_to(Payout.Status.FAILED)  # validates before writing
    _issue_refund(payout)                        # existence-checked inside same tx
```

If the transaction rolls back, neither the status change nor the refund entry is written. They are always consistent.

**Double-refund prevention:** `_issue_refund()` checks for an existing `PAYOUT_REFUND` entry before inserting:

```python
already_refunded = LedgerEntry.objects.filter(
    reference_type="PAYOUT_REFUND",
    reference_id=str(payout.id),
).exists()
if already_refunded:
    return
```

Since `LedgerEntry` is immutable, a refund entry, once written, can never be modified or deleted.

---

## 6. The AI Audit

**The bug:** The initial generated version of `handle_stuck_payouts` evaluated `select_for_update()` outside the transaction:

```python
# WRONG — lock acquired outside transaction
stuck = Payout.objects.filter(
    status=Payout.Status.PROCESSING,
    updated_at__lt=cutoff,
).select_for_update(skip_locked=True)

with transaction.atomic():
    for payout in stuck:
        ...
```

**Why it is wrong:** PostgreSQL requires row locks to be held within a transaction. A `select_for_update()` queryset is lazy — it is not evaluated until iterated. When iterated inside the `for` loop, Django has already exited the `atomic()` block's setup phase and the lock is acquired outside any transaction context. PostgreSQL immediately releases the lock. Two beat workers can read the same stuck payout simultaneously, process it twice, and issue two refunds.

**The fix:** Move `select_for_update()` inside `transaction.atomic()` so the lock is acquired and held for the duration of the block:

```python
# CORRECT — lock acquired and held inside transaction
with transaction.atomic():
    stuck = Payout.objects.filter(
        status=Payout.Status.PROCESSING,
        updated_at__lt=cutoff,
    ).select_for_update(skip_locked=True)

    for payout in stuck:
        ...
```

`skip_locked=True` means a second worker skips rows already locked by the first, rather than blocking. This prevents both deadlocks and duplicate processing.

---

## 7. Guarantees Provided by the System

- **No double spending:** Balance check and DEBIT entry are inside a single `transaction.atomic()` block protected by `select_for_update()`. No two transactions can pass the balance check for the same merchant simultaneously.

- **No duplicate payouts:** `UniqueConstraint` on `(merchant, idempotency_key)` enforced at the DB level. `IntegrityError` on concurrent duplicates is caught and resolved by returning the existing payout.

- **Ledger consistency:** `LedgerEntry` is append-only and immutable. Balance is always `SUM(CREDITS) - SUM(DEBITS)` computed directly from the table. No stored balance column can drift.

- **Idempotent API:** Same `Idempotency-Key` always returns the same response. Safe to retry on network failure without creating duplicate payouts or double-charging.

- **Safe concurrent execution:** Row-level locking via `select_for_update()` serializes concurrent payout creation per merchant. `skip_locked=True` on the retry worker prevents duplicate processing of stuck payouts across multiple Celery workers.
