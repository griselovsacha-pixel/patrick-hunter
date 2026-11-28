"""
PatrickAccount — точка сборки одного Telegram-клиента.

Каждый аккаунт полностью самостоятелен: свой TelegramClient, свой bot_lock,
свой наблюдатель за каналом, свои охотники за промо/чеками, своя запись в
Supabase. Общего состояния между аккаунтами нет, кроме Hub (он только
пересылает «увидел код» соседям и ничего не блокирует).
"""
import asyncio
import os
import random
import time
import datetime
import logging

from telethon import TelegramClient
from telethon import errors as tg_errors
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
from proxy_manager import ProxyManager
from lease import SessionLease
from watcher import ChannelWatcher
from clicker import Clicker
from supabase_db import SupabaseStore
from utils import find_button
from knowledge import Knowledge
from scheduler import JobScheduler
from protection import AccountProtection
from bot_crawler import Crawler
from telethon import events

logger = logging.getLogger(__name__)


class SessionDeadError(Exception):
    """Сессия аккаунта недействительна (разлогинена/отозвана) — повторять старт бессмысленно."""


# Ошибки, при которых аккаунт уже не оживить без новой сессии: бан, отзыв, разлогин, а также
# «ключ использован из двух мест» (AuthKeyDuplicatedError — двойной запуск одной сессии).
FATAL_AUTH_ERRORS = tuple(
    getattr(tg_errors, name)
    for name in (
        "AuthKeyError", "AuthKeyUnregisteredError", "AuthKeyDuplicatedError", "SessionRevokedError",
        "SessionExpiredError", "UserDeactivatedError", "UserDeactivatedBanError",
    )
    if hasattr(tg_errors, name)
) + (SessionDeadError,)


class PatrickAccount:
    def __init__(self, label: str, session_string: str, index: int = 0,
                 api_id: int | None = None, api_hash: str | None = None, device: dict | None = None,
                 lang: str | None = None, center_id: int | None = None):
        self.label = label
        self.center_id = center_id   # id в «Центре аккаунтов» (None — аккаунт из переменных окружения)
        self.index = index
        self.ready = False  # True, когда клиент поднят и ловля промо/чеков запущена
        self.dead = None    # причина, если сессия мертва (бан/отзыв/двойной запуск) — аккаунт остановлен
        self.me = None
        self.db = SupabaseStore(label)

        digits = "".join(ch for ch in label if ch.isdigit())
        self.number = int(digits or "1")
        # Прокси НЕ задаём здесь: маршрут (прокси / IP Render) выбирает ProxyManager перед
        # первым подключением — по настройкам из админки (см. start()).
        # Отпечаток устройства. По умолчанию Telethon представляется как «Linux-контейнер» с одной и той же
        # версией приложения у ВСЕХ аккаунтов — признак фермы. Можно задать для каждого аккаунта отдельно
        # (ACCOUNT_<N>_DEVICE_MODEL / SYSTEM_VERSION / APP_VERSION / LANG_CODE). Значения должны быть
        # СТАБИЛЬНЫМИ: менять их у живой сессии нельзя — смена устройства сама по себе подозрительна.
        device_kw = {}
        # Аккаунты из центра приходят со своим устройством (оно сохранено при входе) — это базовые значения,
        # переменные ACCOUNT_<N>_* ниже их переопределяют.
        for k in ("device_model", "system_version", "app_version"):
            if (device or {}).get(k):
                device_kw[k] = device[k]
        if lang:
            device_kw["lang_code"] = device_kw["system_lang_code"] = lang
        for env_key, kw in (("DEVICE_MODEL", "device_model"), ("SYSTEM_VERSION", "system_version"),
                            ("APP_VERSION", "app_version"), ("LANG_CODE", "lang_code"),
                            ("SYSTEM_LANG_CODE", "system_lang_code")):
            val = (os.getenv(f"ACCOUNT_{self.number}_{env_key}") or "").strip()
            if val:
                device_kw[kw] = val
        self.client = TelegramClient(
            StringSession(session_string),
            api_id or API_ID,
            api_hash or API_HASH,
            **device_kw,
        )

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

        # Единый слой защиты аккаунта (Account Protection Manager): общий КД, лимитер, бюджеты, FloodWait,
        # детектор зависаний. ВСЕ модули ходят в Telegram через него (core.api / core.click_raw / core.delay).
        self.protection = AccountProtection(label, self.sched, self.bot_lock, self.tg_logger)
        for _m in (self.core, self.bio_manager, self.watcher, self.tg_logger):
            _m.protection = self.protection
        self.captcha.core = self.core

        # Кликер просит внеочередной цикл заданий, когда бот пишет «выполни задание»
        self.clicker.on_gate = self.tasks.request_run

        # Маршрут в Telegram (прокси / IP Render): режим и адрес меняются из админки, при аварии
        # прокси — автоматический переход на IP Render (proxy_manager.py)
        self.proxy = ProxyManager(self.number, self.client, label, self.sched, self.tg_logger,
                                  is_live=lambda: self.ready)
        # Защита от двойного запуска сессии (деплой Render: старый и новый инстанс одновременно)
        self.lease = SessionLease(self.db, label, on_lost=self.fatal)

        self._background_tasks = []
        self._bot_dm_handler = None
        self._warmup_task = None

    # ------------------------------------------------------------------
    async def start(self):
        # 1) БД и настройки аккаунта — ДО подключения: от них зависят режим прокси и прогрев
        await self.db.start()
        await self.sched.load()
        await self.protection.load()   # КД/режим восстановления/бюджеты переживают рестарт

        # 2) не подключаемся, пока ту же сессию держит другой инстанс (старый деплой)
        await self.lease.acquire()

        # 3) маршрут: прокси или IP Render (мёртвый прокси на старте не должен блокировать аккаунт)
        route = await self.proxy.initial_route()
        logger.info("[%s] маршрут при старте: %s (режим: %s)", self.label,
                    "прокси" if route == "proxy" else "IP Render", self.proxy.mode)

        # 4) подключение. Вместо client.start() (он при мёртвой сессии просит ввести телефон через
        # input() и вешает/роняет старт) — connect + явная проверка авторизации.
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise SessionDeadError("сессия не авторизована")
        try:
            self.me = await self.client.get_me()
        except FATAL_AUTH_ERRORS:
            raise
        except Exception as e:
            logger.warning("[%s] get_me: %s", self.label, e)
        if self.me is not None:
            # личная реф. ссылка = t.me/<бот>?start=<ID аккаунта> (раньше была зашита в config)
            self.bio_manager.user_id = self.me.id

        self._register_bot_dm_handler()
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

        if self.sched.lurking():
            # ультра-прогрев, «затишье»: профиль не трогаем, бота не открываем — только читаем канал
            logger.info("[%s] ультра-прогрев: затишье ещё %.1f дн. — био и баланс не трогаю", self.label,
                        self.sched.lurk_remaining() / 86400)
        else:
            await self.bio_manager.ensure_referral_bio()
        # Баланс на старте не читаем (лишнее открытие бота при каждом рестарте): его обновят
        # ежедневка/задания, а здесь — один отложенный запрос с разбросом.
        self._background_tasks_delayed = asyncio.create_task(self._delayed_balance())

        wi = self.sched.warmup_info()
        await self.tg_logger.log(
            "🤖 Бот запущен!\n"
            f"🌐 Маршрут: {'прокси' if self.proxy.route == 'proxy' else 'IP Render'} (режим: {self.proxy.mode})\n"
            + ((f"🧊 УЛЬТРА-прогрев: день {wi['day']} из {wi['days']}, фаза «{'затишье' if wi['phase'] == 'lurk' else 'рост'}», "
                f"нагрузка {round(wi['factor'] * 100)}%, строгий прокси\n") if (wi["active"] and wi["mode"] == "ultra") else
               (f"🌱 Прогрев: день {wi['day']} из {wi['days']}, нагрузка {round(wi['factor'] * 100)}%\n" if wi["active"] else ""))
            + f"🛡 Защита аккаунта: {self.protection.snapshot()['state_title']}\n"
            + "📅 Ежедневка: по времени бота\n"
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
            asyncio.create_task(self._supervised("protection", self.protection.run_forever)),
        ]
        if self.crawler:
            self._background_tasks.append(
                asyncio.create_task(self._supervised("crawler", self.crawler.run_forever)))

    async def fatal(self, reason: str):
        """Аккаунт больше работать не может (бан, отозванная сессия, двойной запуск): останавливаем
        всю его активность и помечаем в админке, вместо бесконечных повторов с ошибками."""
        if self.dead:
            return
        self.dead = reason
        self.ready = False
        self.protection.set_auth_error(reason)
        logger.error("❌ [%s] аккаунт остановлен: %s", self.label, reason)
        try:
            await self.tg_logger.log(f"❌ Аккаунт остановлен: {reason}", error="нужна новая сессия или проверка аккаунта")
        except Exception:
            pass
        asyncio.get_running_loop().create_task(self.stop())

    async def _supervised(self, name: str, factory):
        """Фоновая задача перезапускается, если упала: раньше одно
        необработанное исключение молча убивало, например, кликер до
        следующего деплоя. Пауза между перезапусками растёт, но сбрасывается, если задача
        до падения проработала >10 минут (раньше она только росла до 10 минут навсегда)."""
        backoff = 15
        while True:
            began = time.monotonic()
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except FATAL_AUTH_ERRORS as e:
                await self.fatal(f"{name}: {type(e).__name__}")
                return
            except Exception as e:
                logger.exception("[%s] задача %s упала: %s", self.label, name, e)
                await self.tg_logger.log(f"Задача {name} упала, перезапуск через {backoff}с", error=repr(e)[:200])
                if time.monotonic() - began > 600:
                    backoff = 15
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 600)

    async def _watchdog(self):
        """Сторож соединения. Если клиент отвалился и Telethon сам не поднял — поднимаем; если идём
        через прокси и он, похоже, мёртв — переходим на IP Render (кроме СТРОГОГО режима ультра-прогрева).
        Раньше сторож видел только флаг is_connected(): «подключён, но молчит» (зависший сокет) он не замечал,
        а get_me() без таймаута мог повесить его самого. Теперь раз в ~5 минут — ping get_me() с таймаутом;
        два провала подряд = принудительное переподключение. Раз в ~10 минут — проверка живости аккаунта
        (бан/отзыв сессии иначе молча глотаются внутри циклов заданий)."""
        misses = 0
        ping_fails = 0
        ticks = 0
        while True:
            await asyncio.sleep(30)
            ticks += 1
            if self.client.is_connected():
                misses = 0
                self.protection.set_offline(False)
                if ticks % 10 == 0:
                    try:
                        await asyncio.wait_for(self.client.get_me(), 30)
                        ping_fails = 0
                    except FATAL_AUTH_ERRORS as e:
                        await self.fatal(f"{type(e).__name__} (проверка аккаунта)")
                        return
                    except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                        ping_fails += 1
                        logger.warning("[%s] ping get_me не ответил (%d/2): %r", self.label, ping_fails, e)
                        if ping_fails >= 2:
                            ping_fails = 0
                            await self._force_reconnect("соединение «живое», но не отвечает")
                    except Exception as e:
                        logger.debug("[%s] проверка аккаунта: %s", self.label, e)
                continue
            misses += 1
            self.protection.set_offline(True)
            if misses >= 3:
                logger.warning("[%s] нет соединения ~%dс, переподключаюсь", self.label, misses * 30)
                try:
                    if await self.proxy.on_client_down():
                        misses = 0
                        continue
                    await self.client.connect()
                    try:
                        await self.client.catch_up()
                    except Exception:
                        pass
                    misses = 0
                except FATAL_AUTH_ERRORS as e:
                    await self.fatal(f"{type(e).__name__} при переподключении")
                    return
                except Exception as e:
                    logger.warning("[%s] переподключение не удалось: %s", self.label, e)

    async def _force_reconnect(self, why: str):
        logger.warning("[%s] принудительное переподключение: %s", self.label, why)
        try:
            await asyncio.wait_for(self.client.disconnect(), 15)
        except Exception as e:
            logger.debug("[%s] disconnect: %r", self.label, e)
        try:
            await asyncio.wait_for(self.client.connect(), 30)
            try:
                await self.client.catch_up()
            except Exception:
                pass
        except FATAL_AUTH_ERRORS as e:
            await self.fatal(f"{type(e).__name__} при переподключении")
        except Exception as e:
            logger.warning("[%s] принудительное переподключение не удалось: %r", self.label, e)

    async def _delayed_balance(self):
        try:
            await asyncio.sleep(random.uniform(300, 600) + self.index * 180)
            if self.sched.block_seconds("daily") > 0 or self.protection.cooldown_left() > 0:
                return   # затишье/тихие часы ультра-прогрева или КД аккаунта — баланс обновят позже задания
            await self.protection.run_op("balance", self._refresh_real_balance, stall_sec=300)
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
            # аренду освобождаем ПОСЛЕ отключения клиента и ДО закрытия БД: новый инстанс
            # подключится сразу, но не раньше, чем мы отпустили сессию
            try:
                await self.lease.release()
            finally:
                await self.db.stop()
