"""keno production launch seed: real config, tier ladder, paytables

Closes Part 7 audit gaps #6 and #8 (DECISIONS.md, 2026-09-21): "only the
low_variance paytable profile exists anywhere (code or tests)" and "no
production seed/bootstrap for the Part 7.5 launch defaults... only
present in test fixtures." Every prior keno_configs/keno_risk_tiers/
keno_paytables row in this codebase, including every low_variance table,
was ad-hoc test-fixture data hand-picked per test file to exercise
settlement math -- never a real, RTP-validated production table, and
Tiers 2-4 have never existed anywhere at all, even in a test.

Real numbers, not guesses:
- The tier ladder (min_reserve, max_pick_count, max_top_multiplier,
  stake_options, max_win_per_ticket) is keno.md Part 7.2's own table,
  verbatim. stake_options for Tiers 2-4 extend Tier 1's own given
  10/20/50 ladder up to each tier's own given max_stake (unchanged at
  50 for Tier 2; 100 for Tier 3 adds one rung; 200 for Tier 4 adds two).
- max_round_exposure_pct = 0.10 (10%) for every tier -- operator
  -approved default, chosen over the far looser 0.50 sitting in test
  fixtures: at the 45s launch round cycle (~1,900 rounds/day), even
  several unlucky rounds at 99.9th-percentile exposure in a row
  shouldn't meaningfully threaten the reserve.
- Every paytable's multipliers were computed with packages.core.keno's
  own exact hypergeometric math (compute_rtp/compute_paytable_stats,
  pool=80/draws=20) to land at the spec's 82% target RTP (7.5's
  "Target RTP: 82%"), verified inside [floor 75%, ceiling 97%], for
  every pick count 1-10 in both profiles. Both profiles are
  monotonically increasing in top prize by pick count (a first design
  pass wasn't -- picks=4's freely-solved top exceeded picks=5's
  spec-fixed 16x anchor, and picks=9 exceeded picks=10's fixed 5000x --
  fixed by explicitly anchoring every pick count's top multiplier
  rather than letting the solver pick it). low_variance's own hit
  -frequency clears the spec's "50%+ at 4-5 picks" claim comfortably
  (69%/77%). The four tier-anchor multipliers (16x@5, 60x@6, 500x@8,
  5000x@10) are keno.md's own literal "do not raise at launch" figures.

keno_configs.keno_enabled stays false -- this seed makes Keno
*configurable* for real, not *live*. Going live is a separate, explicit
operator decision (flipping keno_enabled), same standing rule as never
moving real money without confirmation first.

Revision ID: c4e8f1a9b6d3
Revises: f3a8c6e2b9d4
Create Date: 2026-09-22

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8f1a9b6d3"
down_revision: Union[str, None] = "f3a8c6e2b9d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            INSERT INTO keno_configs
                (version, number_pool_size, draw_count, min_picks, max_picks,
                 round_cycle_seconds, betting_seconds, draw_seconds, result_seconds,
                 max_tickets_per_user_per_round, per_user_round_capacity_share_bps,
                 jackpot_diversion_bps, rtp_floor_bps, rtp_ceiling_bps,
                 daily_payout_circuit_breaker_multiple, keno_enabled)
            VALUES
                (1, 80, 20, 1, 10, 45, 25, 12, 8, 3, 2000, 150, 7500, 9700, 3.0, false)
            """
        )
    )

    tier_rows = [
        # tier_number, min_reserve, max_pick_count, max_top_multiplier, stake_options, max_win_per_ticket, profile
        (1, "0.00", 5, "16.00", [10.00, 20.00, 50.00], "800.00", "low_variance"),
        (2, "200000.00", 6, "60.00", [10.00, 20.00, 50.00], "3000.00", "low_variance"),
        (3, "1000000.00", 8, "500.00", [10.00, 20.00, 50.00, 100.00], "50000.00", "standard"),
        (4, "5000000.00", 10, "5000.00", [10.00, 20.00, 50.00, 100.00, 200.00], "250000.00", "standard"),
    ]
    tier1_id = None
    for tier_number, min_reserve, max_pick_count, max_top_multiplier, stakes, max_win, profile in tier_rows:
        stakes_sql = "ARRAY[" + ",".join(f"{s:.2f}" for s in stakes) + "]::numeric(18,2)[]"
        row_id = conn.execute(
            sa.text(
                f"""
                INSERT INTO keno_risk_tiers
                    (tier_number, version, min_reserve, max_pick_count, max_top_multiplier,
                     stake_options, max_win_per_ticket, max_round_exposure_pct, paytable_profile)
                VALUES
                    (:tier_number, 1, :min_reserve, :max_pick_count, :max_top_multiplier,
                     {stakes_sql}, :max_win, 0.10, :profile)
                RETURNING id
                """
            ),
            {
                "tier_number": tier_number,
                "min_reserve": min_reserve,
                "max_pick_count": max_pick_count,
                "max_top_multiplier": max_top_multiplier,
                "max_win": max_win,
                "profile": profile,
            },
        ).scalar_one()
        if tier_number == 1:
            tier1_id = row_id

    conn.execute(
        sa.text(
            "INSERT INTO keno_tier_state (id, current_tier_id) VALUES (1, :tier1_id) "
            "ON CONFLICT (id) DO UPDATE SET current_tier_id = :tier1_id, updated_at = now()"
        ),
        {"tier1_id": tier1_id},
    )

    # Both paytable profiles, every pick count 1-10 (low_variance stops at
    # 6 -- Tier 1/2's own max_pick_count ceiling, no tier ever offers more
    # picks under this profile). See this file's own docstring for how
    # every number here was actually computed, not guessed.
    paytable_rows = [
        # pick_count, profile, multipliers_json, computed_rtp_bps, hit_frequency_bps, max_multiplier, volatility
        (1, "low_variance", '{"1": "3.28"}', 8200, 2500, "3.28", "1.4203"),
        (2, "low_variance", '{"1": "1.21", "2": "6.00"}', 8203, 4399, "6.00", "1.4310"),
        (3, "low_variance", '{"1": "0.50", "2": "3.35", "3": "10.00"}', 8190, 5835, "10.00", "1.5433"),
        (4, "low_variance", '{"1": "0.34", "2": "1.62", "3": "6.73", "4": "13.00"}', 8225, 6917, "13.00", "1.5518"),
        (5, "low_variance", '{"1": "0.19", "2": "0.97", "3": "3.89", "4": "11.67", "5": "16.00"}', 8174, 7728, "16.00", "1.6380"),
        (6, "low_variance", '{"1": "0.15", "2": "0.60", "3": "2.25", "4": "7.51", "5": "21.04", "6": "60.00"}', 8188, 8334, "60.00", "1.8842"),
        (1, "standard", '{"1": "3.28"}', 8200, 2500, "3.28", "1.4203"),
        (2, "standard", '{"2": "13.64"}', 8201, 601, "13.64", "3.2425"),
        (3, "standard", '{"2": "2.95", "3": "29.55"}', 8193, 1526, "29.55", "3.5570"),
        (4, "standard", '{"3": "9.19", "4": "137.89"}', 8199, 463, "137.89", "7.8247"),
        (5, "standard", '{"3": "4.20", "4": "25.21", "5": "252.07"}', 8199, 967, "252.07", "7.0336"),
        (6, "standard", '{"3": "2.13", "4": "10.66", "5": "63.94", "6": "319.71"}', 8199, 1616, "319.71", "5.3851"),
        (7, "standard", '{"3": "0.78", "4": "5.20", "5": "31.18", "6": "181.91", "7": "400.00"}', 8202, 2366, "400.00", "6.1134"),
        (8, "standard", '{"3": "0.56", "4": "2.78", "5": "13.89", "6": "69.45", "7": "333.36", "8": "500.00"}', 8211, 3171, "500.00", "5.8213"),
        (9, "standard", '{"4": "1.11", "5": "7.40", "6": "44.38", "7": "258.90", "8": "1331.47", "9": "2500.00"}', 8201, 1531, "2500.00", "10.6998"),
        (10, "standard", '{"4": "0.60", "5": "3.60", "6": "21.00", "7": "120.01", "8": "660.07", "9": "3600.41", "10": "5000.00"}', 8199, 2120, "5000.00", "13.0178"),
    ]
    # keno_paytables' own UNIQUE constraint is (pick_count, version) --
    # deliberately NOT including profile, so a given pick_count's version
    # numbers are shared across every profile that defines that pick
    # count (picks 1-6 exist in both profiles here). version=1 for
    # low_variance, version=2 for standard avoids the collision.
    profile_version = {"low_variance": 1, "standard": 2}
    for pick_count, profile, multipliers_json, rtp_bps, hit_bps, max_mult, vol in paytable_rows:
        conn.execute(
            sa.text(
                """
                INSERT INTO keno_paytables
                    (pick_count, version, profile, multipliers, computed_rtp_bps, hit_frequency_bps,
                     max_multiplier, volatility)
                VALUES
                    (:pick_count, :version, :profile, CAST(:multipliers AS jsonb), :rtp_bps, :hit_bps, :max_mult, :vol)
                """
            ),
            {
                "pick_count": pick_count,
                "version": profile_version[profile],
                "profile": profile,
                "multipliers": multipliers_json,
                "rtp_bps": rtp_bps,
                "hit_bps": hit_bps,
                "max_mult": max_mult,
                "vol": vol,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    # keno_tier_state.current_tier_id is NOT NULL with no "unset" state --
    # downgrading past this seed would leave it pointing at a row this
    # same migration is about to delete. Since nothing references Keno at
    # all before this migration (keno_enabled defaults false, and this is
    # the very first seed), the only safe downgrade is to delete
    # everything this migration added, in dependency order, and leave
    # keno_tier_state with no row at all -- exactly its state before this
    # migration ran (the initial schema migration never seeds it either).
    conn.execute(sa.text("DELETE FROM keno_tier_state WHERE id = 1"))
    conn.execute(sa.text("DELETE FROM keno_paytables WHERE version IN (1, 2)"))
    conn.execute(sa.text("DELETE FROM keno_risk_tiers WHERE version = 1"))
    conn.execute(sa.text("DELETE FROM keno_configs WHERE version = 1"))
