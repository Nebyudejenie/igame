"""Lock order on a player's users row, outside the Keno ticket path that
test_responsible_gaming_paths.py already covers (2026-09-25).

The convention these tests hold every player-scoped transaction to: lock
the users row FOR NO KEY UPDATE, never FOR UPDATE. FOR UPDATE conflicts
with the FOR KEY SHARE that every foreign-key insert referencing the
player takes (round_entries, round_winners, keno_tickets, payments,
accounts, ...), which is what turns an ordinary wait into a deadlock:

- request_withdrawal() held users FOR UPDATE and then waited for the
  player's user_cash balance row inside ledger.post(). Bingo settlement
  locks that balance row first (the payout post) and then inserts
  round_winners (FOR KEY SHARE on users). Reproduced below, before the fix.
- The two admin actions (set_user_status, set_kyc_level) lock nothing
  after the users row that a player transaction could be holding, so they
  can't close a cycle and no deadlock reproduces. What FOR UPDATE did cost
  there: every foreign-key insert for that player (a Bingo join, a Keno
  ticket's row) waited behind the admin's transaction.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import asyncpg
import pytest

from packages.core import ledger
from services.admin import audit, queries
from services.payments import withdrawals
from tests.integration.conftest import create_funded_user
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_admin_withdrawals import _NullProvider

pytestmark = pytest.mark.asyncio


async def _wait_until_blocked_by(conn: asyncpg.Connection, blocker_pid: int) -> None:
    for _ in range(200):
        if await conn.fetchval("SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))", blocker_pid):
            return
        await asyncio.sleep(0.025)
    raise AssertionError("the transaction under test never blocked on the held lock")


async def test_a_withdrawal_request_racing_a_bingo_payout_to_the_same_player_does_not_deadlock(
    pool: asyncpg.Pool, redis, conn: asyncpg.Connection
) -> None:
    """Settlement's two locks, in its order, on a held transaction: the
    winner's user_cash balance row (the payout post), then FOR KEY SHARE
    on the winner's users row (the round_winners insert). A withdrawal
    request that already holds the users row FOR UPDATE while it waits
    for that balance row closes the cycle."""
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")

    async with pool.acquire() as settle_conn:
        settle_pid = await settle_conn.fetchval("SELECT pg_backend_pid()")
        tx = settle_conn.transaction()
        await tx.start()
        try:
            held = await settle_conn.fetchval("SELECT 1 FROM account_balances WHERE account_id = $1 FOR UPDATE", cash.id)
            assert held == 1
            withdrawal = asyncio.create_task(
                withdrawals.request_withdrawal(
                    pool, redis, _NullProvider(),
                    user_id=user_id, amount=Decimal("100.00"), method_kind="telebirr", account_ref="0911223344",
                    holder_name="Test Holder", min_withdraw=Decimal("10.00"), auto_approve_limit=Decimal("0.00"),
                    kyc_threshold=Decimal("1000000.00"), chargeback_window_minutes=0, min_account_age_hours=0,
                )
            )
            await _wait_until_blocked_by(conn, settle_pid)
            await settle_conn.execute("SELECT 1 FROM users WHERE id = $1 FOR KEY SHARE", user_id)
        finally:
            await tx.rollback()
        intent = await asyncio.wait_for(withdrawal, timeout=20)

    assert intent.status == withdrawals.STATUS_REVIEW
    assert await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id) == "review"


@pytest.mark.parametrize("action", ["set_user_status", "set_kyc_level"])
async def test_an_admin_change_to_a_player_does_not_block_that_players_foreign_key_inserts(
    pool: asyncpg.Pool, conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """The admin transaction is held open at a real await point (its own
    audit.record() call, after the users row is locked), and a player-side
    FOR KEY SHARE -- what a Bingo join's round_entries insert takes -- must
    still be granted, not queue behind the admin."""
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("0"))
    real_record = audit.record
    locked = asyncio.Event()
    release = asyncio.Event()

    async def record_then_hold(*args, **kwargs):
        locked.set()
        await release.wait()
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(audit, "record", record_then_hold)
    if action == "set_user_status":
        admin_call = queries.set_user_status(
            pool, admin_id=admin_id, user_id=user_id, status="banned", reason="test: lock order check", ip_address=None
        )
    else:
        admin_call = queries.set_kyc_level(
            pool, admin_id=admin_id, user_id=user_id, kyc_level=2, reason="test: lock order check", ip_address=None
        )
    admin_task = asyncio.create_task(admin_call)
    try:
        await asyncio.wait_for(locked.wait(), timeout=10)
        async with pool.acquire() as player_conn:
            async with player_conn.transaction():
                await player_conn.execute("SET LOCAL lock_timeout = '2s'")
                granted = await player_conn.fetchval("SELECT 1 FROM users WHERE id = $1 FOR KEY SHARE", user_id)
        assert granted == 1
    finally:
        release.set()
        await asyncio.wait_for(admin_task, timeout=10)
