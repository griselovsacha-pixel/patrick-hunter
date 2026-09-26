"""
PatrickAccount — точка сборки одного Telegram-клиента.
"""
import asyncio
import datetime
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from config import (
    API_ID,
    API_HASH,
    TARGET_BOT,
    TIMEZONE_OFFSET,
    CHANNEL_SOURCE,
    CHANNEL_TOUCH_INTERVAL_SEC,
)
from priority_lock import PriorityLock
from tg_logger import TgLogger
from profile_stats import ProfileStats
from bio_manager import BioManager
from captcha import CaptchaSolver
from core import BotCore
from daily import DailyClaimer
from tasks import TaskRunner
from promo import PromoHunter
from clicker import Clicker
from supabase_db import SupabaseStore

logger = logging.getLogger(__name__)


class PatrickAccount:
    def __init__(
        self,
        label: str,
        session_string: str,
    ):
        self.label = label
        self.db = SupabaseStore(label)

        self.client = TelegramClient(
            StringSession(session_string),
            API_ID,
            API_HASH,
        )

        self.bot_lock = PriorityLock()

        self.tg_logger = TgLogger(
            self.client,
            label,
            self.db,
        )

        self.profile_stats = ProfileStats(
            self.tg_logger,
            self.db,
        )

        self.bio_manager = BioManager(
            self.client,
            self.tg_logger,
            label,
        )

        self.captcha = CaptchaSolver(
            self.client,
            self.tg_logger,
            label,
        )

        self.core = BotCore(
            self.client,
            self.tg_logger,
            self.captcha,
            self.bot_lock,
            label,
        )

        self.daily = DailyClaimer(
            self.client,
            self.core,
            self.tg_logger,
            self.profile_stats,
            self.bio_manager,
            label,
            self.db,
        )

        self.tasks = TaskRunner(
            self.client,
            self.core,
            self.tg_logger,
            self.profile_stats,
            label,
            self.db,
        )

        self.promo = PromoHunter(
            self.client,
            self.core,
            self.tg_logger,
            self.profile_stats,
            label,
            self.db,
        )

        self.clicker = Clicker(
            self.client,
            self.core,
            self.tg_logger,
            self.profile_stats,
            label,
            self.db,
        )

        self._background_tasks = []

    async def start(self):
        await self.client.start()

        self._register_bot_dm_handler()
        self.promo.register_channel_handler()

        await self.db.start()
        await self.db.set_connected(True)

        await self.client.catch_up()
        await self.tg_logger.setup()

        await self.bio_manager.ensure_referral_bio()

        # Сразу после запуска читаем реальный баланс из Профиля.
        # Дальше он обновляется после daily / tasks / promo /
        # каждых 10 успешных кликов.
        await self._refresh_real_balance()

        await self.tg_logger.log(
            "🤖 Бот запущен!\n"
            "📅 Ежедневка: по времени бота\n"
            "📋 Задания: каждые ~2ч15м-3ч45м\n"
            "🔔 Промокоды: мгновенный детект + быстрые повторы\n"
            "🎮 Кликер: рандом раз в день, окно 8:00-00:00\n"
            "💰 Баланс: читается из Профиля бота\n"
        )

        await self.db.log(
            "INFO",
            "startup",
            "PatrickAccount started",
        )

        self._background_tasks = [
            asyncio.create_task(
                self.daily.run_forever()
            ),
            asyncio.create_task(
                self.tasks.run_forever()
            ),
            asyncio.create_task(
                self.clicker.run_forever()
            ),
            asyncio.create_task(
                self._stats_cycle()
            ),
            asyncio.create_task(
                self._channel_touch_cycle()
            ),
        ]

    async def _refresh_real_balance(self):
        """Открывает Профиль и сохраняет реальный баланс."""
        try:
            async with self.core.bot_lock.acquire():
                msg = await self.core._open_unlocked()

                if not msg or not msg.buttons:
                    return False

                for row in msg.buttons:
                    for btn in row:
                        if any(
                            word in btn.text.lower()
                            for word in [
                                "профил",
                                "profile",
                            ]
                        ):
                            since = (
                                __import__(
                                    "core"
                                ).utcnow()
                            )

                            await btn.click()

                            resp = (
                                await self.core.wait_fresh(
                                    since,
                                    timeout=8,
                                )
                            )

                            if resp:
                                return await (
                                    self.profile_stats
                                    .update_from_profile_text(
                                        resp.text or ""
                                    )
                                )

                            return False

        except Exception as e:
            logger.warning(
                "[%s] Не удалось получить "
                "реальный баланс: %s",
                self.label,
                e,
            )

        return False

    async def _channel_touch_cycle(self):
        entity = None

        while True:
            try:
                if entity is None:
                    entity = await self.client.get_entity(
                        CHANNEL_SOURCE
                    )

                await self.core.touch_channel(
                    entity
                )

            except Exception as e:
                logger.warning(
                    "[%s] _channel_touch_cycle: %s",
                    self.label,
                    e,
                )

            await asyncio.sleep(
                CHANNEL_TOUCH_INTERVAL_SEC
            )

    def _register_bot_dm_handler(self):
        @self.client.on(
            events.NewMessage(
                chats=TARGET_BOT,
                incoming=True,
            )
        )
        @self.client.on(
            events.MessageEdited(
                chats=TARGET_BOT,
                incoming=True,
            )
        )
        async def handle_bot_dm(event):
            try:
                self.core.bot_msg_queue.put_nowait(
                    event.message
                )
            except asyncio.QueueFull:
                try:
                    self.core.bot_msg_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

                try:
                    self.core.bot_msg_queue.put_nowait(
                        event.message
                    )
                except asyncio.QueueFull:
                    pass

        self._bot_dm_handler = handle_bot_dm

    async def _stats_cycle(self):
        while True:
            now = (
                datetime.datetime.utcnow()
                + datetime.timedelta(
                    hours=TIMEZONE_OFFSET
                )
            )

            target = now.replace(
                hour=20,
                minute=0,
                second=0,
                microsecond=0,
            )

            if now >= target:
                target += datetime.timedelta(
                    days=1
                )

            await asyncio.sleep(
                (target - now).total_seconds()
            )

            report = (
                self.profile_stats.daily_report_text(
                    self.clicker.target_today
                    or "20-25"
                )
            )

            await self.tg_logger.log(
                report,
                pin=True,
            )

            self.profile_stats.reset_daily()

    async def stop(self):
        for task in self._background_tasks:
            task.cancel()

        await self.db.set_connected(False)
        await self.client.disconnect()
        await self.db.stop()
