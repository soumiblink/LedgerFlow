# LedgerFlow – EXPLAINER.md

## 1. The Ledger

**Balance calculation query:**

```python
result = LedgerEntry.objects.filter(merchant_id=merchant_id).aggregate(
    total_credits=Sum("amount_paise", filter=Q(type="CREDIT"), default=0),
    total_debits=Sum("amount_paise",  filter=Q(type="DEBIT"),  default=0),
)
balance = result["total_credits"] - result["total_debits"]
```

This is a single SQL aggregation — no Python loops, no fetching rows.

**Why credits and debits are modeled this way:**

Every financial event writes an immutable `LedgerEntry` row. Credits come from incoming payments. Debits come from payout holds. Refunds on failed payouts write a new CREDIT — they never modify or delete the original DEBIT.

The table is append-only by design: `save()` raises `ValueError` if `pk` already exists, and `delete()` is blocked at the model level. This means the ledger is also the audit trail — any balance figure can be independently verified by replaying the entries. There is no stored balance column that can drift.

`amount_paise` is `BigIntegerField`. The `type` field (CREDIT/DEBIT) carries the direction. Amounts are always positive integers.



---

## 2. The Lock

**Exact code that prevents concurrent overdraw:**

```python
with transaction.atomic():
    # Acquires a PostgreSQL row-level exclusive lock on this merchant row.
    # Any other transaction attempting select_for_update() on the same row
    # will block here until this transaction commits or rolls back.
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)

    # Balance check happens INSIDE the lock — reads the committed state
    # after any concurrent transaction has already written its DEBIT.
    available = get_available_balance(merchant.id)
    if available < amount_paise:
        raise InsufficientBalanceError(...)

    payout = Payout.objects.create(...)
    LedgerEntry.objects.create(type=DEBIT, ...)
```

**The database primitive:** PostgreSQL `SELECT ... FOR UPDATE`. This acquires an exclusive row-level lock on the merchant row for the duration of the transaction. The second concurrent request blocks at `select_for_update()` until the first transaction commits. By then, the first payout's DEBIT entry is visible, the balance check correctly sees reduced funds, and the second request is rejected.

**Why Python-level locking is insufficient:** A `threading.Lock()` only works within a single process. Under gunicorn with multiple workers, or across multiple server instances, there is no shared memory. The lock must live in the database.

---

## 3. The Idempotency

**How the system knows it has seen a key before:**

The `idempotency_key` is stored on the `Payout` model with a `UniqueConstraint` on `(merchant, idempotency_key)`. Before entering the transaction, we do a fast pre-check:

```python
existing = Payout.objects.filter(
    merchant_id=merchant_id,
    idempotency_key=idempotency_key,
).first()
if existing:
    return _build_payout_response(existing)
```

If found, we return the original response immediately — no DB write, no lock acquired.

**What happens if the first request is in flight when the second arrives:**

Both requests pass the pre-check (neither exists yet). Both enter `transaction.atomic()`. One inserts successfully. The other hits the unique constraint and raises `IntegrityError`. We catch it and return the existing payout:

```python
except IntegrityError:
    payout = Payout.objects.get(
        merchant_id=merchant_id,
        idempotency_key=idempotency_key,
    )
    return _build_payout_response(payout)
```

The DB constraint is the final safety net. No application-level coordination is needed. Keys are scoped per merchant — the same UUID from two different merchants creates two separate payouts.

TTL: The `IdempotencyKey` model has an `expires_at` field. A `purge_expired_idempotency_keys` Celery task handles cleanup. The schema supports 24h TTL; the task is ready to wire into the beat schedule.

---

## 4. The State Machine

**Allowed transitions:**
```
PENDING → PROCESSING → COMPLETED
                     → FAILED
```
`COMPLETED` and `FAILED` are terminal. No transitions out.

**Where `FAILED → COMPLETED` is blocked:**

```python
# apps/payouts/models.py

VALID_TRANSITIONS = {
    Status.PENDING:    {Status.PROCESSING},
    Status.PROCESSING: {Status.COMPLETED, Status.FAILED},
    Status.COMPLETED:  set(),   # terminal — empty set means no transitions allowed
    Status.FAILED:     set(),   # terminal — empty set means no transitions allowed
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

`VALID_TRANSITIONS[FAILED]` is an empty set. `new_status not in set()` is always `True`. `ValueError` is raised before any DB write. Every status change in the codebase goes through `transition_to()` — there are no direct `payout.status = ...` assignments.

---

## 5. The AI Audit

**The bug:** The initial AI-generated version of `handle_stuck_payouts` evaluated `select_for_update()` outside the transaction:

```python
# WRONG — AI generated this
stuck = Payout.objects.filter(
    status=Payout.Status.PROCESSING,
    updated_at__lt=cutoff,
).select_for_update(skip_locked=True)

with transaction.atomic():
    for payout in stuck:
        ...
```

**Why it is wrong:** PostgreSQL requires `SELECT ... FOR UPDATE` to run inside an open transaction. Django querysets are lazy — the SQL fires when the queryset is first iterated, which happens inside the `for` loop. But the queryset object itself is created *before* `transaction.atomic()` opens the transaction. On some Django/psycopg2 versions this causes the lock to be acquired outside the transaction context, meaning PostgreSQL releases it immediately. More critically, two beat workers running simultaneously both build the queryset before either enters the `atomic()` block. Both see the same stuck payouts. Both process them. Both issue refunds. The unique constraint on `LedgerEntry` doesn't protect against this because `PAYOUT_REFUND` entries have no such constraint.

**The fix:**

```python
# CORRECT — queryset defined and evaluated inside the transaction
with transaction.atomic():
    stuck = Payout.objects.filter(
        status=Payout.Status.PROCESSING,
        updated_at__lt=cutoff,
    ).select_for_update(skip_locked=True)

    for payout in stuck:
        ...
```

The queryset is now defined inside `transaction.atomic()`, so the `SELECT ... FOR UPDATE` SQL fires within the open transaction. `skip_locked=True` means a second worker skips rows already locked by the first — no blocking, no deadlocks, no duplicate processing.
