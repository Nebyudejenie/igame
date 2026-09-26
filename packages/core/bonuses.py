"""Bonus grant/convert/expire primitives on top of packages/core/ledger.py.

Every dollar amount here comes from a caller-supplied rule or an admin's
own manual-grant input -- nothing in this module invents a business
number. Money only ever moves through ledger.post() using the
user_bonus/promo_expense account kinds and bonus_grant/bonus_convert
transaction kinds packages/core/ledger.py's own migration already
declared and no other code has posted until now.

Bonus money is deliberately "sticky": it is never staked directly (see
docs -- a round pools every player's stake into one shared pot before
paying winners, so there is no way to trace a payout back to "this came
from a bonus-funded stake" without approximating through round_engine.py,
the single highest-stakes file in this codebase). Instead, a bonus grant
sits in user_bonus, non-withdrawable and unstaked, until the recipient's
own real cash wagering since the grant reaches wagering_required (tracked
via wagering_progress_for_user_since(), a read against existing stake
ledger history -- nothing round_engine.py writes ever changes for this
feature). Once that happens, convert_bonus_to_cash() moves it into
user_cash, where it behaves exactly like any other deposit from then on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from packages.core.ledger import AsyncpgConnection, Entry, get_or_create_account, post


@dataclass(frozen=True)
class Bonus:
    id: int
    user_id: int
    rule_id: int | None
    referral_of_user_id: int | None
    amount: Decimal
    wagering_required: Decimal
    status: str
    grant_txn_id: int


class BonusNotFound(ValueError):
    pass


async def grant_bonus(
    conn: AsyncpgConnection,
    *,
    user_id: int,
    idempotency_key: str,
    amount: Decimal,
    wagering_required: Decimal,
    rule_id: int | None = None,
    referral_of_user_id: int | None = None,
    expires_at: datetime | None = None,
    granted_by_admin_id: int | None = None,
    reason: str | None = None,
) -> Bonus:
    """Credits user_bonus, debits promo_expense (the system account this
    codebase's own ledger migration set aside for funding grants), and
    records a `bonuses` row. Idempotent on `idempotency_key` the same way
    every other ledger.post() caller in this codebase already is -- a
    retried call with the same key returns the same, already-created
    grant rather than crediting twice.
    """
    async with conn.transaction():
        existing_txn_id = await conn.fetchval(
            "SELECT id FROM ledger_transactions WHERE idempotency_key = $1", idempotency_key
        )
        if existing_txn_id is not None:
            existing = await conn.fetchrow(
                "SELECT id, user_id, rule_id, referral_of_user_id, amount, wagering_required, "
                "status, grant_txn_id FROM bonuses WHERE grant_txn_id = $1",
                existing_txn_id,
            )
            assert existing is not None  # a bonus_grant txn always has exactly one owning bonuses row
            return Bonus(**dict(existing))

        promo = await get_or_create_account(conn, None, "promo_expense")
        bonus_account = await get_or_create_account(conn, user_id, "user_bonus")
        txn = await post(
            conn,
            "bonus_grant",
            [Entry(promo.id, -amount), Entry(bonus_account.id, amount)],
            idempotency_key=idempotency_key,
            memo=reason,
        )
        row = await conn.fetchrow(
            """
            INSERT INTO bonuses
                (user_id, rule_id, referral_of_user_id, amount, wagering_required,
                 grant_txn_id, expires_at, granted_by_admin_id, reason)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id, user_id, rule_id, referral_of_user_id, amount, wagering_required,
                      status, grant_txn_id
            """,
            user_id,
            rule_id,
            referral_of_user_id,
            amount,
            wagering_required,
            txn.id,
            expires_at,
            granted_by_admin_id,
            reason,
        )
        assert row is not None
        return Bonus(**dict(row))


async def convert_bonus_to_cash(conn: AsyncpgConnection, *, bonus_id: int) -> bool:
    """Moves a bonus's full remaining amount from user_bonus to user_cash.
    Returns False (a no-op) if the bonus is not currently 'active' --
    matching the row-lock-then-no-op-if-already-moved shape
    services/admin/queries.py's approve_manual_deposit_admin/
    approve_withdrawal_admin already use for the exact same reason: a
    retried call (a crashed sweep tick picking the same row up again, an
    admin double-click) must never double-convert.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT user_id, amount, status FROM bonuses WHERE id = $1 FOR UPDATE", bonus_id
        )
        if row is None:
            raise BonusNotFound(f"no bonus with id {bonus_id}")
        if row["status"] != "active":
            return False

        bonus_account = await get_or_create_account(conn, row["user_id"], "user_bonus")
        cash_account = await get_or_create_account(conn, row["user_id"], "user_cash")
        txn = await post(
            conn,
            "bonus_convert",
            [Entry(bonus_account.id, -row["amount"]), Entry(cash_account.id, row["amount"])],
            idempotency_key=f"bonus-convert-{bonus_id}",
        )
        await conn.execute(
            "UPDATE bonuses SET status = 'converted', convert_txn_id = $2, converted_at = now(), "
            "updated_at = now() WHERE id = $1",
            bonus_id,
            txn.id,
        )
        return True


async def expire_bonus(conn: AsyncpgConnection, *, bonus_id: int) -> bool:
    """Reverses an unwagered, never-converted bonus back to promo_expense
    (rather than just flipping a status flag and leaving the ledger
    silently out of sync with what the player's own balance now shows)
    once it's past its own expires_at. Same no-op-if-not-active shape as
    convert_bonus_to_cash().
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT user_id, amount, status FROM bonuses WHERE id = $1 FOR UPDATE", bonus_id
        )
        if row is None:
            raise BonusNotFound(f"no bonus with id {bonus_id}")
        if row["status"] != "active":
            return False

        promo = await get_or_create_account(conn, None, "promo_expense")
        bonus_account = await get_or_create_account(conn, row["user_id"], "user_bonus")
        await post(
            conn,
            "bonus_grant",
            [Entry(bonus_account.id, -row["amount"]), Entry(promo.id, row["amount"])],
            idempotency_key=f"bonus-expire-{bonus_id}",
        )
        await conn.execute(
            "UPDATE bonuses SET status = 'expired', updated_at = now() WHERE id = $1", bonus_id
        )
        return True


async def revoke_bonus(conn: AsyncpgConnection, *, bonus_id: int) -> bool:
    """Same ledger reversal as expire_bonus(), distinguished only by the
    resulting status -- an admin-initiated takeback (e.g. a fraud finding
    after the fact) rather than the automatic passage of expires_at.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT user_id, amount, status FROM bonuses WHERE id = $1 FOR UPDATE", bonus_id
        )
        if row is None:
            raise BonusNotFound(f"no bonus with id {bonus_id}")
        if row["status"] != "active":
            return False

        promo = await get_or_create_account(conn, None, "promo_expense")
        bonus_account = await get_or_create_account(conn, row["user_id"], "user_bonus")
        await post(
            conn,
            "bonus_grant",
            [Entry(bonus_account.id, -row["amount"]), Entry(promo.id, row["amount"])],
            idempotency_key=f"bonus-revoke-{bonus_id}",
        )
        await conn.execute(
            "UPDATE bonuses SET status = 'revoked', updated_at = now() WHERE id = $1", bonus_id
        )
        return True


async def wagering_progress_for_user_since(
    conn: AsyncpgConnection, *, user_id: int, since: datetime
) -> Decimal:
    """Real, at-risk cash wagered by this user since `since`: Bingo stakes
    debited from their user_cash, minus refunds of those same stakes. Read-
    only: nothing this feature ever does writes to this query's own inputs.

    Refunds are netted out (platform audit, 2026-09-25): a card taken and
    dropped, or a lobby refunded for being underfilled, puts nothing at risk,
    and counting it let a player clear a bonus -- which then converts to
    withdrawable cash -- without ever risking a birr. A refund is matched to
    its own stake through the static idempotency keys round_engine.py and
    refunds.py already use (drop-/refund-{round}-{user}-{card} returns
    stake-{round}-{user}-{card}), and only offsets a stake placed since
    `since`, the same rule responsible_gaming.today_net_loss() applies to
    its own window. Withdrawal refunds share the 'refund' kind but carry no
    round_id, so they never match.
    """
    value = await conn.fetchval(
        """
        WITH mine AS (
            SELECT le.account_id, le.amount, lt.kind, lt.round_id, lt.idempotency_key
            FROM ledger_entries le
            JOIN ledger_transactions lt ON lt.id = le.transaction_id
            JOIN accounts a ON a.id = le.account_id
            WHERE a.user_id = $1 AND a.kind = 'user_cash' AND lt.kind IN ('stake', 'refund')
              AND le.created_at >= $2
        )
        SELECT
            COALESCE(SUM(-amount) FILTER (WHERE kind = 'stake'), 0)
            - COALESCE(SUM(amount) FILTER (
                WHERE kind = 'refund' AND round_id IS NOT NULL AND EXISTS (
                    SELECT 1
                    FROM ledger_transactions st
                    JOIN ledger_entries se ON se.transaction_id = st.id AND se.account_id = mine.account_id
                    WHERE st.idempotency_key = regexp_replace(mine.idempotency_key, '^(drop|refund)-', 'stake-')
                      AND st.kind = 'stake' AND se.created_at >= $2
                )
            ), 0)
        FROM mine
        """,
        user_id,
        since,
    )
    result: Decimal = value
    return result.quantize(Decimal("0.01"))
