"""
BotCore — низкоуровневый слой общения с TARGET_BOT, общий для всех фич
(ежедневка/задания/промо/кликер). Открытие меню, ожидание ответа, обход
капчи и "гейта спонсоров", вступление в каналы. Фичи (daily.py, tasks.py,
promo.py, clicker.py) держат ссылку на core и пользуются этими методами,
а не лезут в Telethon напрямую.
"""
import re
import random
import asyncio
import datetime
import logging

from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, StartBotRequest
from telethon.tl.functions.folders import EditPeerFoldersRequest
from telethon.tl.types import InputFolderPeer
import telethon.errors as _tgerr

from config import TARGET_BOT, CONFIRM_WORDS, MINIAPP_REAL_OPEN
from miniapp import MiniAppRunner, parse_app_link
from utils import (
    looks_like_sponsor_gate, human_delay, is_webapp_button, parse_tme_target,
    classify_task_alert,
)
from antiban import safe_call

logger = logging.getLogger(__name__)


def _err(name: str):
    """Класс ошибки Telethon по имени; если в этой версии его нет — заглушка
    (чтобы отсутствие одного класса не роняло импорт всего приложения)."""
    return getattr(_tgerr, name, type(name, (Exception,), {}))


UserAlreadyParticipantError = _err("UserAlreadyParticipantError")
InviteHashInvalidError = _err("InviteHashInvalidError")
InviteHashExpiredError = _err("InviteHashExpiredError")
InviteRequestSentError = _err("InviteRequestSentError")
ChannelsTooMuchError = _err("ChannelsTooMuchError")
UsernameNotOccupiedError = _err("UsernameNotOccupiedError")
UsernameInvalidError = _err("UsernameInvalidError")
ChannelPrivateError = _err("ChannelPrivateError")


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


class BotCore:
    def __init__(self, client, tg_logger, captcha_solver, bot_lock, label: str):
        self.client = client
        self.tg_logger = tg_logger
        self.captcha = captcha_solver
        self.bot_lock = bot_lock
        self.label = label

        # Очередь входящих сообщений от TARGET_BOT — для мгновенного
        # (событийного) ожидания ответа вместо опроса истории раз в 1.5с.
        # Заполняется из account.py через register_bot_dm_handler().
        self.bot_msg_queue: asyncio.Queue = asyncio.Queue(maxsize=30)

        # Связка кликер <-> задания: кликер просит внеочередной цикл заданий
        # (tasks_wanted), а после цикла задания сообщают об этом (tasks_cycle_done).
        self.tasks_wanted = asyncio.Event()
        self.tasks_cycle_done = asyncio.Event()

        # Настоящий запуск мини-аппов (WebView + keepalive + обработчики).
        self.miniapp = MiniAppRunner(client, self, tg_logger, label)

    # --- Очередь свежих сообщений от бота ---
    def _drain_queue(self):
        while not self.bot_msg_queue.empty():
            try:
                self.bot_msg_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    # --- Клик по кнопке с чтением ВСПЛЫВАЮЩЕГО ОКНА (callback-алерт) ---
    async def click_button(self, btn):
        """КРИТИЧЕСКИ ВАЖНЫЙ метод — читай перед тем как трогать логику
        ежедневки/кликера.

        Когда бот отвечает на нажатие inline-кнопки через Telegram-алерт
        (всплывающее окно вида "Бонус можно получить раз в 24 часа!
        Осталось: 6ч 51мин" — см. присланные скриншоты), этот текст
        НИКОГДА не приходит как обычное/отредактированное сообщение в чат.
        Это отдельный ответ на сам callback-запрос (Telethon возвращает его
        напрямую из btn.click() как BotCallbackAnswer.message).

        Раньше весь код по всему проекту делал `await btn.click()` и
        выбрасывал результат, а потом ждал новое сообщение через
        wait_fresh() — которое для алертов никогда не придёт. wait_fresh()
        уходил в таймаут и фолбэчился на poll_response(), который возвращал
        СТАРОЕ сообщение (например, тот же экран Профиля с его постоянным
        статичным текстом "⁉️ Как получить бонус..."), и код принимал это
        старое сообщение за новый ответ бота. Именно так родился ложный
        детект "Всплывашка про ссылку в био" при каждом клике ежедневки —
        это было не реальное всплывающее окно, а старый текст профиля.

        Возвращает (alert_text: str|None, raw_answer). alert_text — текст
        из всплывающего окна, если бот ответил именно так; иначе None
        (тогда нужно идти по обычному пути — ждать новое/отредактированное
        сообщение через wait_fresh, как раньше)."""
        try:
            answer = await btn.click()
        except Exception as e:
            logger.warning(f"⚠️ [{self.label}] Ошибка клика: {e}")
            return None, None
        alert_text = getattr(answer, 'message', None) if answer is not None else None
        return alert_text, answer

    async def touch_channel(self, entity):
        """Периодически 'трогаем' канал через get_messages — известная
        проблема Telethon/MTProto: обновления по каналам иногда перестают
        приходить в реальном времени (устаревший pts), пока клиент не
        сделает явный запрос к этому чату. Раньше это и объясняло, почему
        промокод детектился только после того, как ты сам открывал канал
        руками в приложении — Telegram присылал накопившиеся апдейты
        именно в этот момент."""
        try:
            await self.client.get_messages(entity, limit=1)
        except Exception as e:
            logger.warning(f"⚠️ [{self.label}] touch_channel ошибка: {e}")

    # Telegram отдаёт message.date с точностью до СЕКУНДЫ, а наш `since` —
    # с микросекундами. Раньше сравнивали `date > since`, и ответ бота,
    # пришедший за 0.3с, "из прошлой секунды" отбрасывался как старый:
    # wait_fresh молча висел до таймаута (8с) + ещё до 12с в poll_response
    # на КАЖДОМ шаге. Для промокода (4-5 шагов) это десятки секунд — код к
    # тому времени уже исчерпан. Теперь сравниваем с `since`, округлённым
    # вниз до секунды, минус небольшой допуск на расхождение часов.
    CLOCK_TOLERANCE_SEC = 1.0

    def _floor(self, since: datetime.datetime) -> datetime.datetime:
        return since.replace(microsecond=0) - datetime.timedelta(seconds=self.CLOCK_TOLERANCE_SEC)

    async def wait_fresh(self, since: datetime.datetime, timeout: float = 10.0,
                         *, predicate=None, fallback: bool = True):
        """Событийное ожидание ответа бота.

        predicate(msg) -> bool: принимать только подходящие сообщения
        (например, экран с кнопкой «Промокод»); остальные пропускаем.
        fallback=False: после таймаута не уходить в медленный опрос
        истории (poll_response до ~12с), а сделать один быстрый запрос
        (poll_once). Критичные по скорости пути (промокод) используют его.
        Бот часто РЕДАКТИРУЕТ одно сообщение — поэтому смотрим на
        (edit_date or date)."""
        floor = self._floor(since)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(self.bot_msg_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if msg.out:
                continue  # страховка: наши исходящие сюда попадать не должны
            effective_date = msg.edit_date or msg.date
            if effective_date >= floor and (msg.text or msg.buttons):
                if predicate is None or predicate(msg):
                    return msg
        if fallback:
            return await self.poll_response()
        return await self.poll_once(floor, predicate)

    async def poll_once(self, floor: datetime.datetime, predicate=None):
        """Один быстрый запрос истории: самое свежее подходящее сообщение
        бота новее `floor`, либо None."""
        try:
            async for msg in self.client.iter_messages(TARGET_BOT, limit=5):
                if msg.out:
                    continue
                effective_date = msg.edit_date or msg.date
                if effective_date < floor:
                    return None  # дальше только старее
                if (msg.text or msg.buttons) and (predicate is None or predicate(msg)):
                    return msg
        except Exception as e:
            logger.warning(f"⚠️ [{self.label}] poll_once ошибка: {e}")
        return None

    async def poll_response(self, max_age: int = 120, attempts: int = 8):
        """Медленный фолбэк опросом истории чата — на случай пропуска события."""
        for _ in range(attempts):
            now = utcnow()
            async for msg in self.client.iter_messages(TARGET_BOT, limit=8):
                if msg.out:
                    continue
                effective_date = msg.edit_date or msg.date
                age = (now - effective_date).total_seconds()
                if age < max_age and (msg.text or msg.buttons):
                    return msg
            await asyncio.sleep(1.5)
        async for msg in self.client.iter_messages(TARGET_BOT, limit=5):
            if not msg.out:
                return msg
        return None

    async def yield_sleep(self, min_sec: float, max_sec: float):
        """Человеческая пауза ВНУТРИ удерживаемого bot_lock, во время которой
        лок отдаётся (промокод может пройти), а потом берётся обратно.
        Вызывать только из кода, уже держащего bot_lock."""
        await self.bot_lock.yield_for(random.uniform(min_sec, max_sec))

    # --- Архивирование и join ---
    async def archive_chat(self, entity):
        try:
            peer = await self.client.get_input_entity(entity)
            await safe_call(
                lambda: self.client(EditPeerFoldersRequest(
                    folder_peers=[InputFolderPeer(peer=peer, folder_id=1)]
                )),
                action="archive_chat",
            )
        except Exception as e:
            logger.warning(f"⚠️ [{self.label}] Не удалось архивировать {entity}: {e}")

    async def safe_join(self, target: str) -> str:
        """Вступить в канал/чат. Возвращает статус, а не молча глотает ошибки
        (раньше провал вступления всё равно логировался как «подписался»):
        'ok' | 'already' | 'request' (нужно одобрение) | 'expired' | 'invalid' |
        'limit' (слишком много каналов) | 'error'."""
        status = "ok"
        try:
            if target.startswith('+'):
                await safe_call(lambda: self.client(ImportChatInviteRequest(target[1:])), action="join_invite")
            else:
                await safe_call(lambda: self.client(JoinChannelRequest(target)), action="join_channel")
        except UserAlreadyParticipantError:
            status = "already"
        except InviteRequestSentError:
            status = "request"
        except InviteHashExpiredError:
            status = "expired"
        except (InviteHashInvalidError, UsernameNotOccupiedError, UsernameInvalidError, ChannelPrivateError):
            status = "invalid"
        except ChannelsTooMuchError:
            status = "limit"
        except Exception as e:
            status = "error"
            logger.error(f"[{self.label}] Ошибка вступления {target}: {e}")

        if status != "ok":
            logger.warning(f"⚠️ [{self.label}] Вступление {target}: {status}")
        if status in ("ok", "already"):
            if status == "ok":
                await human_delay(2, 4)
            await self.archive_chat(target)
            if status == "ok":
                await human_delay(2, 4)
        return status

    async def start_bot(self, target: str, payload=None) -> bool:
        """Запустить стороннего бота как по ссылке t.me/bot?start=payload
        (раньше payload терялся и слался голый /start). Сам TARGET_BOT не
        трогаем: он уже запущен, а лишний /start сбивает диалог заданий."""
        if target.lower() == TARGET_BOT.lower():
            return True
        cmd = f"/start {payload}" if payload else "/start"
        try:
            await safe_call(lambda: self.client.send_message(target, cmd), action="task_start_bot")
            await self.archive_chat(target)
            return True
        except Exception as e:
            logger.warning(f"⚠️ [{self.label}] Запуск бота @{target}: {e}")
            return False

    async def handle_startapp(self, url: str) -> bool:
        """Открыть мини-апп по ссылке t.me/bot[/app]?startapp=... .
        Сначала — по-настоящему (WebView, см. miniapp.py); если не вышло —
        старый путь (StartBotRequest + пауза), чтобы не потерять задание."""
        if MINIAPP_REAL_OPEN:
            try:
                res = await self.miniapp.run_link(url)
                if res == "ok":
                    parsed = parse_app_link(url)
                    if parsed:
                        await self.archive_chat(parsed[0])
                    return True
                if res == "blocked":
                    return False  # Cloudflare-проверка: старый путь не пробуем, задание пропустится
            except Exception as e:
                logger.error(f"⚠️ [{self.label}] WebView мини-аппа не открылся, пробую старый путь: {e}")
        return await self._legacy_startapp(url)

    async def handle_webview_button(self, msg, btn) -> bool:
        """Кнопка web_view / simple_web_view прямо в сообщении бота."""
        try:
            return await self.miniapp.run_button(msg, btn) == "ok"
        except Exception as e:
            logger.error(f"⚠️ [{self.label}] WebView-кнопка: {e}")
            return False

    async def _legacy_startapp(self, url: str) -> bool:
        try:
            match = re.search(r't\.me/([a-zA-Z0-9_]+)(?:/\w+)?\?startapp=([a-zA-Z0-9_]+)', url)
            if not match:
                return False
            bot_username, start_param = match.group(1), match.group(2)
            bot = await self.client.get_entity(bot_username)
            await safe_call(
                lambda: self.client(StartBotRequest(bot=bot, peer=bot, start_param=start_param)),
                action="startapp",
            )
            await self.archive_chat(bot_username)
            # Долгая пауза мини-аппа: отдаём лок, чтобы за это время мог пройти промокод.
            await self.yield_sleep(25, 35)
            return True
        except Exception as e:
            logger.error(f"⚠️ [{self.label}] StartApp ошибка: {e}")
            return False

    # --- Гейт "подпишись на спонсоров, чтобы продолжить фарм" ---
    async def handle_sponsor_gate(self, msg):
        await self.tg_logger.log(f"🚧 Гейт спонсоров: {(msg.text or '')[:150]}")

        confirm_btn = None
        joined, bots, unsupported = 0, 0, 0
        failed = []
        for row in msg.buttons:
            for btn in row:
                btn_text = btn.text.lower()
                if any(w in btn_text for w in CONFIRM_WORDS):
                    confirm_btn = btn
                    continue
                if is_webapp_button(btn):
                    unsupported += 1  # мини-апп спонсора (Flyer и т.п.) — без обработчика не засчитать
                    continue
                url = getattr(btn, 'url', None)
                if not url:
                    continue
                t = parse_tme_target(url)
                if not t or t["kind"] == "app":
                    unsupported += 1
                elif t["kind"] in ("channel", "invite"):
                    status = await self.safe_join(t["target"])
                    if status in ("ok", "already"):
                        joined += 1
                    else:
                        failed.append(f"{t['target'][:14]}:{status}")
                elif t["kind"] == "bot":
                    if await self.start_bot(t["target"], t["start"]):
                        bots += 1

        if not confirm_btn:
            await self.tg_logger.log("Гейт спонсоров: кнопка подтверждения не найдена", error="нет кнопки")
            return msg

        summary = f"каналов {joined}, ботов {bots}"
        if failed:
            summary += f", не удалось {len(failed)} ({', '.join(failed)[:80]})"
        if unsupported:
            summary += f", ссылок/мини-аппов без обработки {unsupported}"

        await human_delay(4, 8)
        since = utcnow()
        alert, _ = await self.click_button(confirm_btn)
        verdict = classify_task_alert(alert) if alert else "unknown"
        if verdict == "fail":
            await self.tg_logger.log(
                f"⚠️ Гейт спонсоров НЕ пройден ({summary}): {alert[:100]}", error="гейт не пройден")
        elif verdict == "ok":
            await self.tg_logger.log(f"✅ Гейт спонсоров пройден ({summary}): {alert[:80]}")
        else:
            await self.tg_logger.log(f"🚧 Гейт спонсоров: кнопка нажата ({summary}), ответ бота не распознан"
                                     + (f": {alert[:80]}" if alert else ""))

        return await self.wait_fresh(since, timeout=8)

    # --- Открытие бота (капча/гейт разруливаются автоматически) ---
    async def _open_unlocked(self):
        """Открыть бота БЕЗ захвата bot_lock.

        ⚠️ ВЫЗЫВАТЬ ТОЛЬКО когда bot_lock УЖЕ взят снаружи (ты внутри
        `async with self.bot_lock.acquire(...):`). PriorityLock НЕ
        реентерабелен — если вызвать отсюда open_bot() (который сам берёт
        лок) или использовать эту функцию без внешнего лока вообще, при
        совпадении с любым другим держателем лока будет либо гонка за
        состояние бота, либо (если по ошибке дёрнуть open_bot() изнутри
        уже удержанного лока) самодедлок навсегда — см. историю багов
        в promo.py/clicker.py, оба чинились именно этим."""
        self._drain_queue()  # всё, что в очереди сейчас, — устаревшее
        since = utcnow()
        await safe_call(lambda: self.client.send_message(TARGET_BOT, '/start'), action="send_start")
        msg = await self.wait_fresh(since, timeout=10)
        if not msg:
            return None

        for _ in range(4):
            if not msg:
                return None
            text_lower = (msg.text or "").lower()

            if "проверка" in text_lower:
                since = utcnow()
                solved = await self.captcha.solve_if_needed(msg)
                if not solved:
                    return None
                msg = await self.wait_fresh(since, timeout=8)
                continue

            if looks_like_sponsor_gate(msg):
                msg = await self.handle_sponsor_gate(msg)
                continue

            break

        return msg

    async def open_bot(self):
        """Открыть бота как отдельную атомарную операцию (сама берёт лок).

        ⚠️ НИКОГДА не вызывать эту функцию из кода, который уже держит
        bot_lock (то есть уже внутри `async with self.bot_lock.acquire(...)`).
        Лок не реентерабелен — это гарантированный самодедлок. Если лок уже
        взят снаружи, используй _open_unlocked() напрямую."""
        async with self.bot_lock.acquire():
            return await self._open_unlocked()
