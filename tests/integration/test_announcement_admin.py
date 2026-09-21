"""services/admin/announcement_queries.py + its /announcement routes --
the admin-configurable scrolling banner shown in the Mini App's spectate
screen, replacing the old, functionally-useless "Reserve a card" button.
"""

import httpx
import pytest

from services.admin import announcement_queries as aq
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin


@pytest.fixture(autouse=True)
async def _reset_announcement(pool):
    # A real, shared singleton row -- every test here must leave it
    # exactly as it found it (disabled, empty) so it never leaks into
    # some other test or a manual QA session against this database.
    yield
    await pool.execute("UPDATE platform_announcement SET text = '', enabled = false WHERE id = 1")


async def test_update_requires_a_reason(pool):
    admin_id, *_ = await create_test_admin(pool)
    with pytest.raises(ValueError, match="reason"):
        await aq.update_announcement_admin(
            pool, admin_id=admin_id, text="hello", enabled=False, reason="  ", ip_address=None,
        )


async def test_update_rejects_text_over_the_max_length(pool):
    admin_id, *_ = await create_test_admin(pool)
    with pytest.raises(ValueError, match="280"):
        await aq.update_announcement_admin(
            pool, admin_id=admin_id, text="x" * 281, enabled=False, reason="test", ip_address=None,
        )


async def test_cannot_enable_an_empty_announcement(pool):
    admin_id, *_ = await create_test_admin(pool)
    with pytest.raises(ValueError, match="empty"):
        await aq.update_announcement_admin(
            pool, admin_id=admin_id, text="   ", enabled=True, reason="test", ip_address=None,
        )


async def test_update_sets_text_and_is_audited(pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    result = await aq.update_announcement_admin(
        pool, admin_id=admin_id, text="Deposit bonus this week!", enabled=True,
        reason="launch promo", ip_address=None,
    )
    assert result == {
        "text": "Deposit bonus this week!",
        "enabled": True,
        "updated_at": result["updated_at"],
    }
    fetched = await aq.get_announcement_admin(pool)
    assert fetched["text"] == "Deposit bonus this week!"
    assert fetched["enabled"] is True

    audit_row = await conn.fetchrow(
        "SELECT action, reason, before, after FROM admin_audit_log "
        "WHERE action = 'announcement.update' ORDER BY id DESC LIMIT 1"
    )
    assert audit_row is not None
    assert audit_row["reason"] == "launch promo"


async def test_rbac_over_real_http(admin_server, pool):
    support_headers = await _auth_headers(admin_server, pool, role="support")
    ops_headers = await _auth_headers(admin_server, pool, role="ops")

    async with httpx.AsyncClient() as client:
        # support can view...
        view_resp = await client.get(f"{admin_server}/announcement", headers=support_headers)
        assert view_resp.status_code == 200

        # ...but not manage.
        denied = await client.patch(
            f"{admin_server}/announcement", headers=support_headers,
            json={"text": "nope", "enabled": True, "reason": "should be blocked"},
        )
        assert denied.status_code == 403

        # ops can manage.
        allowed = await client.patch(
            f"{admin_server}/announcement", headers=ops_headers,
            json={"text": "Weekend tournament — join now!", "enabled": True, "reason": "e2e http test"},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["text"] == "Weekend tournament — join now!"

        # A missing reason is rejected with a clean 422, not a 500.
        bad_reason = await client.patch(
            f"{admin_server}/announcement", headers=ops_headers,
            json={"text": "x", "enabled": False, "reason": ""},
        )
        assert bad_reason.status_code == 422
