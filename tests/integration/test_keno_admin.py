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
from tests.integration.conftest import create_funded_user
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


# --- risk-of-ruin simulator (Part 7.4) --------------------------------------

_SIM_MULTIPLIERS = {"1": "0.19", "2": "0.97", "3": "3.89", "4": "11.67", "5": "16.00"}


def _sim_body(**overrides):
    body = {
        "starting_reserve": "40000", "daily_handle": "50000", "avg_stake": "20",
        "pick_count": 5, "multipliers": _SIM_MULTIPLIERS, "jackpot_diversion_bps": 150,
        "floor": "0", "days": 30, "num_simulations": 200, "rng_seed": 1,
    }
    body.update(overrides)
    return body


async def test_simulate_risk_of_ruin_admin_matches_the_pure_function_directly() -> None:
    result = keno_queries.simulate_risk_of_ruin_admin(**_sim_body())
    assert "ending_reserve_mean" in result
    assert "floor_breach_probability" in result
    assert Decimal(result["ending_reserve_mean"]) > 0


async def test_simulate_risk_of_ruin_admin_rejects_invalid_input() -> None:
    with pytest.raises(keno_queries.InvalidKenoConfig):
        keno_queries.simulate_risk_of_ruin_admin(**_sim_body(days=0))


async def test_risk_of_ruin_endpoint_is_reachable_by_ops(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="ops")  # keno:manage includes ops
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{admin_server}/keno/risk-of-ruin", headers=headers, json=_sim_body())
    assert response.status_code == 200, response.text
    assert "floor_breach_probability" in response.json()


async def test_risk_of_ruin_endpoint_rejects_invalid_input_with_422(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/keno/risk-of-ruin", headers=headers, json=_sim_body(num_simulations=0)
        )
    assert response.status_code == 422


# --- staged-launch beta allowlist (2026-09-23) -------------------------------


async def test_add_to_beta_allowlist_is_audited(pool, conn):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    user_id = await create_funded_user(conn, Decimal("0"))

    result = await keno_queries.add_to_beta_allowlist_admin(
        pool, admin_id=admin_id, user_id=user_id, reason="internal Stage 1 tester"
    )
    assert result["user_id"] == user_id

    listed = await keno_queries.list_beta_allowlist_admin(pool)
    assert any(row["user_id"] == user_id for row in listed)

    row = await conn.fetchrow(
        "SELECT * FROM admin_audit_log WHERE action = 'keno.beta_allowlist.add' AND target_id = $1 "
        "ORDER BY id DESC LIMIT 1",
        str(user_id),
    )
    assert row["admin_id"] == admin_id
    assert row["reason"] == "internal Stage 1 tester"


async def test_add_to_beta_allowlist_requires_a_reason(pool, conn):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    user_id = await create_funded_user(conn, Decimal("0"))
    with pytest.raises(keno_queries.InvalidKenoConfig):
        await keno_queries.add_to_beta_allowlist_admin(pool, admin_id=admin_id, user_id=user_id, reason="  ")


async def test_add_to_beta_allowlist_rejects_unknown_user(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(keno_queries.InvalidKenoConfig):
        await keno_queries.add_to_beta_allowlist_admin(pool, admin_id=admin_id, user_id=999_999_999, reason="x")


async def test_remove_from_beta_allowlist_is_audited(pool, conn):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    user_id = await create_funded_user(conn, Decimal("0"))
    await keno_queries.add_to_beta_allowlist_admin(pool, admin_id=admin_id, user_id=user_id, reason="add first")

    result = await keno_queries.remove_from_beta_allowlist_admin(
        pool, admin_id=admin_id, user_id=user_id, reason="Stage 1 wrapped up"
    )
    assert result == {"removed": True}

    listed = await keno_queries.list_beta_allowlist_admin(pool)
    assert not any(row["user_id"] == user_id for row in listed)

    row = await conn.fetchrow(
        "SELECT * FROM admin_audit_log WHERE action = 'keno.beta_allowlist.remove' AND target_id = $1 "
        "ORDER BY id DESC LIMIT 1",
        str(user_id),
    )
    assert row["admin_id"] == admin_id
    assert row["reason"] == "Stage 1 wrapped up"

    # Removing again is a clean no-op, not an error -- same idempotent
    # -delete convention as every other admin remove/stop endpoint in
    # this codebase.
    result_again = await keno_queries.remove_from_beta_allowlist_admin(
        pool, admin_id=admin_id, user_id=user_id, reason="already gone"
    )
    assert result_again == {"removed": False}


async def test_beta_allowlist_endpoints_require_keno_configure_not_just_view(admin_server, pool, conn):
    user_id = await create_funded_user(conn, Decimal("0"))
    view_only_headers = await _auth_headers(admin_server, pool, role="support")  # keno:view, not keno:configure
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/keno/beta-allowlist", headers=view_only_headers,
            json={"user_id": user_id, "reason": "should be forbidden"},
        )
    assert response.status_code == 403


async def test_beta_allowlist_add_list_remove_over_http(admin_server, pool, conn):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    user_id = await create_funded_user(conn, Decimal("0"))

    async with httpx.AsyncClient() as client:
        added = await client.post(
            f"{admin_server}/keno/beta-allowlist", headers=headers,
            json={"user_id": user_id, "reason": "http add test"},
        )
    assert added.status_code == 200, added.text

    async with httpx.AsyncClient() as client:
        listed = await client.get(f"{admin_server}/keno/beta-allowlist", headers=headers)
    assert listed.status_code == 200
    assert any(row["user_id"] == user_id for row in listed.json())

    async with httpx.AsyncClient() as client:
        removed = await client.delete(
            f"{admin_server}/keno/beta-allowlist/{user_id}", headers=headers, params={"reason": "http remove test"}
        )
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"removed": True}


# --- rounds browser / reports (admin Keno UI backends) ------------------------


async def _seed_round_with_ticket(conn, *, status: str, drawn: list[int] | None) -> tuple[int, int]:
    config_id = await conn.fetchval("SELECT id FROM keno_configs ORDER BY id DESC LIMIT 1")
    tier_id = await conn.fetchval("SELECT id FROM keno_risk_tiers ORDER BY id DESC LIMIT 1")
    paytable_id = await conn.fetchval("SELECT id FROM keno_paytables ORDER BY id DESC LIMIT 1")
    round_id = await conn.fetchval(
        "INSERT INTO keno_rounds (seq, status, config_id, tier_id, server_seed, server_seed_hash, drawn_numbers) "
        "VALUES ((SELECT COALESCE(MAX(seq),0)+1 FROM keno_rounds), $1, $2, $3, $4, 'h', $5) RETURNING id",
        status, config_id, tier_id, b"secret-seed", drawn,
    )
    user_id = await create_funded_user(conn, Decimal("0"))
    await conn.execute(
        "INSERT INTO keno_tickets (round_id, user_id, paytable_id, pick_count, stake, status, payout, idempotency_key) "
        "VALUES ($1, $2, $3, 1, 10, 'lost', 0, $4)",
        round_id, user_id, paytable_id, f"test-admin-round-{uuid.uuid4()}",
    )
    return round_id, user_id


async def test_rounds_browser_lists_and_details_a_round_over_http(admin_server, pool, conn):
    round_id, user_id = await _seed_round_with_ticket(conn, status="completed", drawn=[1, 2, 3])
    headers = await _auth_headers(admin_server, pool, role="support")  # keno:view is enough
    async with httpx.AsyncClient() as client:
        listed = await client.get(f"{admin_server}/keno/rounds", headers=headers, params={"limit": 200})
        detail = await client.get(f"{admin_server}/keno/rounds/{round_id}", headers=headers)
        missing = await client.get(f"{admin_server}/keno/rounds/999999999", headers=headers)
    assert listed.status_code == 200
    assert any(r["id"] == round_id for r in listed.json())
    assert detail.status_code == 200
    body = detail.json()
    assert body["round"]["drawn_numbers"] == [1, 2, 3]
    assert [t["user_id"] for t in body["tickets"]] == [user_id]
    assert missing.status_code == 404


async def test_round_detail_hides_a_live_rounds_seed_and_draw(admin_server, pool, conn):
    # Same terminal-only rule as the player API: a live round's draw is
    # persisted before it's revealed, and an admin screen must not leak it.
    round_id, _ = await _seed_round_with_ticket(conn, status="drawing", drawn=[4, 5, 6])
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        detail = await client.get(f"{admin_server}/keno/rounds/{round_id}", headers=headers)
    assert detail.json()["round"]["drawn_numbers"] is None
    assert detail.json()["round"]["server_seed"] is None


async def test_reports_endpoints_return_real_shapes_over_http(admin_server, pool, conn):
    await _seed_round_with_ticket(conn, status="completed", drawn=[7])
    headers = await _auth_headers(admin_server, pool, role="finance")
    async with httpx.AsyncClient() as client:
        daily = await client.get(f"{admin_server}/keno/reports/daily", headers=headers, params={"days": 3})
        kpis = await client.get(f"{admin_server}/keno/reports/kpis", headers=headers)
    assert daily.status_code == 200
    today = daily.json()[0]
    assert Decimal(today["ggr"]) == Decimal(today["handle"]) - Decimal(today["paid"])
    assert kpis.status_code == 200
    assert {"dau", "hold_pct", "arpdau", "d1_retention", "player_ltv"} <= set(kpis.json())
