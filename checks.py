"""
CheckHunter — чеки и deep-link'и из постов канала.

В прошлой версии обработки чеков в коде НЕ БЫЛО вообще: пост парсился только
на текстовый промокод. Здесь — универсальный обработчик ссылок вида
    t.me/<бот>?start=<payload>
(CryptoBot/@send, xRocket, Wallet и ссылки на самого TARGET_BOT). Быстрый
путь: один StartBotRequest (без диалога /start → меню → ...), затем читаем
ответ бота и, если он просит подписаться на спонсоров, вступаем и жмём
«проверить».

Список ботов и правила payload — в config.CHECK_BOTS и _PAYLOAD_RULES ниже.
Если у тебя чеки другого формата — пришли пример поста, подстрою правила.
"""
import asyncio
import datetime
import logging
from contextlib import AsyncExitStack

from telethon.tl.functions.messages import StartBotRequest

from config import CHECK_BOTS, TARGET_BOT, PROMO_KEYWORDS, PROMO_REJECT_KEYWORDS
from utils import BoundedSet, find_button, human_delay, parse_tme_target
from antiban import safe_call, TooManyFloodWaitsError
from core import utcnow

logger = logging.getLogger(__name__)

CHECK_WORDS = ["чек", "cheque", "voucher", "ваучер"]
CHECK_MAX_ATTEMPTS = 3
CHECK_REPLY_TIMEOUT_SEC = 8

REJECT_EXTRA = [
    "уже активирован", "уже получен", "недействител", "не существует",
    "отозван", "already", "no longer", "doesn't exist",
]
SUCCESS_HINTS = [
    "получил", "получено", "зачислен", "начислен", "активирован",
    "успешно", "received", "you got", "claimed", "success",
]

# Какие payload считаем чеком (чтобы не стартовать бота по реферальным
# ссылкам и счетам-инвойсам). Для ботов вне этого словаря берём любой
# нечисловой payload.
_PAYLOAD_RULES = {
    "send": lambda p: not p.startswith(("r-", "IV", "iv")),
    "cryptobot": lambda p: not p.startswith(("r-", "IV", "iv")),
    "wallet": lambda p: p.startswith("C-"),
    "xrocket": lambda p: p.startswith(("t_", "mc_")),
    "tonrocketbot": lambda p: p.startswith(("t_", "mc_")),
}


def classify_reply(text: str) -> str:
    low = (text or "").lower()
    if any(w in low for w in PROMO_REJECT_KEYWORDS + REJECT_EXTRA):
        return "rejected"
    if any(w in low for w in SUCCESS_HINTS):
        return "ok"
    return "unknown"


class CheckHunter:
    def __init__(self, client, core, tg_logger, label: str, db=None):
        self.client = client
        self.core = core
        self.tg_logger = tg_logger
        self.label = label
        self.db = db

        self.done = BoundedSet(200)
        self.in_progress = set()
        self._entities = {}
        self._tasks = set()

    # ------------------------------------------------------------------
    @staticmethod
    def is_check_link(bot: str, payload: str, post_text_lower: str) -> bool:
        bot = bot.lower()
        if bot == TARGET_BOT.lower():
            # у TARGET_BOT чисто числовой payload — это реферальный id
            if payload.isdigit():
                return False
            return any(w in post_text_lower for w in PROMO_KEYWORDS + CHECK_WORDS)
        if bot not in CHECK_BOTS:
            return False
        rule = _PAYLOAD_RULES.get(bot)
        if rule:
            return rule(payload)
        return not payload.isdigit()

    def submit(self, bot: str, payload: str):
        key = (bot.lower(), payload)
        if key in self.done or key in self.in_progress:
            return
        task = asyncio.create_task(self.claim(bot.lower(), payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    async def claim(self, bot: str, payload: str):
        key = (bot, payload)
        if key in self.done or key in self.in_progress:
            return
        self.in_progress.add(key)
        await self.tg_logger.log(f"🧾 Чек: стартую @{bot} с payload `{payload[:40]}`")
        outcome, reply_text = "retry", ""
        try:
            for attempt in range(1, CHECK_MAX_ATTEMPTS + 1):
                try:
                    outcome, reply_text = await asyncio.wait_for(self._try_claim(bot, payload), 40)
                except TooManyFloodWaitsError as e:
                    await self.tg_logger.log(f"Чек @{bot}: слишком много FloodWait", error=str(e))
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"⚠️ [{self.label}] Чек @{bot} попытка #{attempt}: {e!r}")
                    outcome = "retry"
                if outcome != "retry":
                    break
                await human_delay(0.4, 1.0)

            if outcome == "ok":
                self.done.add(key)
                await self.tg_logger.log(f"✅ Чек @{bot} активирован\n📩 {reply_text[:150]}")
            elif outcome == "rejected":
                self.done.add(key)
                await self.tg_logger.log(f"Чек @{bot} отклонён: {reply_text[:150]}", error="отклонён")
            elif outcome == "unknown":
                self.done.add(key)
                await self.tg_logger.log(
                    f"🧾 Чек @{bot}: payload отправлен, ответ бота не распознан (успех не подтверждён)\n📩 {reply_text[:200]}")
            else:
                await self.tg_logger.log(f"Чек @{bot}: бот не ответил за {CHECK_MAX_ATTEMPTS} попытки", error="нет ответа")
        finally:
            self.in_progress.discard(key)

    async def _entity(self, bot: str):
        ent = self._entities.get(bot)
        if ent is None:
            ent = await self.client.get_input_entity(bot)
            self._entities[bot] = ent
        return ent

    async def _wait_reply(self, entity, since, timeout: float):
        floor = since.replace(microsecond=0) - datetime.timedelta(seconds=1)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            async for m in self.client.iter_messages(entity, limit=3):
                if m.out:
                    continue
                if (m.edit_date or m.date) >= floor and (m.text or m.buttons):
                    return m
                break  # самое новое входящее — старое, ждём
            await asyncio.sleep(0.6)
        return None

    async def _try_claim(self, bot: str, payload: str):
        entity = await self._entity(bot)
        is_target = bot == TARGET_BOT.lower()
        async with AsyncExitStack() as stack:
            if is_target:
                # тот же чат/состояние, что у заданий и промо — идём через лок
                await stack.enter_async_context(self.core.bot_lock.acquire(high_priority=True))

            since = utcnow()
            await safe_call(
                lambda: self.client(StartBotRequest(bot=entity, peer=entity, start_param=payload)),
                action="start_check",
            )
            reply = await self._wait_reply(entity, since, CHECK_REPLY_TIMEOUT_SEC)
            if reply is None:
                return "retry", ""

            text = reply.text or ""
            outcome = classify_reply(text)
            if outcome == "unknown" and reply.buttons:
                # Возможно, нужно подписаться на спонсоров и нажать «проверить».
                text, outcome = await self._satisfy_buttons(entity, reply, text, outcome)
            return outcome, text

    async def _satisfy_buttons(self, entity, reply, text, outcome):
        """-> (text, outcome) после вступления в каналы и нажатия «проверить»."""
        joined = False
        for row in reply.buttons:
            for b in row:
                url = getattr(b, "url", None)
                if url:
                    t = parse_tme_target(url)
                    if t and t["kind"] in ("channel", "invite"):
                        status = await self.core.safe_join(t["target"])
                        joined = joined or status in ("ok", "already")
        confirm = find_button(reply, ["подпис", "провер", "check", "готов", "я подписал"])
        if not (joined and confirm):
            return text, outcome

        await human_delay(1, 2)
        since = utcnow()
        alert_text, _ = await self.core.click_button(confirm)
        if alert_text:
            return alert_text, classify_reply(alert_text)
        new_reply = await self._wait_reply(entity, since, CHECK_REPLY_TIMEOUT_SEC)
        if new_reply is None:
            return text, outcome
        new_text = new_reply.text or ""
        return new_text, classify_reply(new_text)
