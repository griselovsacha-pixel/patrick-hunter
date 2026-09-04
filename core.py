"""
BotCore — низкоуровневый слой общения с TARGET_BOT, общий для всех фич
(ежедневка/задания/промо/кликер). Открытие меню, ожидание ответа, обход
капчи и "гейта спонсоров", вступление в каналы. Фичи (daily.py, tasks.py,
promo.py, clicker.py) держат ссылку на core и пользуются этими методами,
а не лезут в Telethon напрямую.
"""
import re
import asyncio
import datetime
import logging

from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, StartBotRequest
from telethon.tl.functions.folders import EditPeerFoldersRequest
from telethon.tl.types import InputFolderPeer
from telethon.errors import UserAlreadyParticipantError, InviteHashInvalidError

from config import TARGET_BOT, CONFIRM_WORDS
from utils import looks_like_sponsor_gate, human_delay
from antiban import safe_call

logger = logging.getLogger(__name__)


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
        self.bot_msg_queue: asyncio.Queue = asyncio.Queue(maxsize=50)

    # --- Очередь свежих сообщений от бота ---
    def _drain_queue(self):
        while not self.bot_msg_queue.empty():
            try:
                self.bot_msg_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def wait_fresh(self, since: datetime.datetime, timeout: float = 10.0):
        """Мгновенное (событийное) ожидание ответа бота. `since` отсекает
        старые/застрявшие сообщения — берём только то, что пришло ПОСЛЕ
        нашего действия. Важно: бот часто РЕДАКТИРУЕТ одно и то же
        сообщение вместо отправки нового — .date при этом не меняется,
        поэтому сравниваем по (edit_date or date)."""
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
            if effective_date > since and (msg.text or msg.buttons):
                return msg
        return await self.poll_response()

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

    async def safe_join(self, target: str):
        try:
            if target.startswith('+'):
                await safe_call(lambda: self.client(ImportChatInviteRequest(target[1:])), action="join_invite")
            else:
                await safe_call(lambda: self.client(JoinChannelRequest(target)), action="join_channel")
            await human_delay(2, 4)
            await self.archive_chat(target)
            await human_delay(2, 4)
        except UserAlreadyParticipantError:
            await self.archive_chat(target)
        except InviteHashInvalidError:
            logger.warning(f"⚠️ [{self.label}] Невалидный инвайт: {target}")
        except Exception as e:
            logger.error(f"[{self.label}] Ошибка вступления {target}: {e}")

    async def handle_startapp(self, url: str) -> bool:
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
            await human_delay(25, 35)
            return True
        except Exception as e:
            logger.error(f"⚠️ [{self.label}] StartApp ошибка: {e}")
            return False

    # --- Гейт "подпишись на спонсоров, чтобы продолжить фарм" ---
    async def handle_sponsor_gate(self, msg):
        await self.tg_logger.log(f"🚧 Гейт спонсоров: {(msg.text or '')[:150]}")

        confirm_btn = None
        joined = 0
        for row in msg.buttons:
            for btn in row:
                btn_text = btn.text.lower()
                if any(w in btn_text for w in CONFIRM_WORDS):
                    confirm_btn = btn
                    continue
                url = getattr(btn, 'url', None)
                if url:
                    tme_match = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                    if tme_match:
                        target = tme_match.group(1)
                        if not target.lower().endswith('bot'):
                            await self.safe_join(target)
                            joined += 1

        if not confirm_btn:
            await self.tg_logger.log("Гейт спонсоров: кнопка подтверждения не найдена", error="нет кнопки")
            return msg

        await human_delay(4, 8)
        since = utcnow()
        try:
            await confirm_btn.click()
            await self.tg_logger.log(f"✅ Гейт спонсоров пройден, подписался на {joined} каналов")
        except Exception as e:
            await self.tg_logger.log("Гейт спонсоров: ошибка подтверждения", error=str(e))
            return msg

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
