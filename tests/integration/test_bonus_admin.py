"""Admin-facing bonus/referral operations (services/admin/bonus_queries.py)
and their RBAC boundary over real HTTP.
"""

import uuid
from decimal import Decimal

import httpx
import pytest

from services.admin import bonus_queries
from tests.integration.conftest import create_user
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin


def rule_name() -> str:
    return f"test-rule-{uuid.uuid4()}"


async def test_create_bonus_rule_admin(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    rule_id = await bonus_queries.create_bonus_rule_admin(
        pool, admin_id=admin_id, name=rule_name(), trigger_type="referral_reward", reward_type="flat",
        reward_amount=Decimal("10.00"), reward_percentage=None, reward_cap=None,
        min_qualifying_deposit=Decimal("20.00"), wagering_multiplier=Decimal("3"), expiry_days=30,
        max_grants_per_user=5, ip_address=None,
    )
    rules = await bonus_queries.list_bonus_rules_admin(pool)
    assert any(r["id"] == rule_id for r in rules)


async def test_create_bonus_rule_rejects_an_unknown_trigger_type(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(bonus_queries.InvalidBonusRule):
        await bonus_queries.create_bonus_rule_admin(
            pool, admin_id=admin_id, name=rule_name(), trigger_type="not_a_real_trigger", reward_type="flat",
            reward_amount=Decimal("10.00"), reward_percentage=None, reward_cap=None,
            min_qualifying_deposit=Decimal("0"), wagering_multiplier=Decimal("3"), expiry_days=None,
            max_grants_per_user=1, ip_address=None,
        )


async def test_create_bonus_rule_rejects_a_flat_reward_with_no_amount(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    with pytest.raises(bonus_queries.InvalidBonusRule):
        await bonus_queries.create_bonus_rule_admin(
            pool, admin_id=admin_id, name=rule_name(), trigger_type="referral_reward", reward_type="flat",
            reward_amount=None, reward_percentage=None, reward_cap=None,
            min_qualifying_deposit=Decimal("0"), wagering_multiplier=Decimal("3"), expiry_days=None,
            max_grants_per_user=1, ip_address=None,
        )


async def test_update_bonus_rule_admin_changes_a_field(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    rule_id = await bonus_queries.create_bonus_rule_admin(
        pool, admin_id=admin_id, name=rule_name(), trigger_type="referral_reward", reward_type="flat",
        reward_amount=Decimal("10.00"), reward_percentage=None, reward_cap=None,
        min_qualifying_deposit=Decimal("0"), wagering_multiplier=Decimal("3"), expiry_days=None,
        max_grants_per_user=1, ip_address=None,
    )
    updated = await bonus_queries.update_bonus_rule_admin(
        pool, admin_id=admin_id, rule_id=rule_id, changes={"is_active": False}, ip_address=None
    )
    assert updated is True
    rules = await bonus_queries.list_bonus_rules_admin(pool)
    match = next(r for r in rules if r["id"] == rule_id)
    assert match["is_active"] is False


async def test_update_bonus_rule_revalidates_reward_shape_against_the_resulting_row(pool):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    rule_id = await bonus_queries.create_bonus_rule_admin(
        pool, admin_id=admin_id, name=rule_name(), trigger_type="referral_reward", reward_type="flat",
        reward_amount=Decimal("10.00"), reward_percentage=None, reward_cap=None,
        min_qualifying_deposit=Decimal("0"), wagering_multiplier=Decimal("3"), expiry_days=None,
        max_grants_per_user=1, ip_address=None,
    )
    # Switching to 'percentage' without ever setting reward_percentage
    # must be rejected -- the resulting row would violate the DB's own
    # chk_bonus_rules_reward_shape constraint.
    with pytest.raises(bonus_queries.InvalidBonusRule):
        await bonus_queries.update_bonus_rule_admin(
            pool, admin_id=admin_id, rule_id=rule_id, changes={"reward_type": "percentage"}, ip_address=None
        )


async def test_grant_manual_bonus_admin_credits_user_bonus_and_audits(pool, conn):
    from packages.core import ledger

    admin_id, *_ = await create_test_admin(pool, role="finance")
    user_id = await create_user(conn)

    bonus_id = await bonus_queries.grant_manual_bonus_admin(
        pool, admin_id=admin_id, user_id=user_id, amount=Decimal("25.00"),
        wagering_multiplier=Decimal("2"), expiry_days=None, reason="Goodwill credit",
        ip_address="10.0.0.1",
    )
    bonus_account = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    assert await ledger.balance(conn, bonus_account.id) == Decimal("25.00")

    row = await conn.fetchrow(
        "SELECT admin_id, action, reason, ip_address FROM admin_audit_log "
        "WHERE action = 'bonuses.manual_grant' AND target_id = $1",
        str(bonus_id),
    )
    assert row["admin_id"] == admin_id
    assert row["reason"] == "Goodwill credit"
    assert row["ip_address"] == "10.0.0.1"


async def test_list_bonuses_admin_filters_by_status(pool, conn):
    admin_id, *_ = await create_test_admin(pool, role="finance")
    user_id = await create_user(conn)
    bonus_id = await bonus_queries.grant_manual_bonus_admin(
        pool, admin_id=admin_id, user_id=user_id, amount=Decimal("5.00"),
        wagering_multiplier=Decimal("1"), expiry_days=None, reason="test", ip_address=None,
    )
    active = await bonus_queries.list_bonuses_admin(pool, user_id=user_id, status="active")
    assert any(b["id"] == bonus_id for b in active)
    converted = await bonus_queries.list_bonuses_admin(pool, user_id=user_id, status="converted")
    assert not any(b["id"] == bonus_id for b in converted)


async def test_revoke_bonus_admin_reverses_and_audits(pool, conn):
    from packages.core import ledger

    admin_id, *_ = await create_test_admin(pool, role="finance")
    user_id = await create_user(conn)
    bonus_id = await bonus_queries.grant_manual_bonus_admin(
        pool, admin_id=admin_id, user_id=user_id, amount=Decimal("8.00"),
        wagering_multiplier=Decimal("1"), expiry_days=None, reason="test", ip_address=None,
    )
    revoked = await bonus_queries.revoke_bonus_admin(
        pool, admin_id=admin_id, bonus_id=bonus_id, reason="fraud finding", ip_address=None
    )
    assert revoked is True
    bonus_account = await ledger.get_or_create_account(conn, user_id, "user_bonus")
    assert await ledger.balance(conn, bonus_account.id) == Decimal("0.00")


async def test_revoke_bonus_admin_returns_false_for_an_unknown_id(pool):
    admin_id, *_ = await create_test_admin(pool, role="finance")
    revoked = await bonus_queries.revoke_bonus_admin(
        pool, admin_id=admin_id, bonus_id=999_999_999, reason="x", ip_address=None
    )
    assert revoked is False


async def test_referral_funnel_counts_are_accurate(pool, conn):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await conn.execute("UPDATE users SET referred_by = $1 WHERE id = $2", referrer_id, referee_id)

    # referred_by was already set above -- "before" already reflects that
    # registration, so only the *reward* changes between here and "after".
    before = await bonus_queries.referral_funnel_admin(pool)

    rule_id = await bonus_queries.create_bonus_rule_admin(
        pool, admin_id=admin_id, name=rule_name(), trigger_type="referral_reward", reward_type="flat",
        reward_amount=Decimal("10.00"), reward_percentage=None, reward_cap=None,
        min_qualifying_deposit=Decimal("0"), wagering_multiplier=Decimal("3"), expiry_days=None,
        max_grants_per_user=10, ip_address=None,
    )
    from packages.core.referrals import maybe_grant_referral_bonus

    await maybe_grant_referral_bonus(conn, user_id=referee_id, deposit_amount=Decimal("100.00"))

    after = await bonus_queries.referral_funnel_admin(pool)
    assert after["registered_via_referral"] == before["registered_via_referral"]  # unchanged
    assert after["referrals_rewarded"] == before["referrals_rewarded"] + 1


async def test_fraud_candidates_flags_a_shared_payout_account_pair(pool, conn):
    referrer_id = await create_user(conn)
    referee_id = await create_user(conn)
    await conn.execute("UPDATE users SET referred_by = $1 WHERE id = $2", referrer_id, referee_id)
    await conn.execute(
        "INSERT INTO payment_methods (user_id, kind, account_ref, holder_name) "
        "VALUES ($1, 'telebirr', '0922000000', 'Same Person')",
        referrer_id,
    )
    await conn.execute(
        "INSERT INTO payment_methods (user_id, kind, account_ref, holder_name) "
        "VALUES ($1, 'telebirr', '0922000000', 'Same Person')",
        referee_id,
    )
    result = await bonus_queries.referral_fraud_candidates_admin(pool)
    pairs = result["shared_payout_account_pairs"]
    assert any(p["referrer_id"] == referrer_id and p["referee_id"] == referee_id for p in pairs)


# --- RBAC over real HTTP -------------------------------------------------


async def test_support_cannot_manage_bonus_rules_but_can_view_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="support")
    async with httpx.AsyncClient() as client:
        create = await client.post(
            f"{admin_server}/bonus-rules",
            json={"name": rule_name(), "trigger_type": "referral_reward", "reward_type": "flat",
                  "reward_amount": "10.00"},
            headers=headers,
        )
        view = await client.get(f"{admin_server}/bonuses", headers=headers)
    assert create.status_code == 403
    assert view.status_code == 200


async def test_ops_can_manage_rules_but_not_grant_bonuses_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        create = await client.post(
            f"{admin_server}/bonus-rules",
            json={"name": rule_name(), "trigger_type": "referral_reward", "reward_type": "flat",
                  "reward_amount": "10.00"},
            headers=headers,
        )
        assert create.status_code == 200, create.text

        grant = await client.post(
            f"{admin_server}/bonuses/grant",
            json={"user_id": 1, "amount": "10.00", "reason": "test"},
            headers=headers,
        )
    assert grant.status_code == 403


async def test_finance_can_grant_a_manual_bonus_over_http(admin_server, pool, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_user(conn)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/bonuses/grant",
            json={"user_id": user_id, "amount": "12.00", "reason": "goodwill gesture"},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    assert "id" in response.json()


async def test_support_cannot_view_fraud_signals_over_http(admin_server, pool):
    # bonuses:view_fraud_signals deliberately matches risk:view's own
    # roles (ops/finance/superadmin) -- support is the one role excluded.
    headers = await _auth_headers(admin_server, pool, role="support")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/bonuses/fraud-candidates", headers=headers)
    assert response.status_code == 403


async def test_ops_and_finance_can_both_view_fraud_signals_over_http(admin_server, pool):
    for role in ("ops", "finance"):
        headers = await _auth_headers(admin_server, pool, role=role)
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{admin_server}/bonuses/fraud-candidates", headers=headers)
        assert response.status_code == 200, f"role {role!r} should see fraud signals"
