"""Platform announcement: a single, admin-configurable scrolling banner
shown in the Mini App -- see migrations/versions/c7e2a9f13b8d_platform_
announcement.py's own docstring for why this exists (replacing the
spectate screen's "Reserve a card" button, which never actually did
anything real).

Same true-singleton shape services/admin/simulated_players_queries.py's
own get_settings_admin()/update_settings_admin() already use for
simulated_players_settings -- id is always 1.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from services.admin import audit

MAX_ANNOUNCEMENT_LENGTH = 280  # a marquee is read a few words at a time, not a paragraph


async def get_announcement_admin(pool: asyncpg.Pool) -> dict[str, Any]:
    row = await pool.fetchrow("SELECT text, enabled, updated_at FROM platform_announcement WHERE id = 1")
    assert row is not None
    return {
        "text": row["text"],
        "enabled": row["enabled"],
        "updated_at": row["updated_at"].isoformat(),
    }


async def update_announcement_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    text: str,
    enabled: bool,
    reason: str,
    ip_address: str | None,
) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("reason is required")
    if len(text) > MAX_ANNOUNCEMENT_LENGTH:
        raise ValueError(f"text must be at most {MAX_ANNOUNCEMENT_LENGTH} characters")
    if enabled and not text.strip():
        raise ValueError("cannot enable an empty announcement")

    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT text, enabled FROM platform_announcement WHERE id = 1 FOR UPDATE"
            )
            assert before is not None
            await conn.execute(
                "UPDATE platform_announcement SET text = $1, enabled = $2, "
                "updated_by_admin_id = $3, updated_at = now() WHERE id = 1",
                text,
                enabled,
                admin_id,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="announcement.update",
                target_type="platform_announcement",
                target_id="1",
                before=dict(before),
                after={"text": text, "enabled": enabled},
                reason=reason,
                ip_address=ip_address,
            )
    return await get_announcement_admin(pool)
