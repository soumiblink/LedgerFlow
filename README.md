# LedgerFlow

Minimal payout engine for a fintech system. Merchants accumulate balance from incoming payments and request payouts to their bank accounts. The system handles concurrency, idempotency, and state machine correctness.

## Stack

- **Backend:** Django 4.2 + DRF + PostgreSQL + Celery + Redis
- **Frontend:** React 19 + Vite + Tailwind CSS 4 + Axios
- **Testing:** Django TestCase with real DB transactions

## Setup

### Backend

```bash
cd LedgerFlow-backend

# Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows
source venv/bin/activate  # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Configure environment
copy .env.example .env
# Edit .env with your PostgreSQL credentials

# Run migrations
python manage.py migrate

# Seed test data (3 merchants with realistic balances)
python manage.py seed_data

# Start Django server
python manage.py runserver
```

### Celery Workers (separate terminals)

```bash
# Terminal 1 — Worker
python -m celery -A ledgerflow worker --loglevel=info --pool=solo

# Terminal 2 — Beat (periodic tasks)
python -m celery -A ledgerflow beat --loglevel=info
```

### Frontend

```bash
cd ledgerflow-frontend

# Install dependencies
npm install

# Configure environment
copy .env.example .env
# Set VITE_MERCHANT_ID to a UUID from seed_data output

# Start dev server
npm run dev
```

Open `http://localhost:5173`

## API Endpoints

- `GET /api/v1/merchants/{id}/balance/` — Total, held, available balance
- `POST /api/v1/payouts/` — Create payout (requires `Idempotency-Key` header)
- `GET /api/v1/payouts/?merchant_id=...` — List payouts
- `GET /api/v1/ledger/?merchant_id=...` — Ledger entries (credits + debits)

## Tests

```bash
cd LedgerFlow-backend
python manage.py test tests --settings=ledgerflow.settings.test
```

26 tests covering:
- Concurrency (balance never goes negative, only one overspend succeeds)
- Idempotency (same key returns same response, no duplicates)
- State machine (invalid transitions rejected)
- Refunds (atomic with status change, never double-issued)

## Key Design Decisions

**No stored balance.** Balance is computed on-demand from `LedgerEntry` rows using a single aggregation query. The ledger is the source of truth.

**Row-level locking.** `select_for_update()` on the merchant row inside `transaction.atomic()` prevents two concurrent payout requests from both passing the balance check.

**Idempotency via DB constraint.** `UniqueConstraint` on `(merchant, idempotency_key)` enforced at the database level. Concurrent duplicates raise `IntegrityError`, which is caught and resolved by returning the existing payout.

**Strict state machine.** `Payout.transition_to()` validates every status change before writing. `COMPLETED` and `FAILED` are terminal — no transitions out.

**Atomic refunds.** On failure, the status update and the refund `CREDIT` entry are written in the same transaction. Double-refund prevention via existence check before insert.

See `EXPLAINER.md` for detailed technical explanations.
