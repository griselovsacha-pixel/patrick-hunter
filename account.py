"""
PatrickAccount — точка сборки: один Telegram-клиент + все фичи-компоненты.
Если добавляешь новую фичу — создаёшь новый файл (как daily.py/tasks.py/...)
и подключаешь её здесь. Сам PatrickAccount не содержит бизнес-логики.
"""
import asyncio
import datetime
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from config import API_ID, API_HASH, TARGET_BOT, TIMEZONE_OFFSET, CHANNEL_SOURCE, CHANNEL_TOUCH_INTERVAL_SEC
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
    def __init__(self, label: str, session_string: str):
        self.label = label
        self.db = SupabaseStore(label)
        self.client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

        self.bot_lock = PriorityLock()
        self.tg_logger = TgLogger(self.client, label, self.db)
        self.profile_stats = ProfileStats(self.tg_logger)
        self.bio_manager = BioManager(self.client, self.tg_logger, label)
        self.captcha = CaptchaSolver(self.client, self.tg_logger, label)
        self.core = BotCore(self.client, self.tg_logger, self.captcha, self.bot_lock, label)

        self.daily = DailyClaimer(self.client, self.core, self.tg_logger, self.profile_stats, self.bio_manager, label, self.db)
        self.tasks = TaskRunner(self.client, self.core, self.tg_logger, self.profile_stats, label, self.db)
        self.promo = PromoHunter(self.client, self.core, self.tg_logger, self.profile_stats, label, self.db)
        self.clicker = Clicker(self.client, self.core, self.tg_logger, self.profile_stats, label, self.db)

        self._background_tasks = []

    async def start(self):
        await self.client.start()
        self._register_bot_dm_handler()
        self.promo.register_channel_handler()
        await self.db.start()
        await self.db.set_connected(True)
        # catch_up() — забрать все обновления, накопившиеся, пока клиент
        # не был подключён (после рестарта на Render). Без этого события
        # по каналам могут не прийти вообще, пока их что-то не "разбудит".
        await self.client.catch_up()
        await self.tg_logger.setup()

        # Ссылка в био ставится ОДИН РАЗ осознанно здесь, при старте —
        # а не реактивно по детекту всплывашки внутри daily.py. Раньше
        # реактивный вариант ложно срабатывал на каждый рестарт (см. историю
        # багов в daily.py) и выглядело так, будто бот "не понимает", что
        # ссылка уже стоит, и переставляет её заново. Сама операция
        # идемпотентна (тот же .about не меняет состояние на сервере), так
        # что от лишнего вызова на каждом рестарте вреда нет — но теперь
        # это явное, понятное действие "настраиваемся при старте", а не
        # ложный сигнал "нашёл проблему и чиню".
        await self.bio_manager.ensure_referral_bio()

        await self.tg_logger.log(
            "🤖 Бот запущен!\n"
            "📅 Ежедневка: по времени бота (ссылка в био ставится при старте)\n"
            "📋 Задания: каждые ~2ч15м-3ч45м\n"
            "🔔 Промокоды: мгновенный детект + приоритетный лок + быстрые повторы\n"
            f"🎮 Кликер: рандом раз в день, окно 8:00-00:00\n"
            "📊 Статистика: каждый день в 20:00 (реальные данные бота)\n"
        )

        await self.db.log("INFO", "startup", "PatrickAccount started")

        self._background_tasks = [
            asyncio.create_task(self.daily.run_forever()),
            asyncio.create_task(self.tasks.run_forever()),
            asyncio.create_task(self.clicker.run_forever()),
            asyncio.create_task(self._stats_cycle()),
            asyncio.create_task(self._channel_touch_cycle()),
        ]

    async def _channel_touch_cycle(self):
        """КРИТИЧЕСКИ ВАЖНО для скорости промо.

        Обнаружили на практике: промокод в канале детектился только ПОСЛЕ
        того, как аккаунт вручную открывали в приложении Telegram — то есть
        реальное время реакции было не "секунды", а "пока кто-то не зайдёт
        в чат руками". Это известная особенность MTProto: обновления по
        каналам иногда перестают приходить в реальном времени, пока клиент
        явно не запросит что-то по этому чату (устаревает internal pts).
        Простое решение — периодически самим "трогать" канал через лёгкий
        get_messages(limit=1), это не даёт Telegram "забыть" держать канал
        живым для нашего соединения."""
        entity = None
        while True:
            try:
                if entity is None:
                    entity = await self.client.get_entity(CHANNEL_SOURCE)
                await self.core.touch_channel(entity)
            except Exception as e:
                logger.warning(f"[{self.label}] _channel_touch_cycle: {e}")
            await asyncio.sleep(CHANNEL_TOUCH_INTERVAL_SEC)

    def _register_bot_dm_handler(self):
        """Личные сообщения от TARGET_BOT -> очередь core.bot_msg_queue,
        чтобы core.wait_fresh() просыпался мгновенно, а не опрашивал историю."""
        @self.client.on(events.NewMessage(chats=TARGET_BOT, incoming=True))
        @self.client.on(events.MessageEdited(chats=TARGET_BOT, incoming=True))
        async def handle_bot_dm(event):
            try:
                self.core.bot_msg_queue.put_nowait(event.message)
            except asyncio.QueueFull:
                try:
                    self.core.bot_msg_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    self.core.bot_msg_queue.put_nowait(event.message)
                except asyncio.QueueFull:
                    pass

        self._bot_dm_handler = handle_bot_dm

    async def _stats_cycle(self):
        while True:
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
            target = now.replace(hour=20, minute=0, second=0, microsecond=0)
            if now >= target:
                target += datetime.timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

            report = self.profile_stats.daily_report_text(self.clicker.target_today or "20-25")
            await self.tg_logger.log(report, pin=True)
            self.profile_stats.reset_daily()

    async def stop(self):
        for t in self._background_tasks:
            t.cancel()
        await self.db.set_connected(False)
        await self.client.disconnect()
        await self.db.stop()

