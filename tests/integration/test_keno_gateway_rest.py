"""Real-HTTP tests for the Keno REST routes (build spec Part 10),
against the actual gateway FastAPI app via uvicorn -- same pattern as
tests/integration/test_gateway_rest.py's own Bingo-side coverage."""

from __future__ import annotations

import itertools
import json
import random
import uuid
from decimal import Decimal

import httpx
import pytest

from packages.core import keno, ledger
from tests.integration.conftest import build_init_data, fund_user, next_telegram_id
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_gateway_rest import http_base

pytestmark = pytest.mark.asyncio

_version_counter = itertools.count(random.randint(5 * 10**6, 6 * 10**6))


def _next_version() -> int:
    return next(_version_counter)


_MULTIPLIERS = {1: Decimal("3.40")}


async def _seed_open_round(conn, *, keno_enabled: bool = True, beta_restricted: bool = False) -> int:
    version = _next_version()
    config_id = await conn.fetchval(
        """
        INSERT INTO keno_configs
            (version, round_cycle_seconds, betting_seconds, draw_seconds, result_seconds,
             max_tickets_per_user_per_round, per_user_round_capacity_share_bps, keno_enabled,
             beta_restricted)
        VALUES ($1, 45, 25, 12, 8, 5, 5000, $2, $3)
        RETURNING id
        """,
        version,
        keno_enabled,
        beta_restricted,
    )
    tier_id = await conn.fetchval(
        """
        INSERT INTO keno_risk_tiers
            (tier_number, version, min_reserve, max_pick_count, max_top_multiplier,
             stake_options, max_win_per_ticket, max_round_exposure_pct, paytable_profile)
        VALUES (1, $1, 0, 5, 16, ARRAY[10.00,20.00,50.00]::numeric(18,2)[], 800, 0.90, 'low_variance')
        RETURNING id
        """,
        version,
    )
    stats = keno.compute_paytable_stats(1, _MULTIPLIERS)
    await conn.execute(
        """
        INSERT INTO keno_paytables (pick_count, version, profile, multipliers, computed_rtp_bps, hit_frequency_bps,
                                     max_multiplier, volatility)
        VALUES (1, $1, 'low_variance', $2::jsonb, $3, $4, $5, $6)
        """,
        version,
        json.dumps({str(k): str(v) for k, v in _MULTIPLIERS.items()}),
        int(stats.rtp * 10000),
        int(stats.hit_frequency * 10000),
        stats.max_multiplier,
        stats.volatility,
    )
    await conn.execute("UPDATE keno_rounds SET status = 'completed' WHERE status NOT IN ('completed','failed','voided')")
    reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
    house_float = await ledger.get_or_create_account(conn, None, "house_float")
    await ledger.post(
        conn, "keno_reserve_deposit",
        [ledger.Entry(house_float.id, Decimal("-100000")), ledger.Entry(reserve.id, Decimal("100000"))],
        idempotency_key=f"test-gw-reserve-{uuid.uuid4()}", created_by="test",
    )
    round_id = await conn.fetchval(
        """
        INSERT INTO keno_rounds (seq, status, config_id, tier_id, server_seed_hash, betting_opened_at)
        VALUES ((SELECT COALESCE(MAX(seq),0)+1 FROM keno_rounds), 'betting_open', $1, $2, $3, now())
        RETURNING id
        """,
        config_id, tier_id, keno.server_seed_hash(keno.generate_server_seed()),
    )
    return round_id


async def test_api_keno_state_requires_authorization(gateway_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{http_base(gateway_server)}/api/keno/state")
    assert response.status_code == 401


async def test_api_keno_state_reflects_the_real_open_round(gateway_server, pool, conn):
    round_id = await _seed_open_round(conn)
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/keno/state", headers={"Authorization": f"tma {init_data}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["round_id"] == round_id
    assert body["status"] == "betting_open"
    assert "10.00" in body["stake_options"]
    assert body["min_picks"] == 1
    # Part 13's live potential-payout preview needs the whole grid up
    # front, not a per-pick_count round trip. The shared dev database
    # accumulates other pick counts' real paytable rows for this same
    # 'low_variance' profile from other tests/seed data -- checking the
    # one row _seed_open_round itself inserted (pick_count=1) is what
    # keeps this robust against that ambient data, the same discipline
    # this suite's other shared-database tests already use.
    assert body["paytable"]["1"] == {"1": "3.40"}


async def test_place_ticket_over_http_debits_real_balance(gateway_server, pool, conn):
    round_id = await _seed_open_round(conn)
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)

    async with httpx.AsyncClient() as client:
        # Triggers the same lazy get_or_create_user_by_telegram_id() path
        # a real first-ever Mini App open would (no /start with the bot
        # yet) -- the user row genuinely doesn't exist until this call.
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_id_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    user_id = user_id_row["id"]
    await fund_user(conn, user_id, Decimal("1000.00"))

    idem_key = f"test-http-{uuid.uuid4()}"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/keno/tickets",
            headers={"Authorization": f"tma {init_data}"},
            json={"picks": [7], "stake": "10", "idempotency_key": idem_key},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["round_id"] == round_id
    assert body["picks"] == [7]
    assert body["status"] == "pending"

    balance = await ledger.user_balance_snapshot(pool, user_id)
    assert Decimal(balance["cash"]) == Decimal("990.00")

    # Idempotent retry over HTTP -- same key, no second debit.
    async with httpx.AsyncClient() as client:
        retry = await client.post(
            f"{http_base(gateway_server)}/api/keno/tickets",
            headers={"Authorization": f"tma {init_data}"},
            json={"picks": [7], "stake": "10", "idempotency_key": idem_key},
        )
    assert retry.status_code == 200
    assert retry.json()["id"] == body["id"]
    balance_after_retry = await ledger.user_balance_snapshot(pool, user_id)
    assert Decimal(balance_after_retry["cash"]) == Decimal("990.00")


async def test_place_ticket_rejects_invalid_picks_with_typed_error(gateway_server, pool, conn):
    await _seed_open_round(conn)
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
        response = await client.post(
            f"{http_base(gateway_server)}/api/keno/tickets",
            headers={"Authorization": f"tma {init_data}"},
            json={"picks": [1, 1, 2], "stake": "10", "idempotency_key": f"test-{uuid.uuid4()}"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_picks"


async def test_round_detail_endpoint_exposes_verification_payload_once_terminal(gateway_server, pool, conn):
    round_id = await _seed_open_round(conn)
    # Force the round straight to a terminal, seed-revealed state for this
    # read-only endpoint test -- the full lifecycle is already proven for
    # real by tests/integration/test_keno_round_engine.py.
    server_seed = keno.generate_server_seed()
    public_seed = "gw-test-public-seed"
    drawn = keno.derive_keno_draw(server_seed, public_seed)
    await conn.execute(
        "UPDATE keno_rounds SET status = 'completed', server_seed = $2, public_seed = $3, drawn_numbers = $4, "
        "completed_at = now() WHERE id = $1",
        round_id, server_seed, public_seed, drawn,
    )
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/keno/rounds/{round_id}", headers={"Authorization": f"tma {init_data}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["verified"] is True
    assert body["drawn_numbers"] == drawn
    assert bytes.fromhex(body["server_seed"]) == server_seed


async def test_round_detail_hides_server_seed_and_draw_before_the_round_is_terminal(gateway_server, pool, conn):
    """A spec-compliance audit caught this as a real commit-reveal leak:
    server_seed is written to keno_rounds at round CREATION (long before
    settlement), and drawn_numbers is written in full the instant drawing
    starts (well before the paced WS reveal finishes) -- gating this
    response on "is the column non-null" (the old bug) exposed both
    early. This seeds a round in an explicitly non-terminal status
    ('drawing') with both columns already populated -- exactly the real
    window the bug was reachable in -- and asserts round_detail() hides
    them anyway, while server_seed_hash and public_seed (never secret)
    still come through."""
    round_id = await _seed_open_round(conn)
    server_seed = keno.generate_server_seed()
    public_seed = "gw-test-non-terminal-public-seed"
    drawn = keno.derive_keno_draw(server_seed, public_seed)
    await conn.execute(
        "UPDATE keno_rounds SET status = 'drawing', server_seed = $2, public_seed = $3, drawn_numbers = $4, "
        "draw_started_at = now() WHERE id = $1",
        round_id, server_seed, public_seed, drawn,
    )
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/keno/rounds/{round_id}", headers={"Authorization": f"tma {init_data}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "drawing"
    assert body["server_seed"] is None
    assert body["drawn_numbers"] is None
    assert body["verified"] is None
    # Never secret, so still exposed even mid-round.
    assert body["public_seed"] == public_seed
    assert body["server_seed_hash"] is not None


async def test_round_detail_404_for_unknown_round(gateway_server, pool, conn):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/keno/rounds/99999999", headers={"Authorization": f"tma {init_data}"}
        )
    assert response.status_code == 404


# --- autoplay / multi-race (2026-09-21) ------------------------------------


async def _real_user_headers(gateway_server, pool):
    """A genuine authenticated user via the same lazy get_or_create path
    /api/me itself uses (no /start with the bot yet) -- returns headers
    ready for any other request, and the user's own real id."""
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    return {"Authorization": f"tma {init_data}"}, user_id


async def test_start_autoplay_over_http_creates_a_real_session(gateway_server, pool):
    headers, user_id = await _real_user_headers(gateway_server, pool)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/keno/autoplay",
            headers=headers,
            json={"picks": [1, 2], "stake": "10", "rounds_total": 5},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["picks"] == [1, 2]
    assert body["stake"] == "10.00"
    assert body["rounds_total"] == 5
    assert body["status"] == "active"

    row = await pool.fetchrow("SELECT user_id, status FROM keno_autoplay_sessions WHERE id = $1", body["id"])
    assert row["user_id"] == user_id
    assert row["status"] == "active"


async def test_start_autoplay_rejects_a_config_with_no_stop_condition(gateway_server, pool):
    headers, _ = await _real_user_headers(gateway_server, pool)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/keno/autoplay",
            headers=headers,
            json={"picks": [1], "stake": "10"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_autoplay_config"


async def test_start_autoplay_rejects_a_second_active_session_over_http(gateway_server, pool):
    headers, _ = await _real_user_headers(gateway_server, pool)
    async with httpx.AsyncClient() as client:
        first = await client.post(
            f"{http_base(gateway_server)}/api/keno/autoplay",
            headers=headers, json={"picks": [1], "stake": "10", "rounds_total": 5},
        )
        assert first.status_code == 200
        second = await client.post(
            f"{http_base(gateway_server)}/api/keno/autoplay",
            headers=headers, json={"picks": [2], "stake": "20", "rounds_total": 3},
        )
    assert second.status_code == 422
    assert second.json()["detail"] == "autoplay_session_already_active"


async def test_get_autoplay_returns_null_when_nothing_is_active(gateway_server, pool):
    headers, _ = await _real_user_headers(gateway_server, pool)
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{http_base(gateway_server)}/api/keno/autoplay", headers=headers)
    assert response.status_code == 200
    assert response.json() is None


async def test_get_and_stop_autoplay_over_http(gateway_server, pool):
    headers, _ = await _real_user_headers(gateway_server, pool)
    async with httpx.AsyncClient() as client:
        started = await client.post(
            f"{http_base(gateway_server)}/api/keno/autoplay",
            headers=headers, json={"picks": [1], "stake": "10", "stop_on_win_amount": "500"},
        )
        assert started.status_code == 200

        fetched = await client.get(f"{http_base(gateway_server)}/api/keno/autoplay", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["id"] == started.json()["id"]
        assert fetched.json()["stop_on_win_amount"] == "500.00"

        stopped = await client.delete(f"{http_base(gateway_server)}/api/keno/autoplay", headers=headers)
        assert stopped.status_code == 200
        assert stopped.json() == {"stopped": True}

        # A second stop is an idempotent no-op, not an error.
        stopped_again = await client.delete(f"{http_base(gateway_server)}/api/keno/autoplay", headers=headers)
        assert stopped_again.status_code == 200
        assert stopped_again.json() == {"stopped": False}

        after = await client.get(f"{http_base(gateway_server)}/api/keno/autoplay", headers=headers)
        assert after.status_code == 200
        assert after.json() is None


# --- staged-launch beta allowlist (2026-09-23, a CTO review's own required
# gate before any Stage 1) -----------------------------------------------
#
# The real bug this covers: GET /api/keno/state used to return live round
# state to *any* authenticated user the instant a single round had ever
# been created, never checking keno_enabled at all -- the Mini App button
# was effectively visible to real users regardless of keno_enabled. These
# three tests are the exact matrix a CTO review asked for: disabled hides
# it, enabled-but-not-allowlisted still hides it (and still rejects a bet),
# allowlisted shows it and lets a bet through.


async def _authed_headers(gateway_server, pool, conn) -> tuple[dict[str, str], int]:
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_id = (await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id))["id"]
    await fund_user(conn, user_id, Decimal("1000.00"))
    return {"Authorization": f"tma {init_data}"}, user_id


async def test_keno_state_hidden_when_keno_disabled(gateway_server, pool, conn):
    await _seed_open_round(conn, keno_enabled=False, beta_restricted=True)
    headers, _user_id = await _authed_headers(gateway_server, pool, conn)
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{http_base(gateway_server)}/api/keno/state", headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"] == "keno_not_configured"


async def test_keno_state_hidden_and_betting_rejected_when_enabled_but_not_allowlisted(gateway_server, pool, conn):
    await _seed_open_round(conn, keno_enabled=True, beta_restricted=True)
    headers, _user_id = await _authed_headers(gateway_server, pool, conn)

    async with httpx.AsyncClient() as client:
        state_response = await client.get(f"{http_base(gateway_server)}/api/keno/state", headers=headers)
    # Identical response to "never configured" (test above) -- see
    # game_center_state()'s own docstring for why the two are
    # deliberately indistinguishable from the outside.
    assert state_response.status_code == 503
    assert state_response.json()["detail"] == "keno_not_configured"

    async with httpx.AsyncClient() as client:
        ticket_response = await client.post(
            f"{http_base(gateway_server)}/api/keno/tickets",
            headers=headers,
            json={"picks": [7], "stake": "10", "idempotency_key": f"test-beta-{uuid.uuid4()}"},
        )
    assert ticket_response.status_code == 422
    assert ticket_response.json()["detail"] == "keno_not_on_allowlist"


async def test_keno_state_visible_and_betting_allowed_once_allowlisted(gateway_server, pool, conn):
    round_id = await _seed_open_round(conn, keno_enabled=True, beta_restricted=True)
    headers, user_id = await _authed_headers(gateway_server, pool, conn)

    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    await conn.execute(
        "INSERT INTO keno_beta_allowlist (user_id, added_by_admin_id, reason) VALUES ($1, $2, 'e2e test')",
        user_id, admin_id,
    )

    async with httpx.AsyncClient() as client:
        state_response = await client.get(f"{http_base(gateway_server)}/api/keno/state", headers=headers)
    assert state_response.status_code == 200
    assert state_response.json()["round_id"] == round_id

    async with httpx.AsyncClient() as client:
        ticket_response = await client.post(
            f"{http_base(gateway_server)}/api/keno/tickets",
            headers=headers,
            json={"picks": [7], "stake": "10", "idempotency_key": f"test-beta-{uuid.uuid4()}"},
        )
    assert ticket_response.status_code == 200, ticket_response.text
    assert ticket_response.json()["round_id"] == round_id
