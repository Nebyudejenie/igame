"""Tests for the bot-notification relay (packages/core/notifications.py's
notify_user() producer, services/bot/notification_relay.py's consumer):
real money-moving flows (a deposit webhook credit, a payout settling, an
admin rejecting a withdrawal) actually end up as a real message delivered
through a real Notifier, not just a Redis Stream entry nobody reads.
"""

import asyncio
from decimal import Decimal

import pytest

from packages.core.notifications import NOTIFICATIONS_STREAM, notify_user
from services.admin import queries as admin_queries
from services.bot import notification_relay
from services.bot.notifier import Notifier
from services.payments import deposits, manual, payout_worker, withdrawals
from tests.integration.conftest import fund_user, next_telegram_id
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_bot_handlers import make_bot
from tests.integration.test_payments_deposits import FakePaymentProvider
from tests.integration.test_payments_deposits import _webhook as build_deposit_webhook
from tests.integration.test_payout_worker import FakePayoutProvider


class _SlowThenFastNotifier:
    """A minimal stand-in for Notifier with fully controlled per-chat
    timing, so a test can isolate notification_relay.py's OWN batch
    -concurrency behavior from Notifier's own internal queue-scheduling
    behavior (already covered by tests/unit/test_notifier.py). Matches
    the same contract process_one() actually uses: send() returns a
    future that resolves only once this chat's delivery reaches a
    terminal outcome.
    """

    def __init__(self, *, slow_chat_id: int, slow_delay: float) -> None:
        self._slow_chat_id = slow_chat_id
        self._slow_delay = slow_delay
        self.sent_at: dict[int, float] = {}

    async def send(self, chat_id: int, text: str, **kwargs: object) -> asyncio.Future[None]:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()

        async def _resolve() -> None:
            if chat_id == self._slow_chat_id:
                await asyncio.sleep(self._slow_delay)
            self.sent_at[chat_id] = loop.time()
            done.set_result(None)

        asyncio.create_task(_resolve())
        return done


async def _make_notifier() -> tuple[Notifier, object]:
    bot, session = make_bot()
    notifier = Notifier(bot)
    notifier.start()
    return notifier, session


async def _register(conn, telegram_id: int) -> int:
    row = await conn.fetchrow(
        "INSERT INTO users (telegram_id, display_name) VALUES ($1, $2) RETURNING id",
        telegram_id,
        f"relay-test-{telegram_id}",
    )
    return row["id"]


async def test_notify_user_enqueues_onto_the_stream(pool, redis, conn):
    telegram_id = next_telegram_id()
    user_id = await _register(conn, telegram_id)

    await notify_user(pool, redis, user_id=user_id, key="notify.you_won", amount="10.00")

    length = await redis.xlen(NOTIFICATIONS_STREAM)
    assert length >= 1


async def test_notify_user_is_a_noop_for_an_unknown_user(pool, redis):
    before = await redis.xlen(NOTIFICATIONS_STREAM)
    await notify_user(pool, redis, user_id=999_999_999, key="notify.you_won", amount="10.00")
    after = await redis.xlen(NOTIFICATIONS_STREAM)
    assert after == before


async def test_relay_delivers_a_queued_notification(pool, redis, conn):
    notifier, session = await _make_notifier()
    try:
        telegram_id = next_telegram_id()
        user_id = await _register(conn, telegram_id)
        await notify_user(pool, redis, user_id=user_id, key="notify.you_won", amount="42.00")

        delivered = await notification_relay.process_next(
            pool, redis, notifier, consumer_name=f"test-{telegram_id}"
        )
        assert delivered is True

        await asyncio.sleep(0.1)  # Notifier.send() enqueues to its own async worker
        assert len(session.sent) == 1
        assert session.sent[0].chat_id == telegram_id
        assert "42.00" in session.sent[0].text
    finally:
        await notifier.stop()


async def test_relay_returns_false_when_stream_is_empty(pool, redis):
    notifier, _session = await _make_notifier()
    try:
        delivered = await notification_relay.process_next(pool, redis, notifier, consumer_name="empty-test")
        assert delivered is False
    finally:
        await notifier.stop()


async def test_relay_does_not_ack_before_the_notifier_actually_delivers(pool, redis, conn):
    """A code review pass caught that process_one() acked the stream entry
    right after notifier.send() returned -- which only means the message
    was enqueued into Notifier's own in-memory queue, not that it was
    actually delivered. If this relay process crashed before Notifier's
    background worker got around to sending it, the notification was
    lost outright: already acked, no redelivery path, and the in-memory
    queue itself is gone on crash too. Simulates that gap directly with a
    Notifier that's deliberately never .start()ed, so nothing ever drains
    its queue -- standing in for "the process died before reaching this
    message" -- and confirms process_one() never completes (and
    therefore never acks) within a short deadline.
    """
    telegram_id = next_telegram_id()
    user_id = await _register(conn, telegram_id)
    await notify_user(pool, redis, user_id=user_id, key="notify.you_won", amount="42.00")

    bot, session = make_bot()
    notifier = Notifier(bot)  # deliberately never started

    consumer_name = f"test-{telegram_id}"
    await notification_relay.ensure_group(redis)
    pending = await redis.xreadgroup(
        notification_relay.GROUP, consumer_name, {NOTIFICATIONS_STREAM: ">"}, count=1
    )
    entries = notification_relay._flatten(pending)
    assert entries
    msg_id, fields = entries[0]

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            notification_relay.process_one(pool, redis, notifier, msg_id=msg_id, fields=fields),
            timeout=0.3,
        )

    # Still unacked -- claimable by a fresh read of this consumer's own
    # pending list, the exact crash-recovery path a live relay process
    # would use on restart.
    still_pending = await redis.xreadgroup(
        notification_relay.GROUP, consumer_name, {NOTIFICATIONS_STREAM: "0"}, count=1
    )
    assert notification_relay._flatten(still_pending)
    assert len(session.sent) == 0


async def test_deposit_credit_notifies_the_depositor(pool, redis, conn):
    notifier, session = await _make_notifier()
    try:
        telegram_id = next_telegram_id()
        user_id = await _register(conn, telegram_id)
        provider = FakePaymentProvider()
        intent = await deposits.create_deposit_intent(
            pool,
            redis,
            provider,
            user_id=user_id,
            amount=Decimal("120.00"),
            phone_e164="+251911000000",
            return_url="https://app.test/return",
            callback_url="https://payments.test/webhooks/chapa",
            min_deposit=Decimal("1.00"),
            daily_cap=Decimal("1000000.00"),
        )
        headers, body = build_deposit_webhook(
            event_id=f"evt-{intent.our_ref}", our_ref=intent.our_ref, status="succeeded", amount="120.00"
        )
        outcome = await deposits.handle_webhook(pool, redis, provider, headers=headers, raw_body=body)
        assert outcome == "credited"

        delivered = await notification_relay.process_next(
            pool, redis, notifier, consumer_name=f"test-{telegram_id}"
        )
        assert delivered is True
        await asyncio.sleep(0.1)

        assert len(session.sent) == 1
        assert session.sent[0].chat_id == telegram_id
        assert "120.00" in session.sent[0].text
    finally:
        await notifier.stop()


async def test_successful_payout_notifies_the_withdrawer(pool, redis, conn):
    notifier, session = await _make_notifier()
    try:
        telegram_id = next_telegram_id()
        user_id = await _register(conn, telegram_id)
        await fund_user(conn, user_id, Decimal("500.00"))

        intent = await withdrawals.request_withdrawal(
            pool,
            redis,
            FakePayoutProvider(),
            user_id=user_id,
            amount=Decimal("200.00"),
            method_kind="telebirr",
            account_ref="0911223344",
            holder_name="Test Holder",
            min_withdraw=Decimal("10.00"),
            auto_approve_limit=Decimal("100000.00"),
            kyc_threshold=Decimal("100000.00"),
            chargeback_window_minutes=0,
            min_account_age_hours=0,
        )
        assert intent.status == withdrawals.STATUS_APPROVED

        outcome = await payout_worker.process_next(
            pool, redis, FakePayoutProvider(), consumer_name=f"payout-{telegram_id}"
        )
        assert outcome == "succeeded"

        delivered = await notification_relay.process_next(
            pool, redis, notifier, consumer_name=f"test-{telegram_id}"
        )
        assert delivered is True
        await asyncio.sleep(0.1)

        assert len(session.sent) == 1
        assert session.sent[0].chat_id == telegram_id
        assert "200.00" in session.sent[0].text
    finally:
        await notifier.stop()


async def test_admin_rejected_withdrawal_notifies_with_the_reason(pool, redis, conn):
    notifier, session = await _make_notifier()
    try:
        telegram_id = next_telegram_id()
        user_id = await _register(conn, telegram_id)
        await fund_user(conn, user_id, Decimal("500.00"))

        intent = await withdrawals.request_withdrawal(
            pool,
            redis,
            FakePayoutProvider(),
            user_id=user_id,
            amount=Decimal("300.00"),
            method_kind="telebirr",
            account_ref="0911223344",
            holder_name="Test Holder",
            min_withdraw=Decimal("10.00"),
            auto_approve_limit=Decimal("0.00"),  # forces review
            kyc_threshold=Decimal("1000000.00"),
            chargeback_window_minutes=0,
            min_account_age_hours=0,
        )
        assert intent.status == withdrawals.STATUS_REVIEW

        admin_id, *_ = await create_test_admin(pool)
        rejected = await admin_queries.reject_withdrawal_admin(
            pool,
            redis,
            admin_id=admin_id,
            payment_id=intent.payment_id,
            reason="account under review",
            ip_address=None,
        )
        assert rejected is True

        delivered = await notification_relay.process_next(
            pool, redis, notifier, consumer_name=f"test-{telegram_id}"
        )
        assert delivered is True
        await asyncio.sleep(0.1)

        assert len(session.sent) == 1
        assert "300.00" in session.sent[0].text
        assert "account under review" in session.sent[0].text
    finally:
        await notifier.stop()


async def test_admin_rejected_manual_deposit_notifies_with_the_reason(pool, redis, conn):
    # notify.manual_deposit_rejected is a key this session's own Stage 2
    # work introduced -- this proves it actually resolves through a real
    # Notifier delivery, not just that something landed on the Redis
    # stream (which every other manual-deposit test already checked).
    # Catches exactly the class of bug a missing/typo'd locale key would
    # be: t()'s own contract is to raise on an unresolved key, so a
    # broken key here would surface as this test failing loudly, not a
    # silently dropped notification in production.
    notifier, session = await _make_notifier()
    try:
        telegram_id = next_telegram_id()
        user_id = await _register(conn, telegram_id)
        destination_row = await conn.fetchrow(
            "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name) "
            "VALUES ('telebirr', '0911000000', 'Zemen Game PLC') RETURNING id"
        )
        intent = await manual.create_manual_deposit_request(
            pool,
            redis,
            user_id=user_id,
            amount=Decimal("75.00"),
            manual_destination_id=destination_row["id"],
            external_reference="FT-RELAY-TEST",
            receipt_telegram_file_id=None,
            min_deposit=Decimal("10.00"),
            daily_cap=Decimal("50000.00"),
        )

        admin_id, *_ = await create_test_admin(pool)
        rejected = await admin_queries.reject_manual_deposit_admin(
            pool, redis, admin_id=admin_id, payment_id=intent.payment_id,
            reason="reference did not match any statement line", ip_address=None,
        )
        assert rejected is True

        delivered = await notification_relay.process_next(
            pool, redis, notifier, consumer_name=f"test-{telegram_id}"
        )
        assert delivered is True
        await asyncio.sleep(0.1)

        assert len(session.sent) == 1
        assert "75.00" in session.sent[0].text
        assert "reference did not match any statement line" in session.sent[0].text
    finally:
        await notifier.stop()


async def test_relay_does_not_head_of_line_block_across_users(pool, redis, conn):
    """A code review pass caught that run_forever() awaited process_one()
    for each stream entry in turn, and process_one() awaits notifier.send()
    all the way to a terminal outcome -- which for a chat currently in a
    Telegram 429 backoff can mean several retry/sleep cycles (see
    Notifier._run()). One backed-off chat_id in a batch used to stall
    delivery to every other, unrelated user in that same batch for the
    whole backoff duration. Queues user A's notification first, then
    user B's right behind it, gives A's delivery a deliberately slow
    (1s) terminal outcome via a controllable stub notifier, and confirms
    B's delivery timestamp lands almost immediately rather than after
    A's -- which is exactly what the old sequential `for ... await
    process_one()` loop would have produced instead.
    """
    telegram_id_a = next_telegram_id()
    telegram_id_b = next_telegram_id()
    user_a = await _register(conn, telegram_id_a)
    user_b = await _register(conn, telegram_id_b)

    await notify_user(pool, redis, user_id=user_a, key="notify.you_won", amount="1.00")
    await notify_user(pool, redis, user_id=user_b, key="notify.you_won", amount="2.00")

    await notification_relay.ensure_group(redis)
    pending = await redis.xreadgroup(
        notification_relay.GROUP, f"test-{telegram_id_a}", {NOTIFICATIONS_STREAM: ">"}, count=10
    )
    entries = notification_relay._flatten(pending)
    assert len(entries) == 2

    notifier = _SlowThenFastNotifier(slow_chat_id=telegram_id_a, slow_delay=1.0)
    start = asyncio.get_running_loop().time()
    await notification_relay._process_batch(pool, redis, notifier, entries)

    b_delay = notifier.sent_at[telegram_id_b] - start
    a_delay = notifier.sent_at[telegram_id_a] - start
    assert b_delay < 0.3, f"B was delayed {b_delay:.2f}s -- stalled behind A's slow delivery"
    assert a_delay >= 1.0


async def test_relay_preserves_per_user_order_when_processing_concurrently(pool, redis, conn):
    """The head-of-line-blocking fix groups batch entries by telegram_id
    and runs each user's group concurrently with the others -- but a
    single user's own notifications must still arrive in their original
    stream order, not race each other, since naive full concurrency
    (one task per entry instead of one task per user) could let a later
    message's DB language lookup finish before an earlier one's.
    """
    telegram_id = next_telegram_id()
    user_id = await _register(conn, telegram_id)

    await notify_user(pool, redis, user_id=user_id, key="notify.you_won", amount="1.00")
    await notify_user(pool, redis, user_id=user_id, key="notify.you_won", amount="2.00")
    await notify_user(pool, redis, user_id=user_id, key="notify.you_won", amount="3.00")

    notifier, session = await _make_notifier()
    try:
        await notification_relay.ensure_group(redis)
        pending = await redis.xreadgroup(
            notification_relay.GROUP, f"test-{telegram_id}", {NOTIFICATIONS_STREAM: ">"}, count=10
        )
        entries = notification_relay._flatten(pending)
        assert len(entries) == 3

        await notification_relay._process_batch(pool, redis, notifier, entries)
        await asyncio.sleep(0.2)

        assert len(session.sent) == 3
        assert "1.00" in session.sent[0].text
        assert "2.00" in session.sent[1].text
        assert "3.00" in session.sent[2].text
    finally:
        await notifier.stop()


async def test_process_batch_survives_one_users_failure_and_still_delivers_the_rest(pool, redis, conn):
    # The real regression a code-review pass caught: packages/core/
    # db_pool.py's new bounded pool.acquire() turns a sustained-load pool
    # exhaustion into a real TimeoutError (previously an indefinite hang)
    # -- and _process_batch() runs every user's own _drain_one_user()
    # concurrently via asyncio.gather() with no per-user exception
    # isolation, so (gather()'s own default behavior, no
    # return_exceptions=True) one user's failure would cancel every OTHER
    # user's still-in-flight delivery in the same batch too, not just
    # skip the one that failed. Confirms the fix: user B's delivery still
    # lands even though user A's process_one() raises.
    telegram_id_a = next_telegram_id()
    telegram_id_b = next_telegram_id()
    user_a = await _register(conn, telegram_id_a)
    user_b = await _register(conn, telegram_id_b)

    await notify_user(pool, redis, user_id=user_a, key="notify.you_won", amount="1.00")
    await notify_user(pool, redis, user_id=user_b, key="notify.you_won", amount="2.00")

    await notification_relay.ensure_group(redis)
    pending = await redis.xreadgroup(
        notification_relay.GROUP, f"test-{telegram_id_a}", {NOTIFICATIONS_STREAM: ">"}, count=10
    )
    entries = notification_relay._flatten(pending)
    assert len(entries) == 2

    class _FlakyNotifier:
        def __init__(self, real: Notifier, *, fail_chat_id: int) -> None:
            self._real = real
            self._fail_chat_id = fail_chat_id

        async def send(self, chat_id: int, text: str, **kwargs: object) -> asyncio.Future[None]:
            if chat_id == self._fail_chat_id:
                raise RuntimeError("simulated pool exhaustion")
            return await self._real.send(chat_id, text, **kwargs)

    notifier, session = await _make_notifier()
    try:
        flaky = _FlakyNotifier(notifier, fail_chat_id=telegram_id_a)
        await notification_relay._process_batch(pool, redis, flaky, entries)
        await asyncio.sleep(0.2)

        assert len(session.sent) == 1
        assert "2.00" in session.sent[0].text

        # A's message was never acked -- still pending for this same
        # consumer, exactly the crash-redelivery guarantee this module's
        # own docstring promises, just without anything actually crashing.
        still_pending = notification_relay._flatten(
            await redis.xreadgroup(
                notification_relay.GROUP, f"test-{telegram_id_a}", {NOTIFICATIONS_STREAM: "0"}, count=10
            )
        )
        assert len(still_pending) == 1
        assert int(still_pending[0][1]["telegram_id"]) == telegram_id_a
    finally:
        await notifier.stop()
