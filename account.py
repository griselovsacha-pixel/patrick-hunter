"""
PatrickAccount — точка сборки одного Telegram-клиента.

Каждый аккаунт полностью самостоятелен: свой TelegramClient, свой bot_lock,
свой наблюдатель за каналом, свои охотники за промо/чеками, своя запись в
Supabase. Общего состояния между аккаунтами нет, кроме Hub (он только
пересылает «увидел код» соседям и ничего не блокирует).
"""
import asyncio
import random
import datetime
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import (
    API_ID,
    API_HASH,
    TARGET_BOT,
    TIMEZONE_OFFSET,
    WATCH_CHANNELS,
    TASK_START_STAGGER_SEC,
    TASK_INTERVAL_MIN_SEC,
    TASK_INTERVAL_MAX_SEC,
)
from priority_lock import PriorityLock
from tg_logger import TgLogger
from profile_stats import ProfileStats
from bio_manager import BioManager
from captcha import CaptchaSolver
from core import BotCore, utcnow
from daily import DailyClaimer
from tasks import TaskRunner
from promo import PromoHunter
from checks import CheckHunter
from proxy_check import ProxyMonitor
from watcher import ChannelWatcher
from clicker import Clicker
from supabase_db import SupabaseStore
from utils import find_button
from knowledge import Knowledge
from scheduler import JobScheduler
from bot_crawler import Crawler
from telethon import events

logger = logging.getLogger(__name__)


class PatrickAccount:
    def __init__(self, label: str, session_string: str, index: int = 0):
        self.label = label
        self.index = index
        self.ready = False  # True, когда клиент поднят и ловля промо/чеков запущена
        self.db = SupabaseStore(label)

        from proxy_conf import proxy_for
        digits = "".join(ch for ch in label if ch.isdigit())
        self.number = int(digits or "1")
        self.client = TelegramClient(
            StringSession(session_string),
            API_ID,
            API_HASH,
            proxy=proxy_for(self.number),
        )
        # Статус прокси для админки (проверка раз в 5 мин + ручная кнопка)
        self.proxy = ProxyMonitor(self.number, self.client, label)

        self.bot_lock = PriorityLock()
        self.tg_logger = TgLogger(self.client, label, self.db)
        self.profile_stats = ProfileStats(self.tg_logger, self.db)
        self.bio_manager = BioManager(self.client, self.tg_logger, label)
        self.captcha = CaptchaSolver(self.client, self.tg_logger, label)
        self.core = BotCore(self.client, self.tg_logger, self.captcha, self.bot_lock, label)

        self.daily = DailyClaimer(
            self.client, self.core, self.tg_logger, self.profile_stats,
            self.bio_manager, label, self.db,
        )
        self.tasks = TaskRunner(
            self.client, self.core, self.tg_logger, self.profile_stats, label, self.db,
        )
        # Сдвиг первого цикла заданий: аккаунты не должны одновременно
        # выполнять самую тяжёлую часть работы.
        self.tasks.start_offset = index * TASK_START_STAGGER_SEC

        self.promo = PromoHunter(
            self.client, self.core, self.tg_logger, self.profile_stats, label, self.db,
        )
        self.checks = CheckHunter(self.client, self.core, self.tg_logger, label, self.db)
        self.promo.checks = self.checks

        self.watcher = ChannelWatcher(
            self.client, label, WATCH_CHANNELS, self.promo.on_post, self.tg_logger,
        )
        self.clicker = Clicker(
            self.client, self.core, self.tg_logger, self.profile_stats, label, self.db,
        )

        # «Обучение»: авто-сбор экранов/кнопок/алертов бота для админки
        self.knowledge = Knowledge(self.db, label)
        self.core.knowledge = self.knowledge
        # Обход кнопок бота — только на первом аккаунте (второй не нужен)
        self.crawler = Crawler(self.core, self.tg_logger, self.knowledge, label) if index == 0 else None

        # Расписание задач в БД (переживает рестарт) + ручной запуск из админки
        self.sched = JobScheduler(self.db, label, index)
        for _m in (self.daily, self.tasks, self.clicker):
            _m.sched = self.sched
        if self.crawler:
            self.crawler.sched = self.sched
            self.sched.handlers["crawler"] = self.crawler.request
        self.sched.handlers["daily"] = self.daily.request
        self.sched.handlers["tasks"] = lambda: self.tasks.request_run("админка", force=True)
        self.sched.handlers["clicker"] = self.clicker.request

        # Кликер просит внеочередной цикл заданий, когда бот пишет «выполни задание»
        self.clicker.on_gate = self.tasks.request_run

        self._background_tasks = []
        self._bot_dm_handler = None
        self._warmup_task = None

    # ------------------------------------------------------------------
    async def start(self):
        await self.client.start()

        self._register_bot_dm_handler()

        await self.db.start()
        await self.db.set_connected(True)

        await self.tg_logger.setup()
        self.tg_logger.start_worker()

        # Ловля промо/чеков поднимается РАНЬШЕ всего остального (био, баланс,
        # приветственный лог) — это самая срочная функция аккаунта.
        self.checks.install()  # событийное ожидание ответов бота-магазина
        await self.watcher.start()
        self.ready = True
        # Прогрев: резолвим бота-магазин заранее, чтобы первый чек не терял время
        self._warmup_task = asyncio.create_task(self.checks.warmup())
        try:
            await self.client.catch_up()
        except Exception as e:
            logger.warning("[%s] catch_up: %s", self.label, e)

        await self.bio_manager.ensure_referral_bio()
        # Баланс на старте не читаем (лишнее открытие бота при каждом рестарте): его обновят
        # ежедневка/задания, а здесь — один отложенный запрос с разбросом.
        self._background_tasks_delayed = asyncio.create_task(self._delayed_balance())

        await self.tg_logger.log(
            "🤖 Бот запущен!\n"
            "📅 Ежедневка: по времени бота\n"
            f"📋 Задания: каждые ~{TASK_INTERVAL_MIN_SEC // 60}-{TASK_INTERVAL_MAX_SEC // 60} мин и по запросу кликера\n"
            "🔔 Промокоды и чеки: события + опрос канала каждые несколько секунд\n"
            "🎮 Кликер: рандом раз в день, окно 8:00-00:00 (план дня хранится в БД)\n"
            "🗓 После рестарта задачи идут по расписанию из БД, без залпа при старте\n"
            "💰 Баланс: читается из Профиля бота\n"
        )
        await self.db.log("INFO", "startup", "PatrickAccount started")

        self._background_tasks = [
            asyncio.create_task(self._supervised("daily", self.daily.run_forever)),
            asyncio.create_task(self._supervised("tasks", self.tasks.run_forever)),
            asyncio.create_task(self._supervised("clicker", self.clicker.run_forever)),
            asyncio.create_task(self._supervised("stats", self._stats_cycle)),
            asyncio.create_task(self._supervised("watchdog", self._watchdog)),
            asyncio.create_task(self._supervised("proxy", self.proxy.run_forever)),
        ]
        if self.crawler:
            self._background_tasks.append(
                asyncio.create_task(self._supervised("crawler", self.crawler.run_forever)))

    async def _supervised(self, name: str, factory):
        """Фоновая задача перезапускается, если упала: раньше одно
        необработанное исключение молча убивало, например, кликер до
        следующего деплоя."""
        backoff = 15
        while True:
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[%s] задача %s упала: %s", self.label, name, e)
                await self.tg_logger.log(f"Задача {name} упала, перезапуск через {backoff}с", error=repr(e)[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 600)

    async def _watchdog(self):
        """Если соединение пропало и Telethon сам не поднял — поднимаем."""
        misses = 0
        while True:
            await asyncio.sleep(60)
            if self.client.is_connected():
                misses = 0
                continue
            misses += 1
            if misses >= 2:
                logger.warning("[%s] нет соединения, переподключаюсь", self.label)
                try:
                    await self.client.connect()
                    misses = 0
                except Exception as e:
                    logger.warning("[%s] переподключение не удалось: %s", self.label, e)

    async def _delayed_balance(self):
        try:
            await asyncio.sleep(random.uniform(300, 600) + self.index * 180)
            await self._refresh_real_balance()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[%s] delayed balance: %s", self.label, e)

    async def _refresh_real_balance(self):
        """Открывает Профиль и сохраняет реальный баланс."""
        try:
            async with self.core.bot_lock.acquire():
                msg = await self.core._open_unlocked()
                btn = find_button(msg, ["профил", "profile"])
                if not btn:
                    return False
                since = utcnow()
                await self.core.click_button(btn)
                resp = await self.core.wait_fresh(since, timeout=8)
                if resp:
                    return await self.profile_stats.update_from_profile_text(resp.text or "")
                return False
        except Exception as e:
            logger.warning("[%s] Не удалось получить реальный баланс: %s", self.label, e)
        return False

    def _register_bot_dm_handler(self):
        async def handle_bot_dm(event):
            self.knowledge.on_message(event.message)
            # Бот сам присылает «Возможно, тебе уже доступны новые задания» / «Патрик проснулся…
            # новые задания уже лежат под подушкой» — это сигнал запустить цикл заданий
            # (request_run сам не чаще раза в TASK_WAKE_MIN_GAP_SEC и не во время цикла).
            if isinstance(event, events.NewMessage.Event):
                low = (event.message.text or "").lower()
                if "новые задания" in low and ("доступны" in low or "под подушкой" in low):
                    if self.tasks.request_run("бот сообщил о новых заданиях"):
                        await self.tg_logger.log("📣 Бот сообщил о новых заданиях — запускаю цикл заданий")
            q = self.core.bot_msg_queue
            try:
                q.put_nowait(event.message)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event.message)
                except asyncio.QueueFull:
                    pass

        self.client.add_event_handler(handle_bot_dm, events.NewMessage(chats=TARGET_BOT, incoming=True))
        self.client.add_event_handler(handle_bot_dm, events.MessageEdited(chats=TARGET_BOT, incoming=True))
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
        self.ready = False
        try:
            self._background_tasks_delayed.cancel()
        except Exception:
            pass
        if self._warmup_task is not None:
            self._warmup_task.cancel()
            self._warmup_task = None
        self.checks.uninstall()
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks = []
        try:
            await self.watcher.stop()
        except Exception:
            pass
        if self._bot_dm_handler is not None:
            try:
                self.client.remove_event_handler(self._bot_dm_handler)
            except Exception:
                pass
            self._bot_dm_handler = None
        try:
            await self.tg_logger.stop_worker()
        except Exception:
            pass
        await self.db.set_connected(False)
        try:
            await self.client.disconnect()
        finally:
            await self.db.stop()
