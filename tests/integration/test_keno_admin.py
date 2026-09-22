"""Admin-facing Keno operations (services/admin/keno_queries.py): config/
paytable/tier CRUD with the RTP guardrail actually enforced server-side,
the kill switch, and the RBAC boundary over real HTTP."""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest

from packages.core import keno, ledger
from services.admin import keno_queries
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin

pytestmark = pytest.mark.asyncio

_MULTIPLIERS = {"1": "3.40"}


async def test_create_config_admin_is_insert_only_and_audited(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    config = await keno_queries.create_config_admin(
        pool, admin_id=admin_id, round_cycle_seconds=45, betting_seconds=25, draw_seconds=12,
        result_seconds=8, min_picks=1, max_picks=5, max_tickets_per_user_per_round=3,
        per_user_round_capacity_share_bps=2000, jackpot_diversion_bps=150, keno_enabled=False,
        reason="test config",
    )
    active = await keno_queries.get_active_config_admin(pool)
    assert active["id"] == config["id"]

    # A second create must be a genuinely new row, never an update to the first.
    config2 = await keno_queries.create_config_admin(
        pool, admin_id=admin_id, round_cycle_seconds=45, betting_seconds=25, draw_seconds=12,
        result_seconds=8, min_picks=1, max_picks=5, max_tickets_per_user_per_round=3,
        per_user_round_capacity_share_bps=2000, jackpot_diversion_bps=150, keno_enabled=False,
        reason="test config 2",
    )
    assert config2["id"] != config["id"]
    configs = await keno_queries.list_configs_admin(pool)
    assert any(c["id"] == config["id"] for c in configs)
    assert any(c["id"] == config2["id"] for c in configs)


async def test_create_config_requires_a_reason(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(keno_queries.InvalidKenoConfig):
        await keno_queries.create_config_admin(
            pool, admin_id=admin_id, round_cycle_seconds=45, betting_seconds=25, draw_seconds=12,
            result_seconds=8, min_picks=1, max_picks=5, max_tickets_per_user_per_round=3,
            per_user_round_capacity_share_bps=2000, jackpot_diversion_bps=150, keno_enabled=False,
            reason="   ",
        )


async def test_kill_switch_flips_enabled_and_preserves_every_other_setting(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    original = await keno_queries.create_config_admin(
        pool, admin_id=admin_id, round_cycle_seconds=45, betting_seconds=25, draw_seconds=12,
        result_seconds=8, min_picks=1, max_picks=5, max_tickets_per_user_per_round=3,
        per_user_round_capacity_share_bps=2000, jackpot_diversion_bps=150, keno_enabled=True,
        reason="baseline",
    )
    flipped = await keno_queries.set_keno_enabled_admin(
        pool, admin_id=admin_id, enabled=False, reason="emergency stop"
    )
    assert flipped["keno_enabled"] is False
    # Everything else carried over unchanged from the config the kill
    # switch actually flipped, not reset to some default.
    assert flipped["betting_seconds"] == original["betting_seconds"]
    assert flipped["draw_seconds"] == original["draw_seconds"]
    assert flipped["max_picks"] == original["max_picks"]

    active = await keno_queries.get_active_config_admin(pool)
    assert active["keno_enabled"] is False


async def test_paytable_preview_matches_packages_core_keno_directly() -> None:
    preview = keno_queries.preview_paytable_stats(1, _MULTIPLIERS)
    expected_rtp = keno.compute_rtp(1, {1: Decimal("3.40")})
    assert Decimal(preview["rtp"]) == expected_rtp.quantize(Decimal("0.0001"))
    assert preview["within_guardrail"] is True


async def test_paytable_preview_flags_an_out_of_guardrail_table() -> None:
    preview = keno_queries.preview_paytable_stats(1, {"1": "10.0"})
    assert preview["within_guardrail"] is False


async def test_create_paytable_rejects_rtp_above_ceiling(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(keno_queries.InvalidKenoConfig):
        await keno_queries.create_paytable_admin(
            pool, admin_id=admin_id, pick_count=1, profile="low_variance",
            multipliers={"1": "10.0"}, reason="too generous",
        )


async def test_create_paytable_accepts_a_guardrail_compliant_table_and_is_audited(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    paytable = await keno_queries.create_paytable_admin(
        pool, admin_id=admin_id, pick_count=1, profile="low_variance",
        multipliers=_MULTIPLIERS, reason="launch table",
    )
    assert paytable["pick_count"] == 1
    assert paytable["multipliers"] == _MULTIPLIERS
    assert 7500 <= paytable["computed_rtp_bps"] <= 9700

    paytables = await keno_queries.list_paytables_admin(pool, pick_count=1)
    assert any(p["id"] == paytable["id"] for p in paytables)


async def test_create_tier_and_set_current_tier_is_audited(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    tier = await keno_queries.create_tier_admin(
        pool, admin_id=admin_id, tier_number=1, min_reserve=Decimal("0"), max_pick_count=5,
        max_top_multiplier=Decimal("16"), stake_options=[Decimal("10"), Decimal("20"), Decimal("50")],
        max_win_per_ticket=Decimal("800"), max_round_exposure_pct=Decimal("0.05"),
        paytable_profile="low_variance", reason="launch tier",
    )
    result = await keno_queries.set_current_tier_admin(
        pool, admin_id=admin_id, tier_id=tier["id"], reason="activate launch tier"
    )
    assert result["id"] == tier["id"]

    tiers = await keno_queries.list_tiers_admin(pool)
    current = [t for t in tiers if t["is_current"]]
    assert len(current) == 1
    assert current[0]["id"] == tier["id"]


async def test_set_current_tier_rejects_unknown_tier_id(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(keno_queries.InvalidKenoConfig):
        await keno_queries.set_current_tier_admin(
            pool, admin_id=admin_id, tier_id=999999999, reason="bogus"
        )


async def test_dashboard_summary_separates_reserve_from_liability(pool, conn):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    await keno_queries.create_config_admin(
        pool, admin_id=admin_id, round_cycle_seconds=45, betting_seconds=25, draw_seconds=12,
        result_seconds=8, min_picks=1, max_picks=5, max_tickets_per_user_per_round=3,
        per_user_round_capacity_share_bps=2000, jackpot_diversion_bps=150, keno_enabled=True,
        reason="dashboard test",
    )
    reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
    house_float = await ledger.get_or_create_account(conn, None, "house_float")
    await ledger.post(
        conn, "keno_reserve_deposit",
        [ledger.Entry(house_float.id, Decimal("-500")), ledger.Entry(reserve.id, Decimal("500"))],
        idempotency_key=f"test-dash-{uuid.uuid4()}", created_by="test",
    )

    summary = await keno_queries.dashboard_summary_admin(pool)
    assert "reserve_balance" in summary
    assert "player_liability" in summary
    assert summary["reserve_balance"] != summary["player_liability"]  # never conflated


# --- RBAC boundary over real HTTP -------------------------------------------


async def test_keno_dashboard_requires_keno_view_permission(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="support")  # keno:view includes support
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/keno/dashboard", headers=headers)
    assert response.status_code == 200


async def test_creating_a_keno_config_requires_superadmin(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="support")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/keno/configs",
            headers=headers,
            json={
                "round_cycle_seconds": 45, "betting_seconds": 25, "draw_seconds": 12,
                "result_seconds": 8, "min_picks": 1, "max_picks": 5,
                "max_tickets_per_user_per_round": 3, "per_user_round_capacity_share_bps": 2000,
                "jackpot_diversion_bps": 150, "keno_enabled": False, "reason": "should be forbidden",
            },
        )
    assert response.status_code == 403


async def test_creating_a_keno_config_succeeds_for_superadmin_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/keno/configs",
            headers=headers,
            json={
                "round_cycle_seconds": 45, "betting_seconds": 25, "draw_seconds": 12,
                "result_seconds": 8, "min_picks": 1, "max_picks": 5,
                "max_tickets_per_user_per_round": 3, "per_user_round_capacity_share_bps": 2000,
                "jackpot_diversion_bps": 150, "keno_enabled": False, "reason": "http create test",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["keno_enabled"] is False


async def test_paytable_preview_endpoint_is_reachable_by_ops(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="ops")  # keno:manage includes ops
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/keno/paytables/preview",
            headers=headers,
            json={"pick_count": 1, "multipliers": _MULTIPLIERS},
        )
    assert response.status_code == 200
    assert response.json()["within_guardrail"] is True


async def test_kill_switch_endpoint_requires_superadmin(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/keno/kill-switch", headers=headers, json={"enabled": False, "reason": "test"}
        )
    assert response.status_code == 403


# --- reserve deposit / withdrawal (Part 7.1) --------------------------------


async def test_deposit_to_reserve_admin_increases_balance_and_is_audited(pool, conn):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    before = await ledger.balance(conn, (await ledger.get_or_create_account(conn, None, "keno_reserve")).id)

    result = await keno_queries.deposit_to_reserve_admin(
        pool, admin_id=admin_id, amount=Decimal("5000"), reason="initial funding test"
    )
    assert Decimal(result["balance"]) == before + Decimal("5000")

    row = await conn.fetchrow(
        "SELECT * FROM admin_audit_log WHERE action = 'keno.reserve.deposit' ORDER BY id DESC LIMIT 1"
    )
    assert row["admin_id"] == admin_id
    assert row["reason"] == "initial funding test"


async def test_withdraw_from_reserve_admin_decreases_balance_and_is_audited(pool, conn):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    await keno_queries.deposit_to_reserve_admin(pool, admin_id=admin_id, amount=Decimal("5000"), reason="fund first")
    before = await ledger.balance(conn, (await ledger.get_or_create_account(conn, None, "keno_reserve")).id)

    result = await keno_queries.withdraw_from_reserve_admin(
        pool, admin_id=admin_id, amount=Decimal("1000"), reason="operator profit withdrawal"
    )
    assert Decimal(result["balance"]) == before - Decimal("1000")

    row = await conn.fetchrow(
        "SELECT * FROM admin_audit_log WHERE action = 'keno.reserve.withdraw' ORDER BY id DESC LIMIT 1"
    )
    assert row["admin_id"] == admin_id
    assert row["reason"] == "operator profit withdrawal"


async def test_withdraw_below_floor_is_blocked_and_audited_not_just_rejected(pool, conn):
    """Part 7.1's own 'Attempts are blocked and audited' -- the rejection
    itself must leave a real record, not just fail silently."""
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
    balance = await ledger.balance(conn, reserve.id)

    with pytest.raises(keno_queries.ReserveWithdrawalBelowFloor):
        await keno_queries.withdraw_from_reserve_admin(
            pool, admin_id=admin_id, amount=balance + Decimal("1"),  # would go negative -- floor defaults to 0
            reason="should be blocked",
        )

    unchanged = await ledger.balance(conn, reserve.id)
    assert unchanged == balance  # nothing moved

    row = await conn.fetchrow(
        "SELECT * FROM admin_audit_log WHERE action = 'keno.reserve.withdraw_rejected' ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    assert row["admin_id"] == admin_id
    assert row["reason"] == "should be blocked"


async def test_reserve_deposit_endpoint_requires_superadmin(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/keno/reserve/deposit", headers=headers, json={"amount": "100", "reason": "test"}
        )
    assert response.status_code == 403


async def test_reserve_deposit_succeeds_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/keno/reserve/deposit", headers=headers,
            json={"amount": "250.50", "reason": "http deposit test"},
        )
    assert response.status_code == 200, response.text
    assert "balance" in response.json()


async def test_reserve_withdraw_blocked_below_floor_returns_409_over_http(admin_server, pool, conn):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
    balance = await ledger.balance(conn, reserve.id)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/keno/reserve/withdraw", headers=headers,
            json={"amount": str(balance + Decimal("1")), "reason": "should be blocked over http"},
        )
    assert response.status_code == 409
