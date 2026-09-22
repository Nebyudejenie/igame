"""Admin-controlled payment configuration: which company destinations
manual deposits get paid into (manual_payment_destinations) and which
provider/direction combinations are currently live
(payment_provider_availability). Both are payments:configure-gated
(superadmin only -- narrower than payments:approve, see rbac.py's own
comment on why).
"""

import httpx

from packages.core.config import Settings, get_settings
from services.admin import queries
from services.payments.availability import get_payment_availability
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin


async def test_seeded_availability_matches_the_launch_principle_chapa_plus_manual(pool):
    # "The product must be able to launch with ONE AUTOMATIC PROVIDER +
    # MANUAL FALLBACK" -- the migration seeds exactly this, not four
    # providers all live from day one.
    rows = await queries.get_payment_provider_availability(pool)
    by_key = {(r["provider"], r["direction"]): r["enabled"] for r in rows}
    assert by_key[("chapa", "in")] is True
    assert by_key[("chapa", "out")] is True
    assert by_key[("manual", "in")] is True
    assert by_key[("manual", "out")] is True
    assert by_key[("santimpay", "in")] is False
    assert by_key[("santimpay", "out")] is False
    assert by_key[("arifpay", "in")] is False
    assert by_key[("arifpay", "out")] is False


async def test_set_availability_writes_an_audit_row(pool, conn):
    admin_id, *_ = await create_test_admin(pool)

    updated = await queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="santimpay", direction="in", enabled=True,
        reason="SantimPay integration approved for launch", ip_address="10.0.0.1",
    )
    assert updated is True

    rows = await queries.get_payment_provider_availability(pool)
    match = next(r for r in rows if r["provider"] == "santimpay" and r["direction"] == "in")
    assert match["enabled"] is True

    audit_row = await conn.fetchrow(
        "SELECT action, before, after, reason FROM admin_audit_log "
        "WHERE target_type = 'payment_provider_availability' AND target_id = 'santimpay:in' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert audit_row["action"] == "payment_provider_availability.set"
    assert audit_row["reason"] == "SantimPay integration approved for launch"

    # Restore -- this table is shared, session-wide state, not per-test data.
    await queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="santimpay", direction="in", enabled=False,
        reason="test cleanup", ip_address="10.0.0.1",
    )


async def test_set_availability_rejects_an_unknown_provider_direction_pair(pool):
    admin_id, *_ = await create_test_admin(pool)
    updated = await queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="chapa", direction="sideways", enabled=True,
        reason="nonsense", ip_address="10.0.0.1",
    )
    assert updated is False


async def test_create_and_update_manual_payment_destination(pool, conn):
    admin_id, *_ = await create_test_admin(pool)

    destination_id = await queries.create_manual_payment_destination_admin(
        pool, admin_id=admin_id, method_kind="cbe_birr", account_ref="1000123456789",
        account_name="Zemen Game PLC", instructions="Reference your Zemen Game user id in the memo",
        ip_address="10.0.0.1",
    )
    assert isinstance(destination_id, int)

    rows = await queries.list_manual_payment_destinations(pool)
    match = next(r for r in rows if r["id"] == destination_id)
    assert match["method_kind"] == "cbe_birr"
    assert match["account_ref"] == "1000123456789"
    assert match["is_active"] is True

    updated = await queries.update_manual_payment_destination_admin(
        pool, admin_id=admin_id, destination_id=destination_id, changes={"is_active": False},
        reason="account closed", ip_address="10.0.0.1",
    )
    assert updated is True

    rows_after = await queries.list_manual_payment_destinations(pool)
    match_after = next(r for r in rows_after if r["id"] == destination_id)
    assert match_after["is_active"] is False

    audit_row = await conn.fetchrow(
        "SELECT action, reason FROM admin_audit_log WHERE target_type = 'manual_payment_destination' "
        "AND target_id = $1 ORDER BY id DESC LIMIT 1",
        str(destination_id),
    )
    assert audit_row["action"] == "manual_payment_destinations.update"
    assert audit_row["reason"] == "account closed"


async def test_update_destination_effective_dates_from_a_real_json_body(pool):
    """Regression: web/admin's own "Edit destination" form submits
    effective_from/effective_until as plain ISO strings inside the
    generic `changes` dict (services/admin/app.py's
    UpdateManualPaymentDestinationRequest.changes: dict[str, Any] has no
    per-field schema to coerce them the way create_manual_payment_
    destination_admin's own dedicated `datetime | None` parameter
    already does) -- asyncpg previously rejected the resulting str
    outright with a real 500 (DataError: expected a datetime.datetime
    instance, got 'str'), caught live via a real HTTP PATCH before this
    fix, not a hypothetical.
    """
    admin_id, *_ = await create_test_admin(pool)
    destination_id = await queries.create_manual_payment_destination_admin(
        pool, admin_id=admin_id, method_kind="telebirr", account_ref="0911000000",
        account_name="Zemen Game PLC", instructions=None, ip_address="10.0.0.1",
    )

    updated = await queries.update_manual_payment_destination_admin(
        pool, admin_id=admin_id, destination_id=destination_id,
        changes={"effective_from": "2026-09-05T09:00:00", "effective_until": "2026-09-06T12:33:00"},
        reason="test window", ip_address="10.0.0.1",
    )
    assert updated is True

    rows = await queries.list_manual_payment_destinations(pool)
    match = next(r for r in rows if r["id"] == destination_id)
    assert match["effective_from"].isoformat().startswith("2026-09-05T09:00:00")
    assert match["effective_until"].isoformat().startswith("2026-09-06T12:33:00")

    # Clearing a date (the form's own "leave blank for always-valid") must
    # also work -- an empty string, not a null, is what a cleared HTML
    # form field actually sends.
    cleared = await queries.update_manual_payment_destination_admin(
        pool, admin_id=admin_id, destination_id=destination_id,
        changes={"effective_until": ""},
        reason="remove expiry", ip_address="10.0.0.1",
    )
    assert cleared is True
    rows_after = await queries.list_manual_payment_destinations(pool)
    match_after = next(r for r in rows_after if r["id"] == destination_id)
    assert match_after["effective_until"] is None


async def test_update_destination_rejects_an_unknown_field(pool):
    admin_id, *_ = await create_test_admin(pool)
    destination_id = await queries.create_manual_payment_destination_admin(
        pool, admin_id=admin_id, method_kind="telebirr", account_ref="0911000000",
        account_name="Zemen Game PLC", instructions=None, ip_address="10.0.0.1",
    )
    try:
        await queries.update_manual_payment_destination_admin(
            pool, admin_id=admin_id, destination_id=destination_id, changes={"secret_key": "nope"},
            reason="x", ip_address="10.0.0.1",
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


async def test_finance_can_view_but_not_configure_payment_availability_over_http(admin_server, pool):
    finance_headers = await _auth_headers(admin_server, pool, role="finance")

    async with httpx.AsyncClient() as client:
        view_response = await client.get(f"{admin_server}/payment-provider-availability", headers=finance_headers)
        assert view_response.status_code == 200

        configure_response = await client.patch(
            f"{admin_server}/payment-provider-availability/chapa/in",
            headers=finance_headers,
            json={"enabled": False, "reason": "testing"},
        )
    assert configure_response.status_code == 403


async def test_superadmin_can_configure_payment_availability_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")

    async with httpx.AsyncClient() as client:
        response = await client.patch(
            f"{admin_server}/payment-provider-availability/arifpay/in",
            headers=headers,
            json={"enabled": True, "reason": "approved for a market pilot"},
        )
        assert response.status_code == 200
        assert response.json()["updated"] is True

        # Restore -- shared, session-wide state.
        await client.patch(
            f"{admin_server}/payment-provider-availability/arifpay/in",
            headers=headers,
            json={"enabled": False, "reason": "test cleanup"},
        )


async def test_finance_cannot_create_manual_payment_destinations_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="finance")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/manual-payment-destinations",
            headers=headers,
            json={"method_kind": "telebirr", "account_ref": "0911000000", "account_name": "Zemen Game PLC"},
        )
    assert response.status_code == 403


# --- get_payment_availability (services/payments/availability.py) --
# the player-facing side: what the Mini App's GET /api/payment-methods
# and the bot's /deposit and /withdraw commands actually see, combining
# the admin toggle above with whether a provider is genuinely wired up
# at all.


async def test_chapa_and_manual_available_by_default_santimpay_arifpay_never_are(pool):
    # The seeded defaults (chapa+manual live, santimpay/arifpay off) plus
    # the real, already-configured test-environment Chapa credentials --
    # this is "the platform as it would actually appear to a player
    # today," not a synthetic scenario.
    methods = await get_payment_availability(pool, get_settings())
    assert "chapa" in methods["deposit"]
    assert "manual" in methods["deposit"]
    assert "chapa" in methods["withdraw"]
    assert "manual" in methods["withdraw"]
    assert "santimpay" not in methods["deposit"]
    assert "arifpay" not in methods["deposit"]


async def test_santimpay_stays_unavailable_even_if_an_admin_enables_the_toggle(pool):
    # The whole point of hardcoding _IMPLEMENTED_PROVIDERS: no adapter
    # class exists for santimpay, so an admin flipping the DB toggle on
    # (maybe in anticipation of a future integration) must never make it
    # appear to a player before real code actually backs it.
    admin_id, *_ = await create_test_admin(pool)
    await queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="santimpay", direction="in", enabled=True,
        reason="test: simulate a premature toggle", ip_address=None,
    )
    try:
        methods = await get_payment_availability(pool, get_settings())
        assert "santimpay" not in methods["deposit"]
    finally:
        await queries.set_payment_provider_availability_admin(
            pool, admin_id=admin_id, provider="santimpay", direction="in", enabled=False,
            reason="test cleanup", ip_address=None,
        )


async def test_chapa_deposit_unavailable_without_a_return_or_callback_url_even_with_a_key(pool):
    # Mirrors the exact real-world gate services/gateway/app.py's own
    # /api/deposit already enforces (app.state.chapa is None or not
    # settings.miniapp_url or not settings.payments_public_base_url) --
    # an admin toggle can't substitute for genuinely missing deploy-time
    # config. Either URL being empty is enough to refuse -- checked
    # separately, not just "both empty at once".
    settings = Settings(chapa_api_key="a-real-looking-key", miniapp_url="", payments_public_base_url="https://pay.test")
    methods = await get_payment_availability(pool, settings)
    assert "chapa" not in methods["deposit"]

    settings = Settings(chapa_api_key="a-real-looking-key", miniapp_url="https://app.test", payments_public_base_url="")
    methods = await get_payment_availability(pool, settings)
    assert "chapa" not in methods["deposit"]

    # Withdrawals don't need either URL (no checkout/return_url
    # involved), so chapa withdrawal stays available on key alone.
    assert "chapa" in methods["withdraw"]


async def test_no_providers_available_when_chapa_unconfigured_and_manual_disabled(pool):
    admin_id, *_ = await create_test_admin(pool)
    await queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="manual", direction="in", enabled=False,
        reason="test: simulate manual disabled too", ip_address=None,
    )
    try:
        settings = Settings(chapa_api_key="", miniapp_url="", payments_public_base_url="")
        methods = await get_payment_availability(pool, settings)
        assert methods["deposit"] == []
    finally:
        await queries.set_payment_provider_availability_admin(
            pool, admin_id=admin_id, provider="manual", direction="in", enabled=True,
            reason="test cleanup", ip_address=None,
        )
