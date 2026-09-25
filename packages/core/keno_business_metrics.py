"""Part 8's revenue-model and retention metrics, computed from stored data.

Monthly GGR = DAU x sessions/day x rounds/session x tickets/round x avg stake x hold

Every term is defined so the product reproduces the real trailing-24h GGR
exactly (tests/integration/test_keno_business_metrics.py proves it) -- if
the decomposition ever stops multiplying out, one of the definitions below
has drifted.

Definitions (docs/keno/07-economics-and-bankroll.md has the long form):

- Window: the trailing 24h ending at `now`. Only *settled* tickets
  (won/lost) count -- a pending ticket has no outcome yet, and a refunded
  one never generated revenue.
- Session: a player's run of Keno tickets with no gap longer than
  SESSION_GAP (30 min, the standard web-analytics inactivity window)
  between consecutive tickets. Derived entirely from keno_tickets.created_at
  -- no client-side tracking exists or is needed. Measures the span of
  betting activity, not time spent looking at the screen; a player who
  watches for an hour and bets once has a zero-length session.
- tickets/round: tickets per (player, round) participation -- not per
  round overall -- which is what makes the formula multiply out.
- Hold: (stake - base payout - jackpot payout) / stake. The jackpot is
  player-funded, but from the operator's side it is still money paid back
  out of stakes, so it belongs in the payout side of hold.
- LTV: mean lifetime (stake - payouts) of players who placed a settled
  ticket in the trailing 30 days -- the value of the *current* player base,
  not an average dragged down by every long-churned one-ticket account.
- Dn retention: of players whose first-ever settled ticket fell on UTC day
  X, the fraction who played again on day X+n, for the most recent X where
  day X+n is already complete. NaN (not 0) when that cohort is empty -- "no
  data" and "everyone churned" are different facts.
- Deposit conversion: platform-wide (deposits aren't per-game), succeeded /
  all deposits that reached a terminal state in the window. Pending/
  processing/review deposits are excluded, not counted as failures.

Simulated players are excluded everywhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg

from packages.core import ledger, metrics

SESSION_GAP = timedelta(minutes=30)
WINDOW = timedelta(hours=24)
LTV_ACTIVE_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class Session:
    user_id: int
    start: datetime
    end: datetime
    ticket_count: int
    round_count: int


@dataclass(frozen=True)
class BusinessMetrics:
    dau: int
    sessions: int
    sessions_per_user: float
    rounds_per_session: float
    tickets_per_round: float
    avg_stake: Decimal
    hold_pct: float
    ggr: Decimal
    arpdau: Decimal
    d1_retention: float
    d7_retention: float
    d30_retention: float
    player_ltv: Decimal
    deposit_conversion_rate: float
    closed_sessions: list[Session]


def group_sessions(rows: list[tuple[int, datetime, int]]) -> list[Session]:
    """rows: (user_id, created_at, round_id), in any order."""
    by_user: dict[int, list[tuple[datetime, int]]] = {}
    for user_id, created_at, round_id in rows:
        by_user.setdefault(user_id, []).append((created_at, round_id))
    sessions: list[Session] = []
    for user_id, events in by_user.items():
        events.sort()
        start = prev = events[0][0]
        tickets = 0
        rounds: set[int] = set()
        for created_at, round_id in events:
            if created_at - prev > SESSION_GAP:
                sessions.append(Session(user_id, start, prev, tickets, len(rounds)))
                start, tickets, rounds = created_at, 0, set()
            tickets += 1
            rounds.add(round_id)
            prev = created_at
        sessions.append(Session(user_id, start, prev, tickets, len(rounds)))
    return sessions


async def _retention(conn: ledger.AsyncpgConnection, now: datetime, n: int) -> float:
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    check_day = today - timedelta(days=1)  # most recent complete day
    cohort_day = check_day - timedelta(days=n)
    row = await conn.fetchrow(
        """
        WITH firsts AS (
            SELECT t.user_id, min(t.created_at) AS first_at
            FROM keno_tickets t JOIN users u ON u.id = t.user_id
            WHERE t.status IN ('won', 'lost') AND NOT u.is_simulated
            GROUP BY t.user_id
        ), cohort AS (
            SELECT user_id FROM firsts WHERE first_at >= $1 AND first_at < $1 + interval '1 day'
        )
        SELECT
            (SELECT count(*) FROM cohort) AS size,
            (SELECT count(DISTINCT t.user_id) FROM keno_tickets t JOIN cohort c ON c.user_id = t.user_id
             WHERE t.status IN ('won', 'lost') AND t.created_at >= $2 AND t.created_at < $2 + interval '1 day')
             AS retained
        """,
        cohort_day,
        check_day,
    )
    assert row is not None
    return row["retained"] / row["size"] if row["size"] else math.nan


async def compute(pool: asyncpg.Pool, *, now: datetime | None = None) -> BusinessMetrics:
    now = now or datetime.now(timezone.utc)
    start = now - WINDOW
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.user_id, t.created_at, t.round_id, t.stake,
                   COALESCE(t.payout, 0) + COALESCE(t.jackpot_payout, 0) AS paid
            FROM keno_tickets t JOIN users u ON u.id = t.user_id
            WHERE t.status IN ('won', 'lost') AND NOT u.is_simulated
              AND t.created_at >= $1 AND t.created_at < $2
            """,
            start,
            now,
        )
        ltv = await conn.fetchval(
            """
            SELECT avg(net) FROM (
                SELECT t.user_id,
                       sum(t.stake - COALESCE(t.payout, 0) - COALESCE(t.jackpot_payout, 0)) AS net
                FROM keno_tickets t JOIN users u ON u.id = t.user_id
                WHERE t.status IN ('won', 'lost') AND NOT u.is_simulated AND t.created_at < $2
                  AND t.user_id IN (
                      SELECT user_id FROM keno_tickets
                      WHERE status IN ('won', 'lost') AND created_at >= $1 AND created_at < $2)
                GROUP BY t.user_id
            ) per_player
            """,
            now - LTV_ACTIVE_WINDOW,
            now,
        )
        deposits = await conn.fetchrow(
            """
            SELECT count(*) FILTER (WHERE p.status = 'succeeded') AS ok,
                   count(*) FILTER (WHERE p.status IN ('succeeded', 'failed', 'cancelled', 'rejected')) AS done
            FROM payments p JOIN users u ON u.id = p.user_id
            WHERE p.direction = 'in' AND NOT u.is_simulated AND p.created_at >= $1 AND p.created_at < $2
            """,
            start,
            now,
        )
        d1 = await _retention(conn, now, 1)
        d7 = await _retention(conn, now, 7)
        d30 = await _retention(conn, now, 30)

    assert deposits is not None
    handle = sum((Decimal(r["stake"]) for r in rows), start=Decimal(0))
    paid = sum((Decimal(r["paid"]) for r in rows), start=Decimal(0))
    ggr = handle - paid
    tickets = len(rows)
    dau = len({r["user_id"] for r in rows})
    participations = len({(r["user_id"], r["round_id"]) for r in rows})
    sessions = group_sessions([(r["user_id"], r["created_at"], r["round_id"]) for r in rows])
    session_rounds = sum(s.round_count for s in sessions)

    return BusinessMetrics(
        dau=dau,
        sessions=len(sessions),
        sessions_per_user=len(sessions) / dau if dau else 0.0,
        rounds_per_session=session_rounds / len(sessions) if sessions else 0.0,
        tickets_per_round=tickets / participations if participations else 0.0,
        avg_stake=(handle / tickets).quantize(Decimal("0.01")) if tickets else Decimal(0),
        hold_pct=float(ggr / handle) if handle else 0.0,
        ggr=ggr,
        arpdau=(ggr / dau).quantize(Decimal("0.01")) if dau else Decimal(0),
        d1_retention=d1,
        d7_retention=d7,
        d30_retention=d30,
        player_ltv=Decimal(ltv).quantize(Decimal("0.01")) if ltv is not None else Decimal(0),
        deposit_conversion_rate=deposits["ok"] / deposits["done"] if deposits["done"] else math.nan,
        closed_sessions=[s for s in sessions if now - s.end > SESSION_GAP],
    )


class Publisher:
    """Sets the Part 8 gauges from compute(). Holds a watermark so each
    closed session's duration is observed into the histogram exactly once
    per process, not re-observed on every refresh. A restarted process
    starts its watermark at startup time, so sessions that closed while it
    was down are never observed -- a small, documented undercount, not a
    double count."""

    def __init__(self, started_at: datetime | None = None) -> None:
        self._watermark = started_at or datetime.now(timezone.utc)

    async def publish(self, pool: asyncpg.Pool, *, now: datetime | None = None) -> BusinessMetrics:
        result = await compute(pool, now=now)
        metrics.keno_dau.set(result.dau)
        metrics.keno_sessions_per_user.set(result.sessions_per_user)
        metrics.keno_rounds_per_session.set(result.rounds_per_session)
        metrics.keno_tickets_per_round.set(result.tickets_per_round)
        metrics.keno_avg_stake.set(float(result.avg_stake))
        metrics.keno_hold_pct.set(result.hold_pct)
        metrics.keno_arpdau.set(float(result.arpdau))
        metrics.keno_d1_retention.set(result.d1_retention)
        metrics.keno_d7_retention.set(result.d7_retention)
        metrics.keno_d30_retention.set(result.d30_retention)
        metrics.keno_player_ltv.set(float(result.player_ltv))
        metrics.keno_deposit_conversion_rate.set(result.deposit_conversion_rate)
        newest = self._watermark
        for session in result.closed_sessions:
            if session.end > self._watermark:
                metrics.keno_session_duration_seconds.observe((session.end - session.start).total_seconds())
                newest = max(newest, session.end)
        self._watermark = newest
        return result
