"""Tests for services/admin/seed_simulated_players_cli.py -- run as a real
subprocess (same discipline as test_create_admin_cli.py), against the real
create_simulated_player() path, not a mocked one.

Every test here consumes the *entire* 10-bot roster cap (the script's
whole point is seeding all 10 named profiles at once), so each one cleans
up every bot it created in a finally block -- the same "shared, never-
reset dev database has a real hard cap" discipline test_simulated_players.py's
own bot_factory fixture already established.
"""

import asyncio
import os
import sys

from packages.core import ledger
from services.admin import simulated_players_queries as spq
from services.admin.seed_simulated_players_cli import SEED_NAMES
from tests.integration.test_admin_auth import create_test_admin


async def _delete_bot_completely(pool, user_id: int) -> None:
    """See test_simulated_players.py's own _delete_bot_completely
    docstring: only simulated_players needs deleting (it's what the
    10-bot roster cap actually counts) -- ledger_entries is append-only
    (migrations/versions/b8e4a1f0c3d7_ledger_append_only.py) and was
    never load-bearing here. DECISIONS.md, 2026-09-17."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM claim_attempts WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM round_winners WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM round_entries WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM simulated_players WHERE user_id = $1", user_id)


async def _run_cli(admin_id: int) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "services.admin.seed_simulated_players_cli",
        "--admin-id", str(admin_id),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=os.environ,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode is not None
    return proc.returncode, (stdout + stderr).decode()


async def test_seed_creates_all_ten_named_bots_disabled_and_funded(pool):
    admin_id, *_ = await create_test_admin(pool)
    existing = await pool.fetchval("SELECT count(*) FROM simulated_players")
    assert existing == 0, "roster must be empty for this test to prove all 10 seed correctly"

    created_ids: list[int] = []
    try:
        returncode, output = await _run_cli(admin_id)
        assert returncode == 0, output
        for name in SEED_NAMES:
            assert name in output
            assert "created" in output

        rows = await pool.fetch(
            "SELECT u.id, u.display_name, u.is_simulated, sp.status FROM simulated_players sp "
            "JOIN users u ON u.id = sp.user_id"
        )
        created_ids = [r["id"] for r in rows]
        assert len(rows) == len(SEED_NAMES)
        names_seen = {r["display_name"] for r in rows}
        assert names_seen == set(SEED_NAMES)
        for row in rows:
            assert row["is_simulated"] is True
            assert row["status"] == "disabled"  # never auto-activated

        for user_id in created_ids:
            cash = await ledger.get_or_create_account(pool, user_id, "user_cash")
            assert await ledger.balance(pool, cash.id) == spq.INITIAL_SIMULATED_BALANCE

        settings = await spq.get_settings_admin(pool)
        assert settings["enabled"] is False  # seeding never flips the global switch
    finally:
        for user_id in created_ids:
            await _delete_bot_completely(pool, user_id)


async def test_seed_is_idempotent_on_a_second_run(pool):
    admin_id, *_ = await create_test_admin(pool)
    existing = await pool.fetchval("SELECT count(*) FROM simulated_players")
    assert existing == 0, "roster must be empty for this test to prove idempotency cleanly"

    created_ids: list[int] = []
    try:
        first_rc, _ = await _run_cli(admin_id)
        assert first_rc == 0
        created_ids = [
            r["id"] for r in await pool.fetch(
                "SELECT u.id FROM simulated_players sp JOIN users u ON u.id = sp.user_id"
            )
        ]
        assert len(created_ids) == len(SEED_NAMES)

        second_rc, second_output = await _run_cli(admin_id)
        assert second_rc == 0
        assert "skipped (already exists)" in second_output
        assert "0 bot(s) created" in second_output

        # No duplicates and no extra funding transactions from the second run.
        count_after = await pool.fetchval("SELECT count(*) FROM simulated_players")
        assert count_after == len(SEED_NAMES)
    finally:
        for user_id in created_ids:
            await _delete_bot_completely(pool, user_id)
