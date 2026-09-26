"""The double-entry ledger. This is the only way money moves in Jo Bingo.

There is no `UPDATE users SET balance = balance + x` anywhere in this
codebase -- every movement is one or more signed ledger_entries rows summing
to exactly zero, written inside a single database transaction alongside the
account_balances cache update. Postgres itself rejects (via a deferred
constraint trigger, see migrations/versions/81d041ff4513_ledger_foundation.py)
any transaction whose entries do not sum to zero, so this invariant survives
even a bug in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from decimal import Decimal

import asyncpg
import asyncpg.pool
from redis.asyncio import Redis

from packages.core import metrics

# Every function here is called both with a bare connection (tests, one-off
# scripts) and with a connection checked out of a pool via `async with
# pool.acquire() as conn`, which asyncpg wraps in a proxy that isn't a
# subclass of Connection. Accept both rather than forcing every caller to
# know which one they happen to have.
AsyncpgConnection = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy

USER_BALANCE_KINDS = frozenset({"user_cash", "user_bonus", "user_locked"})


class IdempotencyKeyConflict(Exception):
    """post() was given a key that already belongs to a *different*
    operation (another kind, round or payment). Idempotency keys are one
    namespace across every kind, so treating that as a replay would report
    money as moved when it never was -- the silent failure a platform audit
    (2026-09-25) found reachable through a player-chosen Keno key."""

    def __init__(self, idempotency_key: str, *, existing_kind: str, requested_kind: str) -> None:
        self.idempotency_key = idempotency_key
        self.existing_kind = existing_kind
        self.requested_kind = requested_kind
        super().__init__(
            f"idempotency key {idempotency_key!r} already belongs to a {existing_kind!r} transaction; "
            f"refusing to treat this {requested_kind!r} post as its replay"
        )


class InsufficientFunds(Exception):
    def __init__(self, account_id: int, attempted_balance: Decimal) -> None:
        self.account_id = account_id
        self.attempted_balance = attempted_balance
        super().__init__(
            f"account {account_id} would go to {attempted_balance}, which is "
            "not allowed for a user balance account"
        )


@dataclass(frozen=True)
class Account:
    id: int
    user_id: int | None
    kind: str
    currency: str


@dataclass(frozen=True)
class Entry:
    account_id: int
    amount: Decimal


@dataclass
class _LockedBalance:
    kind: str
    balance: Decimal


@dataclass(frozen=True)
class LedgerTransaction:
    id: int
    kind: str
    idempotency_key: str
    round_id: int | None
    payment_id: int | None
    created_by: str
    memo: str | None


async def get_or_create_account(
    conn: AsyncpgConnection,
    user_id: int | None,
    kind: str,
    currency: str = "ETB",
) -> Account:
    if user_id is None:
        row = await conn.fetchrow(
            """
            INSERT INTO accounts (user_id, kind, currency)
            VALUES (NULL, $1, $2)
            ON CONFLICT (kind, currency) WHERE user_id IS NULL DO NOTHING
            RETURNING id, user_id, kind, currency
            """,
            kind,
            currency,
        )
        if row is None:
            row = await conn.fetchrow(
                "SELECT id, user_id, kind, currency FROM accounts "
                "WHERE user_id IS NULL AND kind = $1 AND currency = $2",
                kind,
                currency,
            )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO accounts (user_id, kind, currency)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, kind, currency) DO NOTHING
            RETURNING id, user_id, kind, currency
            """,
            user_id,
            kind,
            currency,
        )
        if row is None:
            row = await conn.fetchrow(
                "SELECT id, user_id, kind, currency FROM accounts "
                "WHERE user_id = $1 AND kind = $2 AND currency = $3",
                user_id,
                kind,
                currency,
            )
    assert row is not None
    account = Account(row["id"], row["user_id"], row["kind"], row["currency"])

    # account_balances row is created lazily, once, the first time this
    # account is touched -- not here, so an account with no history never
    # shows up in balance queries with a phantom zero row for no reason.
    # (post() creates it via the same ON CONFLICT DO NOTHING pattern.)
    return account


async def post(
    conn: AsyncpgConnection,
    kind: str,
    entries: list[Entry],
    idempotency_key: str,
    *,
    round_id: int | None = None,
    payment_id: int | None = None,
    memo: str | None = None,
    created_by: str = "system",
    created_at: datetime | None = None,
) -> LedgerTransaction:
    """`created_at`: nearly every caller should leave this None (the
    column's own `DEFAULT now()` applies) -- the one legitimate
    exception is a test proving a query buckets by calendar day/timezone
    boundary correctly, which needs an entry that genuinely originates at
    a specific historical instant, not one back-dated after the fact.
    This is an INSERT-time parameter, never a way to retroactively change
    an existing entry: ledger_entries/ledger_transactions are append-only
    at the database level (migrations/versions/
    b8e4a1f0c3d7_ledger_append_only.py) specifically because a prior
    incident showed the alternative (insert now, UPDATE created_at
    after) is exactly the kind of history-mutation that invariant exists
    to prevent."""
    if not entries:
        raise ValueError("post() requires at least one entry")

    # A second code review pass caught that the fix below (incrementing
    # only after the `async with` block exits, not inside it) is still
    # not safe in general: every real production caller already has its
    # own transaction open by the time it calls post(), which makes this
    # function's own `async with conn.transaction()` a SAVEPOINT, not a
    # real commit. If a later statement in the *caller's* own transaction
    # then fails, the whole thing rolls back -- but by then this metric
    # would already have fired for a ledger write that never actually
    # persisted. Checked here, before opening this function's own block:
    # if the caller already has a transaction open, this call can't know
    # whether it will ultimately commit, so it leaves the metric to the
    # caller to record once *its* own transaction genuinely commits --
    # the same convention this module's own publish_balance_update()
    # already documents, for the same reason. Only when post() itself is
    # the real top-level transaction owner (a direct, non-nested call --
    # this module's own tests, notably) is it safe to record here.
    already_in_transaction = conn.is_in_transaction()

    async with conn.transaction():
        txn_row = await conn.fetchrow(
            """
            INSERT INTO ledger_transactions
                (kind, idempotency_key, round_id, payment_id, created_by, memo)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, kind, idempotency_key, round_id, payment_id, created_by, memo
            """,
            kind,
            idempotency_key,
            round_id,
            payment_id,
            created_by,
            memo,
        )

        if txn_row is None:
            # Someone else already owns this idempotency_key. If they're
            # still mid-transaction, the unique index blocks this INSERT
            # above until they commit or roll back, so by the time we reach
            # this SELECT their row (and its entries) are already visible.
            existing = await conn.fetchrow(
                """
                SELECT id, kind, idempotency_key, round_id, payment_id, created_by, memo
                FROM ledger_transactions WHERE idempotency_key = $1
                """,
                idempotency_key,
            )
            assert existing is not None
            # A genuine replay is the same operation: same kind, and the same
            # round/payment when the caller names one. Anything else is a key
            # collision between two operations, and returning the existing row
            # would silently drop this caller's money movement.
            if (
                existing["kind"] != kind
                or (round_id is not None and existing["round_id"] != round_id)
                or (payment_id is not None and existing["payment_id"] != payment_id)
            ):
                raise IdempotencyKeyConflict(idempotency_key, existing_kind=existing["kind"], requested_kind=kind)
            return LedgerTransaction(**dict(existing))

        transaction_id = txn_row["id"]

        # Lock every touched account's balance row, in a fixed order, so two
        # transactions that both touch accounts A and B can never deadlock
        # by locking them in opposite order.
        account_ids = sorted({e.account_id for e in entries})
        locked: dict[int, _LockedBalance] = {}
        for account_id in account_ids:
            row = await conn.fetchrow(
                "SELECT account_id, kind, balance FROM account_balances "
                "WHERE account_id = $1 FOR UPDATE",
                account_id,
            )
            if row is None:
                # Brand-new account, no balance row yet. Create it at zero;
                # if a concurrent post() for a different transaction is
                # racing us to create the same row, ON CONFLICT DO NOTHING
                # lets exactly one INSERT win and the re-SELECT below then
                # blocks on that winner's row lock like any other contender.
                acct = await conn.fetchrow(
                    "SELECT kind FROM accounts WHERE id = $1", account_id
                )
                assert acct is not None
                await conn.execute(
                    """
                    INSERT INTO account_balances (account_id, kind, balance)
                    VALUES ($1, $2, 0)
                    ON CONFLICT (account_id) DO NOTHING
                    """,
                    account_id,
                    acct["kind"],
                )
                row = await conn.fetchrow(
                    "SELECT account_id, kind, balance FROM account_balances "
                    "WHERE account_id = $1 FOR UPDATE",
                    account_id,
                )
                assert row is not None
            locked[account_id] = _LockedBalance(kind=row["kind"], balance=row["balance"])

        deltas: dict[int, Decimal] = {}
        for entry in entries:
            deltas[entry.account_id] = deltas.get(
                entry.account_id, Decimal("0")
            ) + entry.amount

        for account_id, delta in deltas.items():
            current = locked[account_id]
            new_balance = current.balance + delta
            if current.kind in USER_BALANCE_KINDS and new_balance < 0:
                raise InsufficientFunds(account_id, new_balance)

        for entry in entries:
            await conn.execute(
                """
                INSERT INTO ledger_entries (transaction_id, account_id, amount, created_at)
                VALUES ($1, $2, $3, COALESCE($4::timestamptz, now()))
                """,
                transaction_id,
                entry.account_id,
                entry.amount,
                created_at,
            )

        last_entry_id = await conn.fetchval(
            "SELECT max(id) FROM ledger_entries WHERE transaction_id = $1",
            transaction_id,
        )
        for account_id, delta in deltas.items():
            await conn.execute(
                """
                UPDATE account_balances
                SET balance = balance + $2, last_entry_id = $3
                WHERE account_id = $1
                """,
                account_id,
                delta,
                last_entry_id,
            )

        result = LedgerTransaction(
            id=transaction_id,
            kind=txn_row["kind"],
            idempotency_key=txn_row["idempotency_key"],
            round_id=txn_row["round_id"],
            payment_id=txn_row["payment_id"],
            created_by=txn_row["created_by"],
            memo=txn_row["memo"],
        )

    if not already_in_transaction:
        metrics.ledger_transactions_total.labels(kind=result.kind).inc()
    return result


_CENT = Decimal("0.01")


async def balance(conn: AsyncpgConnection, account_id: int) -> Decimal:
    value = await conn.fetchval(
        "SELECT balance FROM account_balances WHERE account_id = $1", account_id
    )
    result = value if value is not None else Decimal("0")
    return result.quantize(_CENT)


async def available(conn: AsyncpgConnection, user_id: int) -> Decimal:
    row = await conn.fetchrow(
        """
        SELECT
            COALESCE(SUM(b.balance) FILTER (WHERE a.kind IN ('user_cash', 'user_bonus')), 0)
                - COALESCE(SUM(b.balance) FILTER (WHERE a.kind = 'user_locked'), 0)
                AS available
        FROM accounts a
        JOIN account_balances b ON b.account_id = a.id
        WHERE a.user_id = $1
        """,
        user_id,
    )
    result = row["available"] if row and row["available"] is not None else Decimal("0")
    return result.quantize(_CENT)


async def reconcile(conn: AsyncpgConnection) -> list[tuple[int, Decimal, Decimal]]:
    """Recomputes every account's balance from ledger_entries and compares it
    to the account_balances cache. Returns a list of (account_id, cached,
    computed) for every mismatch found -- empty means the ledger is
    reconciled. Intended to run as a nightly job; exposed here as a plain
    function so it's directly unit-testable.
    """
    rows = await conn.fetch(
        """
        SELECT
            b.account_id,
            b.balance AS cached,
            COALESCE(SUM(e.amount), 0) AS computed
        FROM account_balances b
        LEFT JOIN ledger_entries e ON e.account_id = b.account_id
        GROUP BY b.account_id, b.balance
        HAVING b.balance <> COALESCE(SUM(e.amount), 0)
        """
    )
    return [(r["account_id"], r["cached"], r["computed"]) for r in rows]


async def user_balance_snapshot(pool: asyncpg.Pool, user_id: int) -> dict[str, str]:
    """The three balance figures the Mini App and the bot both show a
    player: cash, bonus, locked. Lives here (not services/gateway/queries
    .py, where it started) because it's a pure ledger read with nothing
    gateway-specific about it, and publish_balance_update() below --
    used by every service that ever moves a player's money, not just the
    gateway -- needs it too; packages/core never depends on services/*,
    so the dependency can only run this direction.

    One query, not three get_or_create_account() + three balance() calls
    (up to nine round trips) -- publish_balance_update() puts this on
    round_engine.py's join()/drop_card() hot path, called on every stake,
    so this stayed a straight read (never lazily creating an accounts row
    the way get_or_create_account() does) rather than reusing that
    already-existing but much chattier helper. A kind with no accounts
    row yet genuinely has zero of it, the same value get_or_create_account
    + balance()'s combination would themselves have produced for it.
    """
    rows = await pool.fetch(
        """
        SELECT a.kind, COALESCE(b.balance, 0) AS balance
        FROM accounts a
        LEFT JOIN account_balances b ON b.account_id = a.id
        WHERE a.user_id = $1 AND a.kind IN ('user_cash', 'user_bonus', 'user_locked')
        """,
        user_id,
    )
    balances = {row["kind"]: str(row["balance"].quantize(_CENT)) for row in rows}
    return {
        "cash": balances.get("user_cash", "0.00"),
        "bonus": balances.get("user_bonus", "0.00"),
        "locked": balances.get("user_locked", "0.00"),
    }


async def publish_balance_update(pool: asyncpg.Pool, redis: Redis, user_id: int) -> dict[str, str]:
    """Pushes this user's current balance over their Mini App WebSocket's
    per-user fanout channel (services/gateway/connection.py subscribes
    every connection to `user:{user_id}` at handshake).

    A code review pass caught that only services/payments/deposits.py
    ever called this: staking, dropping a card, winning or losing a
    round, requesting a withdrawal, and a payout settling or reversing
    all move real money too, but none of them told a connected player's
    UI its balance had actually changed -- the on-screen number stayed
    stale until the player happened to reopen the wallet screen (which
    does its own fresh /api/me fetch) or reconnect. Every one of those
    call sites already has both `pool` and `redis` in hand, so this is
    meant to be called right after the money-moving transaction commits,
    everywhere that happens.
    """
    snapshot = await user_balance_snapshot(pool, user_id)
    await redis.publish(f"user:{user_id}", json.dumps({"t": "balance_update", **snapshot}))
    return snapshot
