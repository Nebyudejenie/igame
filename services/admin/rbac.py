"""Role-based access control (spec section 33/35: least privilege).

Four roles, per the spec's own admin panel section: support, finance, ops,
superadmin. Permissions are additive per role -- there is no hidden "god
mode" bypass; superadmin is just the role every permission happens to be
assigned to, checked through the exact same function as everyone else.
"""

from __future__ import annotations

# Matches admin_users.role's own CHECK constraint exactly (migrations/
# versions/1c85c3d09653_admin_console.py's ADMIN_ROLES) -- kept here
# rather than imported from that migration (migrations are one-off,
# frozen-in-time scripts, never a runtime dependency for app code) so
# services/admin/queries.py's admin-account provisioning can validate a
# requested role before insert instead of surfacing a raw DB constraint
# violation as a 500.
KNOWN_ADMIN_ROLES = frozenset({"support", "finance", "ops", "superadmin"})

PERMISSIONS: dict[str, frozenset[str]] = {
    "dashboard:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "users:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "users:adjust_balance": frozenset({"finance", "superadmin"}),
    "users:suspend": frozenset({"ops", "finance", "superadmin"}),
    # Same roles as payments:approve, not users:suspend -- KYC level is a
    # financial-compliance control (it gates withdrawal size), not a
    # user-standing one, even though both end up as a field on the same
    # users row.
    "users:verify_kyc": frozenset({"finance", "superadmin"}),
    "rounds:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "rounds:void": frozenset({"ops", "superadmin"}),
    "rooms:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "rooms:manage": frozenset({"ops", "superadmin"}),
    # Narrower than rooms:manage and rounds:void on purpose: this
    # immediately halts an active, money-bearing round platform-wide for
    # one room and deactivates it in the same action -- a bigger single
    # blast radius than either existing permission covers on its own
    # (rooms:manage never touches an in-flight round's money; rounds:void
    # never also deactivates the room). Matches payments:configure's own
    # "single highest-leverage lever" reasoning -- superadmin-only.
    "rooms:emergency_stop": frozenset({"superadmin"}),
    "reports:view": frozenset({"finance", "superadmin"}),
    "audit:view": frozenset({"superadmin"}),
    "payments:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "payments:approve": frozenset({"finance", "superadmin"}),
    # Narrower than payments:view on purpose (spec section 97: least-
    # privilege raw-SMS access) -- support/ops can see a Telebirr evidence
    # row's status and amount like any other payment, but the raw SMS text
    # itself (payer's phone number fragment, exact wording) is finance/
    # superadmin only.
    "payments:view_raw_evidence": frozenset({"finance", "superadmin"}),
    # Narrower than payments:approve on purpose: approving one payment
    # bounds the blast radius of a bad call to that one request, but
    # toggling which rail is live or editing where manual deposits get
    # paid into changes behavior for every player at once -- the single
    # highest-leverage lever a compromised/rogue admin account could
    # pull (e.g. quietly redirecting the manual-deposit destination to a
    # personal account).
    "payments:configure": frozenset({"superadmin"}),
    # Same roles as rounds:void, not reports:view -- reading the risk
    # screen is an investigation tool for the roles who'd act on what it
    # shows (ops handles collusion/room abuse, finance handles payout
    # fraud), not a general reporting/analytics permission.
    "risk:view": frozenset({"ops", "finance", "superadmin"}),
    # Notification Center: deliberately does NOT include finance or
    # support -- messaging every player is an operational (ops) concern
    # (maintenance windows, game announcements), not a financial-review
    # or player-support one, and existing roles gain nothing here just
    # because the feature exists. Drafting/viewing is ops+superadmin;
    # actually causing a real send is superadmin-only, the same
    # "highest-leverage lever" reasoning payments:configure already uses
    # -- a real broadcast reaches every targeted player at once, the
    # same blast-radius shape as redirecting where deposits get paid.
    "notifications:view": frozenset({"ops", "superadmin"}),
    "notifications:create": frozenset({"ops", "superadmin"}),
    "notifications:send": frozenset({"superadmin"}),
    "notifications:schedule": frozenset({"superadmin"}),
    "notifications:cancel": frozenset({"superadmin"}),
    "notifications:templates_manage": frozenset({"ops", "superadmin"}),
    "notifications:view_analytics": frozenset({"ops", "superadmin"}),
    "notifications:view_delivery_details": frozenset({"ops", "superadmin"}),
    # The single highest-leverage lever in the whole system, higher even
    # than payments:configure: this is what decides who *holds* every
    # other permission in this table, including this one. superadmin-only
    # on purpose -- finance/ops/support managing their own or each
    # other's accounts would mean a compromised lower-privilege account
    # could mint itself a fresh, unaudited-by-anyone-above-it identity.
    "admin_users:manage": frozenset({"superadmin"}),
    # Editing player-facing bot text (menu button labels, message
    # templates) is an operational/UX concern, not a financial one -- same
    # roles as notifications:templates_manage, the closest precedent
    # (both edit text real players see, neither moves money or grants
    # access to anyone else's account).
    "bot_content:manage": frozenset({"ops", "superadmin"}),
    # A bonus/referral row's existence and status is low-sensitivity --
    # same breadth as payments:view.
    "bonuses:view": frozenset({"support", "finance", "ops", "superadmin"}),
    # Configuring reward amounts/wagering/eligibility is operational
    # rule-authoring, not itself a money-movement action -- same roles as
    # notifications:templates_manage/bot_content:manage.
    "bonuses:manage_rules": frozenset({"ops", "superadmin"}),
    # A manual, ad-hoc grant directly credits a specific player's wallet
    # -- exactly users:adjust_balance's own shape and roles.
    "bonuses:grant": frozenset({"finance", "superadmin"}),
    # Same investigative audience as risk:view -- this is the referral-
    # specific extension of that same screen's fraud-signal philosophy.
    "bonuses:view_fraud_signals": frozenset({"ops", "finance", "superadmin"}),
    # Same breadth as dashboard:view -- "is the bot reachable right now" is
    # informational operational status every role benefits from seeing
    # (support triaging a "the bot isn't replying" ticket needs this just
    # as much as ops does), and a getWebhookInfo() call is a read-only,
    # side-effect-free GET against Telegram's own API, not a control lever
    # that needs narrowing the way an actual configuration change would.
    "telegram:view_health": frozenset({"support", "finance", "ops", "superadmin"}),
    # Telegram Command Center (Phase 2): viewing the command registry
    # (which handlers exist, whether each is enabled, its own real
    # latency/error/usage figures) is the same kind of broad operational
    # visibility as telegram:view_health -- support needs to see "is
    # /deposit currently disabled" just as much as ops does when
    # triaging a ticket.
    "telegram:commands_view": frozenset({"support", "finance", "ops", "superadmin"}),
    # Enabling/disabling a command, or changing its cooldown/rate limit/
    # sort order, is an operational control lever that can take a real
    # feature away from every player at once -- same roles and reasoning
    # as notifications:templates_manage/bot_content:manage (an
    # operational, non-financial content/behavior change), narrower than
    # telegram:commands_view's own read-only breadth.
    "telegram:commands_manage": frozenset({"ops", "superadmin"}),

    # Enterprise SMS Control Plane (DECISIONS.md, 2026-09-07) -- a
    # genuinely separate product (services/sms/app.py, its own
    # sms.arada.fun subdomain) that reuses this exact PERMISSIONS dict and
    # has_permission() rather than growing a second authorization concept.
    "sms:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "sms:contacts:manage": frozenset({"ops", "superadmin"}),
    "sms:templates:manage": frozenset({"ops", "superadmin"}),
    "sms:campaigns:manage": frozenset({"ops", "superadmin"}),
    # Narrower than sms:campaigns:manage on purpose, same reasoning as
    # notifications:send -- creating/editing a draft campaign is
    # reversible and harmless; actually starting one sends real messages
    # to real people and cannot be undone once a node has dispatched a
    # message, the single highest-leverage action in this whole product.
    "sms:campaigns:approve": frozenset({"superadmin"}),
    "sms:nodes:manage": frozenset({"ops", "superadmin"}),
    "sms:compliance:manage": frozenset({"ops", "superadmin"}),

    # Simulated Players: admin-controlled bot accounts that join real
    # rooms during early launch so they don't feel empty. A bot's
    # existence/status/balance is low-sensitivity operational info -- same
    # breadth as payments:view/bonuses:view.
    "simulated_players:view": frozenset({"support", "finance", "ops", "superadmin"}),
    # Creating/configuring/starting/pausing/resetting one bot also funds
    # or re-funds its balance (a real house_float-backed ledger
    # transaction, see services/admin/simulated_players_queries.py) --
    # but unlike bonuses:grant, this money is capped at a fixed 5,000 ETB
    # seed, entirely house-funded, and fully reversible via Reset, so it
    # stays ops-reachable rather than finance-gated like a real per-player
    # bonus grant.
    "simulated_players:manage": frozenset({"ops", "superadmin"}),
    # Matches rooms:emergency_stop's own reasoning exactly: the single
    # highest-leverage lever in this screen (kills every bot platform-wide
    # at once, and doubles as the global on/off switch) -- superadmin only.
    "simulated_players:stop_all": frozenset({"superadmin"}),

    # Keno (build spec Part 15). Same breadth tiering as payments/bonuses:
    # broad read access, narrower write access, and the single highest
    # -leverage lever (activating a paytable/tier, or the global kill
    # switch) reserved for superadmin only -- an admin editing the live
    # paytable or reserve-tier ladder is exactly the kind of action a
    # compromised/rogue lower-privilege account could otherwise use to
    # quietly move the house edge or blow through the reserve.
    "keno:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "keno:manage": frozenset({"ops", "superadmin"}),
    "keno:configure": frozenset({"superadmin"}),

    # Platform announcement: a single scrolling banner every real player
    # sees in the Mini App. Same view/manage split and breadth as
    # simulated_players above: read-only visibility is low-sensitivity,
    # but writing it is public-facing copy every real player immediately
    # sees, so it's ops-gated rather than support-reachable (matches
    # bot_content:manage's own {ops, superadmin} for the same reason).
    "announcement:view": frozenset({"support", "finance", "ops", "superadmin"}),
    "announcement:manage": frozenset({"ops", "superadmin"}),
}


def has_permission(role: str, permission: str) -> bool:
    allowed_roles = PERMISSIONS.get(permission)
    if allowed_roles is None:
        raise ValueError(f"unknown permission: {permission!r}")
    return role in allowed_roles
