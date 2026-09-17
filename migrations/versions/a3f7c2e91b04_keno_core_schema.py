"""keno core schema

Adds Keno as a purely additive product alongside Bingo: its own
independent keno_* table set (no FK into rooms/rounds/round_entries --
see docs/keno/00-discovery.md section C on why), plus two backward
-compatible CHECK-constraint widenings so Keno can post through the
*existing* ledger (packages/core/ledger.py) rather than building a
second wallet. Every existing row, every existing query, every existing
CHECK-constrained value continues to work unchanged -- both widenings are
proven round-trip-safe by tests/integration/test_keno_migration.py,
which inserts one row of every pre-existing kind before and after this
migration and confirms both still succeed, then downgrades and confirms
the pre-existing kinds still work with the narrowed constraint restored.

Revision ID: a3f7c2e91b04
Revises: a1c9e7f4d2b6
Create Date: 2026-09-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f7c2e91b04"
down_revision: Union[str, None] = "a1c9e7f4d2b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Existing values preserved byte-for-byte; new values appended at the end
# so a diff against the ledger_foundation migration's own list is trivial
# to eyeball.
ACCOUNT_KINDS = (
    "user_cash",
    "user_bonus",
    "user_locked",
    "house_revenue",
    "house_float",
    "pot_escrow",
    "provider_settlement",
    "promo_expense",
    "keno_reserve",
    "keno_jackpot_pool",
)

TRANSACTION_KINDS = (
    "deposit",
    "withdrawal",
    "stake",
    "payout",
    "refund",
    "commission",
    "bonus_grant",
    "bonus_convert",
    "adjustment",
    "keno_stake",
    "keno_payout",
    "keno_refund",
    "keno_jackpot_contribution",
    "keno_jackpot_payout",
    "keno_reserve_deposit",
    "keno_reserve_withdrawal",
)

ROUND_STATUSES = (
    "scheduled",
    "betting_open",
    "betting_closed",
    "drawing",
    "draw_complete",
    "settling",
    "completed",
    "failed",
    "voided",
)

TICKET_STATUSES = ("pending", "won", "lost", "refunded")

PAYTABLE_PROFILES = ("low_variance", "standard")


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Widen the two existing ledger CHECK constraints. Additive only:
    # every pre-existing allowed value is repeated verbatim, nothing
    # removed, nothing renamed. ledger_transactions.round_id/payment_id
    # keep their existing hard FKs into rounds(id)/payments(id)
    # unchanged and are simply never populated by Keno code (see
    # docs/keno/00-discovery.md) -- no schema change needed for that
    # part of the integration.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_kind_check")
    accounts_kinds_sql = ", ".join(f"'{k}'" for k in ACCOUNT_KINDS)
    op.execute(f"ALTER TABLE accounts ADD CONSTRAINT accounts_kind_check CHECK (kind IN ({accounts_kinds_sql}))")

    op.execute("ALTER TABLE ledger_transactions DROP CONSTRAINT ledger_transactions_kind_check")
    txn_kinds_sql = ", ".join(f"'{k}'" for k in TRANSACTION_KINDS)
    op.execute(
        f"ALTER TABLE ledger_transactions ADD CONSTRAINT ledger_transactions_kind_check "
        f"CHECK (kind IN ({txn_kinds_sql}))"
    )

    # ------------------------------------------------------------------
    # keno_configs -- insert-only versioning: a "change" is a new row
    # with a later effective_from, never an UPDATE to an existing row's
    # settings. The engine always reads the row with the latest
    # effective_from <= now() and pins its id onto every round created
    # under it (keno_rounds.config_id) -- so "effective from the next
    # round only" (spec Part 15) falls out of this shape for free, with
    # zero flag-flipping race to get wrong.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE keno_configs (
          id                                  bigserial PRIMARY KEY,
          version                              int NOT NULL,
          number_pool_size                     int NOT NULL DEFAULT 80 CHECK (number_pool_size > 0),
          draw_count                           int NOT NULL DEFAULT 20
                                                CHECK (draw_count > 0 AND draw_count <= number_pool_size),
          min_picks                            int NOT NULL DEFAULT 1 CHECK (min_picks >= 1),
          max_picks                            int NOT NULL DEFAULT 10 CHECK (max_picks >= min_picks),
          round_cycle_seconds                  int NOT NULL CHECK (round_cycle_seconds > 0),
          betting_seconds                      int NOT NULL CHECK (betting_seconds > 0),
          draw_seconds                         int NOT NULL CHECK (draw_seconds > 0),
          result_seconds                       int NOT NULL CHECK (result_seconds > 0),
          max_tickets_per_user_per_round        int NOT NULL CHECK (max_tickets_per_user_per_round > 0),
          per_user_round_capacity_share_bps     int NOT NULL DEFAULT 2000
                                                CHECK (per_user_round_capacity_share_bps BETWEEN 0 AND 10000),
          jackpot_diversion_bps                int NOT NULL DEFAULT 150
                                                CHECK (jackpot_diversion_bps BETWEEN 0 AND 10000),
          rtp_floor_bps                        int NOT NULL DEFAULT 7500 CHECK (rtp_floor_bps BETWEEN 0 AND 10000),
          rtp_ceiling_bps                      int NOT NULL DEFAULT 9700
                                                CHECK (rtp_ceiling_bps BETWEEN rtp_floor_bps AND 10000),
          daily_payout_circuit_breaker_multiple numeric(6,2) NOT NULL DEFAULT 3.0
                                                CHECK (daily_payout_circuit_breaker_multiple > 0),
          keno_enabled                          boolean NOT NULL DEFAULT false,
          effective_from                        timestamptz NOT NULL DEFAULT now(),
          created_by_admin_id                   bigint REFERENCES admin_users(id),
          created_at                            timestamptz NOT NULL DEFAULT now(),
          CHECK (draw_count <= number_pool_size)
        );
        CREATE INDEX ix_keno_configs_effective ON keno_configs (effective_from DESC);
        """
    )

    # ------------------------------------------------------------------
    # keno_paytables -- one versioned row per (pick_count, version).
    # computed_rtp_bps/hit_frequency_bps/max_multiplier/volatility are a
    # snapshot of packages.core.keno.compute_paytable_stats() at save
    # time (Part 3.2: "the admin paytable editor must compute and
    # display exact RTP ... live"), stored so a historical round's
    # pinned paytable can be audited without re-deriving stats from a
    # multipliers blob whose own guardrail-validity might change if the
    # code's guardrail constants ever change later.
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE keno_paytables (
          id                   bigserial PRIMARY KEY,
          pick_count           int NOT NULL CHECK (pick_count BETWEEN 1 AND 10),
          version              int NOT NULL,
          profile              text NOT NULL CHECK (profile IN ({", ".join(f"'{p}'" for p in PAYTABLE_PROFILES)})),
          multipliers          jsonb NOT NULL,
          computed_rtp_bps     int NOT NULL,
          hit_frequency_bps    int NOT NULL,
          max_multiplier       numeric(18,2) NOT NULL,
          volatility           numeric(18,4) NOT NULL,
          effective_from       timestamptz NOT NULL DEFAULT now(),
          created_by_admin_id  bigint REFERENCES admin_users(id),
          created_at           timestamptz NOT NULL DEFAULT now(),
          UNIQUE (pick_count, version)
        );
        CREATE INDEX ix_keno_paytables_effective ON keno_paytables (pick_count, effective_from DESC);
        """
    )

    # ------------------------------------------------------------------
    # keno_risk_tiers -- Part 7.2's tier ladder, versioned the same
    # insert-only way. tier_number is the human-facing 1/2/3/4; version
    # lets an operator retune an existing tier's limits without losing
    # the history of what a round under an earlier version actually saw
    # (keno_rounds.tier_id pins the exact row, not just the tier_number).
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE keno_risk_tiers (
          id                       bigserial PRIMARY KEY,
          tier_number              int NOT NULL CHECK (tier_number > 0),
          version                  int NOT NULL,
          min_reserve              numeric(18,2) NOT NULL CHECK (min_reserve >= 0),
          max_pick_count           int NOT NULL CHECK (max_pick_count BETWEEN 1 AND 10),
          max_top_multiplier       numeric(18,2) NOT NULL CHECK (max_top_multiplier > 0),
          stake_options            numeric(18,2)[] NOT NULL,
          max_win_per_ticket       numeric(18,2) NOT NULL CHECK (max_win_per_ticket > 0),
          max_round_exposure_pct   numeric(5,4) NOT NULL CHECK (max_round_exposure_pct BETWEEN 0 AND 1),
          paytable_profile         text NOT NULL CHECK (paytable_profile IN ('low_variance', 'standard')),
          effective_from           timestamptz NOT NULL DEFAULT now(),
          created_by_admin_id      bigint REFERENCES admin_users(id),
          created_at               timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tier_number, version)
        );
        CREATE INDEX ix_keno_risk_tiers_effective ON keno_risk_tiers (tier_number, effective_from DESC);
        """
    )

    # Singleton row tracking the promotion/demotion state machine (Part
    # 7.2: promotion needs the reserve above a tier's threshold for 7
    # consecutive days; demotion is immediate). A periodic sweep (the
    # Keno engine worker) is the only writer.
    op.execute(
        """
        CREATE TABLE keno_tier_state (
          id                 bigint PRIMARY KEY DEFAULT 1,
          current_tier_id    bigint NOT NULL REFERENCES keno_risk_tiers(id),
          candidate_tier_id  bigint REFERENCES keno_risk_tiers(id),
          candidate_since    timestamptz,
          updated_at         timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT keno_tier_state_singleton CHECK (id = 1)
        );
        """
    )

    # ------------------------------------------------------------------
    # keno_rounds -- the state machine (Part 5.1). config_id/tier_id are
    # pinned at round creation and never re-read for that round again
    # (Part 6.2: "An admin editing the paytable mid-round must not alter
    # in-flight results"). drawn_numbers goes from NULL to a real value
    # exactly once, enforced by a trigger below, not just application
    # discipline.
    # ------------------------------------------------------------------
    statuses_sql = ", ".join(f"'{s}'" for s in ROUND_STATUSES)
    op.execute(
        f"""
        CREATE TABLE keno_rounds (
          id                    bigserial PRIMARY KEY,
          seq                   bigint NOT NULL UNIQUE,
          status                text NOT NULL CHECK (status IN ({statuses_sql})),
          config_id             bigint NOT NULL REFERENCES keno_configs(id),
          tier_id               bigint NOT NULL REFERENCES keno_risk_tiers(id),
          server_seed           bytea,
          server_seed_hash      text NOT NULL,
          public_seed           text,
          ticket_ids_hash       text,
          drawn_numbers         smallint[],
          -- How many of drawn_numbers have been broadcast to clients so
          -- far (Part 5.1/5.3): the draw itself is computed and
          -- persisted as one atomic, deterministic operation the
          -- instant betting closes (never partially computed, never
          -- re-randomized), but *revealing* it to clients is paced out
          -- over draw_seconds for the ball-by-ball animation (Part 13).
          -- If a worker crashes mid-reveal, a recovering worker resumes
          -- broadcasting from reveal_index+1 using the already-persisted
          -- drawn_numbers -- it never recomputes or restarts the reveal
          -- from zero.
          reveal_index          int NOT NULL DEFAULT 0,
          total_stake           numeric(18,2) NOT NULL DEFAULT 0,
          total_payout          numeric(18,2) NOT NULL DEFAULT 0,
          ticket_count          int NOT NULL DEFAULT 0,
          -- Running CLT accumulators for the Part 7.3 exposure estimate
          -- (see packages.core.keno_exposure's own module docstring for
          -- the full mean+variance reasoning) -- total_expected_payout
          -- is sum(stake*RTP) and total_payout_variance is
          -- sum(stake^2*volatility^2) across every accepted ticket.
          -- projected_exposure is the derived display value
          -- (expected + 3.09*sqrt(variance)) at the moment of the last
          -- accepted ticket, kept for cheap dashboard reads.
          total_expected_payout  numeric(18,2) NOT NULL DEFAULT 0,
          total_payout_variance  numeric(24,6) NOT NULL DEFAULT 0,
          projected_exposure    numeric(18,2),
          jackpot_hit           boolean NOT NULL DEFAULT false,
          worker_id             text,
          failure_reason        text,
          scheduled_at          timestamptz NOT NULL DEFAULT now(),
          betting_opened_at     timestamptz,
          betting_closed_at     timestamptz,
          draw_started_at       timestamptz,
          draw_completed_at     timestamptz,
          settled_at            timestamptz,
          completed_at          timestamptz
        );
        CREATE INDEX ix_keno_rounds_status ON keno_rounds (status);
        -- Recovery-sweep scan (mirrors ix_rounds_room_status's own role
        -- for Bingo): cheap lookup of every non-terminal round.
        CREATE INDEX ix_keno_rounds_nonterminal ON keno_rounds (status)
          WHERE status NOT IN ('completed', 'failed', 'voided');
        -- At most one round may ever be betting_open at a time --
        -- packages.core.keno_tickets.place_ticket() finds "the" current
        -- round via "WHERE status = 'betting_open' ORDER BY id DESC
        -- LIMIT 1", which is only unambiguous if this is a real,
        -- DB-enforced invariant and not just an operational assumption
        -- resting on the single-lock-owning engine's own discipline
        -- (services/engine/keno_lock.py) -- the same reasoning
        -- admin_audit_log's append-only trigger and rounds' ledger
        -- balance trigger exist for elsewhere in this codebase: an
        -- invariant this load-bearing shouldn't depend on every future
        -- code path getting it right. A partial unique index on a
        -- constant expression is the standard Postgres idiom for
        -- "at most one row may have property X".
        CREATE UNIQUE INDEX ux_keno_rounds_single_betting_open ON keno_rounds ((true)) WHERE status = 'betting_open';

        CREATE OR REPLACE FUNCTION keno_prevent_drawn_numbers_mutation() RETURNS trigger AS $$
        BEGIN
          IF OLD.drawn_numbers IS NOT NULL AND NEW.drawn_numbers IS DISTINCT FROM OLD.drawn_numbers THEN
            RAISE EXCEPTION 'keno_rounds.drawn_numbers is immutable once set (round %)', OLD.id
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_keno_rounds_drawn_numbers_immutable
          BEFORE UPDATE ON keno_rounds
          FOR EACH ROW
          EXECUTE FUNCTION keno_prevent_drawn_numbers_mutation();
        """
    )

    # keno_round_events -- the per-round audit trail (Part 5.1: "every
    # transition ... written to audit"), distinct from admin_audit_log
    # (which covers admin config/RBAC actions, reused as-is -- see
    # services/sms/admin_queries.py for the identical precedent of a
    # feature reusing the platform's existing admin audit table for its
    # *admin* actions while keeping its own domain-event log separate).
    op.execute(
        """
        CREATE TABLE keno_round_events (
          id           bigserial PRIMARY KEY,
          round_id     bigint NOT NULL REFERENCES keno_rounds(id),
          from_status  text,
          to_status    text NOT NULL,
          worker_id    text,
          reason       text,
          created_at   timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_keno_round_events_round ON keno_round_events (round_id, created_at);
        """
    )

    # ------------------------------------------------------------------
    # keno_tickets / keno_ticket_selections. idempotency_key is the
    # client-supplied key from Part 6.1 ("Client sends an idempotency
    # key with every bet ... Enforce with a unique index, not an
    # existence check") -- UNIQUE NOT NULL here does exactly that.
    # Duplicate-picks-within-one-ticket is prevented structurally by
    # keno_ticket_selections' own primary key, not just application
    # validation (packages.core.keno.validate_picks already rejects it
    # too -- belt and suspenders, DB is the final backstop).
    # ------------------------------------------------------------------
    ticket_statuses_sql = ", ".join(f"'{s}'" for s in TICKET_STATUSES)
    op.execute(
        f"""
        CREATE TABLE keno_tickets (
          id                   bigserial PRIMARY KEY,
          round_id             bigint NOT NULL REFERENCES keno_rounds(id),
          user_id              bigint NOT NULL REFERENCES users(id),
          paytable_id          bigint NOT NULL REFERENCES keno_paytables(id),
          pick_count           int NOT NULL CHECK (pick_count BETWEEN 1 AND 10),
          stake                numeric(18,2) NOT NULL CHECK (stake > 0),
          -- This ticket's own contribution to the round's Part 7.3 CLT
          -- exposure accumulators (packages.core.keno_exposure's own
          -- module docstring has the full mean+variance reasoning),
          -- snapshotted at acceptance time so the per-user-share check
          -- can sum a user's existing tickets without re-deriving each
          -- one from a paytable that may have since changed for future
          -- rounds.
          expected_payout_contribution numeric(18,2) NOT NULL DEFAULT 0,
          payout_variance_contribution numeric(24,6) NOT NULL DEFAULT 0,
          status               text NOT NULL DEFAULT 'pending' CHECK (status IN ({ticket_statuses_sql})),
          matches              int,
          payout               numeric(18,2) CHECK (payout IS NULL OR payout >= 0),
          -- Base-game payout only. Any jackpot win (Part 8.3) is
          -- recorded separately here so the ticket row alone -- not just
          -- the ledger transaction -- shows the full outcome, per Part
          -- 9's "a completed round must be fully reconstructable from
          -- the database alone" requirement.
          jackpot_payout       numeric(18,2) CHECK (jackpot_payout IS NULL OR jackpot_payout >= 0),
          idempotency_key      text UNIQUE NOT NULL,
          stake_txn_id         bigint REFERENCES ledger_transactions(id),
          payout_txn_id        bigint REFERENCES ledger_transactions(id),
          created_at           timestamptz NOT NULL DEFAULT now(),
          settled_at           timestamptz
        );
        CREATE INDEX ix_keno_tickets_round ON keno_tickets (round_id, status);
        CREATE INDEX ix_keno_tickets_user ON keno_tickets (user_id, created_at DESC);
        -- Round-exposure/per-user-share checks (Part 3.3, 7.3) both
        -- query "this user's tickets in this still-open round" on the
        -- hot bet-placement path.
        CREATE INDEX ix_keno_tickets_user_round ON keno_tickets (round_id, user_id);
        """
    )

    op.execute(
        """
        CREATE TABLE keno_ticket_selections (
          ticket_id  bigint NOT NULL REFERENCES keno_tickets(id),
          number     smallint NOT NULL CHECK (number BETWEEN 1 AND 80),
          PRIMARY KEY (ticket_id, number)
        );
        """
    )

    # ------------------------------------------------------------------
    # Jackpot pool metadata (Part 8.3). The real money balance is the
    # ledger's own account_balances row for the keno_jackpot_pool system
    # account (account_id here) -- single source of truth, never
    # duplicated onto this table. This table only holds jackpot-specific
    # configuration/state the ledger has no natural column for.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE keno_jackpot_pool (
          id                       bigint PRIMARY KEY DEFAULT 1,
          account_id               bigint NOT NULL REFERENCES accounts(id),
          seed_amount              numeric(18,2) NOT NULL DEFAULT 0 CHECK (seed_amount >= 0),
          cap_amount               numeric(18,2) CHECK (cap_amount IS NULL OR cap_amount > 0),
          trigger_description      text NOT NULL DEFAULT 'All picks matched on a 5-spot ticket',
          overflow_pool_balance    numeric(18,2) NOT NULL DEFAULT 0 CHECK (overflow_pool_balance >= 0),
          updated_at               timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT keno_jackpot_pool_singleton CHECK (id = 1)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE keno_jackpot_pool")
    op.execute("DROP TABLE keno_ticket_selections")
    op.execute("DROP TABLE keno_tickets")
    op.execute("DROP TABLE keno_round_events")
    op.execute("DROP TRIGGER trg_keno_rounds_drawn_numbers_immutable ON keno_rounds")
    op.execute("DROP FUNCTION keno_prevent_drawn_numbers_mutation")
    op.execute("DROP TABLE keno_rounds")
    op.execute("DROP TABLE keno_tier_state")
    op.execute("DROP TABLE keno_risk_tiers")
    op.execute("DROP TABLE keno_paytables")
    op.execute("DROP TABLE keno_configs")

    op.execute("ALTER TABLE ledger_transactions DROP CONSTRAINT ledger_transactions_kind_check")
    original_txn_kinds_sql = ", ".join(
        f"'{k}'"
        for k in (
            "deposit",
            "withdrawal",
            "stake",
            "payout",
            "refund",
            "commission",
            "bonus_grant",
            "bonus_convert",
            "adjustment",
        )
    )
    op.execute(
        f"ALTER TABLE ledger_transactions ADD CONSTRAINT ledger_transactions_kind_check "
        f"CHECK (kind IN ({original_txn_kinds_sql}))"
    )

    op.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_kind_check")
    original_account_kinds_sql = ", ".join(
        f"'{k}'"
        for k in (
            "user_cash",
            "user_bonus",
            "user_locked",
            "house_revenue",
            "house_float",
            "pot_escrow",
            "provider_settlement",
            "promo_expense",
        )
    )
    op.execute(f"ALTER TABLE accounts ADD CONSTRAINT accounts_kind_check CHECK (kind IN ({original_account_kinds_sql}))")
