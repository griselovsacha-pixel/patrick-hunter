"""
CheckHunter — чеки из постов канала («Чек на 111 ⭐»).

Как выглядит чек в канале (формат не меняется):
    пост «Чек на 111 ⭐» «через @patrickshoprobot»
    + inline URL-кнопка «Получить 111 ⭐» → https://t.me/patrickshoprobot?start=<payload>

Почему раньше не ловилось:
  1. patrickshoprobot не был в списке CHECK_BOTS → ссылка вообще не считалась чеком;
  2. после /start бот-магазин показывает сообщение с кнопкой «Получить», а код её
     не нажимал (искал только «подписаться/проверить»).

Что делает теперь (цель — нажать «Получить» максимально быстро):
  • один StartBotRequest (как переход по ссылке), без диалога /start → меню;
  • ответ бота ловится СОБЫТИЕМ (раньше — опрос истории раз в 0.6с), с опросом
    только как страховкой;
  • кнопка «Получить» нажимается сразу, как пришёл ответ; если бот требует
    подписку на спонсоров — вступаем, жмём «проверить» и снова «Получить»;
  • username бота-магазина резолвится заранее при старте аккаунта (warmup);
  • чек забирает ОДИН аккаунт — тот, кто первым увидел пост (см. hub.claim_check).
    Если он не смог дойти до бота — чек уходит второму аккаунту.

Список ботов и правила payload — в config.CHECK_BOTS и _PAYLOAD_RULES ниже.
Если ответ бота не распознан, в лог-группу уходит его текст и список кнопок —
по ним правила можно подстроить.
"""
import asyncio
import contextlib
import datetime
import logging
import time
from contextlib import AsyncExitStack

from telethon import events
from telethon.tl.functions.messages import StartBotRequest

from config import (
    CHECK_BOTS, TARGET_BOT, SHOP_BOT, CHECK_WARM_BOTS, CHECK_CLAIM_WORDS,
    PROMO_KEYWORDS, PROMO_REJECT_KEYWORDS,
)
from utils import BoundedSet, find_button, human_delay, parse_tme_target
from antiban import safe_call, TooManyFloodWaitsError
from core import utcnow

logger = logging.getLogger(__name__)

CHECK_WORDS = ["чек", "cheque", "voucher", "ваучер"]
CHECK_MAX_ATTEMPTS = 3
CHECK_REPLY_TIMEOUT_SEC = 8     # ждать ответа бота на один шаг
CHECK_CLAIM_STEPS = 3           # сколько шагов «нажать кнопку → ждать ответ» максимум
CHECK_POLL_FALLBACK_SEC = 1.0   # страховочный опрос, если событие не пришло
CHECK_RETRY_DELAY = (0.2, 0.5)  # пауза между попытками (скорость важнее «человечности»)

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
    SHOP_BOT: lambda p: not p.isdigit(),
}


def classify_reply(text: str) -> str:
    low = (text or "").lower()
    if any(w in low for w in PROMO_REJECT_KEYWORDS + REJECT_EXTRA):
        return "rejected"
    if any(w in low for w in SUCCESS_HINTS):
        return "ok"
    return "unknown"


def describe_buttons(msg) -> str:
    """'[Получить 111 ⭐|cb] [Канал|url]' — для лога, когда ответ не распознан."""
    out = []
    for row in (getattr(msg, "buttons", None) or []):
        for b in row:
            kind = "url" if getattr(b, "url", None) else ("cb" if getattr(b, "data", None) else "btn")
            out.append(f"[{b.text}|{kind}]")
    return " ".join(out) or "(кнопок нет)"


class CheckHunter:
    def __init__(self, client, core, tg_logger, label: str, db=None):
        self.client = client
        self.core = core
        self.tg_logger = tg_logger
        self.label = label
        self.db = db

        self.hub = None  # ставится в Hub.register()

        self.done = BoundedSet(200)
        self.in_progress = set()
        self._entities = {}   # bot -> InputPeer
        self._ids = {}        # bot -> user_id (чтобы сопоставлять входящие события)
        self._waiters = {}    # user_id -> {asyncio.Queue}
        self._tasks = set()
        self._handler = None

    # ------------------------------------------------------------------
    # Жизненный цикл: события ответов ботов + прогрев
    # ------------------------------------------------------------------
    def install(self):
        """Событийное ожидание ответов ботов (идемпотентно)."""
        if self._handler is not None:
            return

        async def on_msg(event):
            qs = self._waiters.get(event.chat_id)
            if not qs:
                return
            for q in list(qs):
                try:
                    q.put_nowait(event.message)
                except asyncio.QueueFull:
                    pass

        self._handler = on_msg
        self.client.add_event_handler(on_msg, events.NewMessage(incoming=True, func=lambda e: e.is_private))
        self.client.add_event_handler(on_msg, events.MessageEdited(incoming=True, func=lambda e: e.is_private))

    def uninstall(self):
        if self._handler is not None:
            try:
                self.client.remove_event_handler(self._handler)
            except Exception:
                pass
            self._handler = None

    async def warmup(self):
        """Заранее резолвим бота-магазин: первый чек не тратит время на
        ResolveUsername. Ошибки не фатальны — резолв повторится в момент чека."""
        for bot in CHECK_WARM_BOTS:
            try:
                await self._entity(bot)
            except Exception as e:
                logger.warning("[%s] прогрев @%s не удался: %r", self.label, bot, e)

    async def _entity(self, bot: str):
        ent = self._entities.get(bot)
        if ent is None:
            ent = await self.client.get_input_entity(bot)
            self._entities[bot] = ent
            uid = getattr(ent, "user_id", None)
            if uid:
                self._ids[bot] = uid
        return ent

    # ------------------------------------------------------------------
    @staticmethod
    def is_check_link(bot: str, payload: str, context_lower: str) -> bool:
        """context_lower — текст поста + подписи кнопок под ним (нижний регистр)."""
        bot = bot.lower()
        if bot == TARGET_BOT.lower():
            # у TARGET_BOT чисто числовой payload — это реферальный id
            if payload.isdigit():
                return False
            return any(w in context_lower for w in PROMO_KEYWORDS + CHECK_WORDS)
        if bot not in CHECK_BOTS:
            return False
        rule = _PAYLOAD_RULES.get(bot)
        if rule and not rule(payload):
            return False
        if not rule and payload.isdigit():
            return False
        if bot == SHOP_BOT:
            # магазин шлёт в канал не только чеки: нужен «чек» в тексте или
            # кнопка «Получить…» и payload похож на id чека
            if any(w in context_lower for w in CHECK_WORDS):
                return True
            return any(w in context_lower for w in CHECK_CLAIM_WORDS) and len(payload) >= 8
        return True

    def submit(self, bot: str, payload: str):
        """Поставить чек в работу. Синхронно занимает чек в хабе: если другой
        аккаунт увидел пост раньше — тихо выходим, чек заберёт он."""
        bot = bot.lower()
        key = (bot, payload)
        if key in self.done or key in self.in_progress:
            return
        if self.hub is not None and not self.hub.claim_check(self.label, key):
            return
        self.in_progress.add(key)
        task = asyncio.create_task(self.claim(bot, payload, time.monotonic()))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    async def claim(self, bot: str, payload: str, t0: float):
        key = (bot, payload)
        # логи уходят в очередь и не тормозят путь к боту (см. tg_logger.py)
        await self.tg_logger.log(f"🧾 Чек: забираю @{bot} `{payload[:40]}`")
        outcome, reply_text, extra = "retry", "", ""
        try:
            for attempt in range(1, CHECK_MAX_ATTEMPTS + 1):
                try:
                    outcome, reply_text = await asyncio.wait_for(self._try_claim(bot, payload), 40)
                except TooManyFloodWaitsError as e:
                    extra = f"FloodWait: {e}"
                    await self.tg_logger.log(f"Чек @{bot}: слишком много FloodWait", error=str(e))
                    outcome = "retry"
                    break
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    extra = "таймаут"
                    logger.warning(f"⚠️ [{self.label}] Чек @{bot} попытка #{attempt}: таймаут")
                    outcome = "retry"
                except Exception as e:
                    extra = repr(e)[:120]
                    logger.warning(f"⚠️ [{self.label}] Чек @{bot} попытка #{attempt}: {e!r}")
                    outcome = "retry"
                if outcome != "retry":
                    break
                await human_delay(*CHECK_RETRY_DELAY)

            took = time.monotonic() - t0
            if outcome == "ok":
                self.done.add(key)
                await self.tg_logger.log(f"✅ Чек @{bot} активирован за {took:.1f} с\n📩 {reply_text[:150]}")
            elif outcome == "rejected":
                self.done.add(key)
                await self.tg_logger.log(f"Чек @{bot} отклонён ({took:.1f} с): {reply_text[:150]}", error="отклонён")
            elif outcome == "unknown":
                self.done.add(key)
                await self.tg_logger.log(
                    f"🧾 Чек @{bot}: ответ бота не распознан, успех не подтверждён ({took:.1f} с)\n"
                    f"📩 {reply_text[:300]}\n🔘 {self._last_buttons}")
            else:
                await self.tg_logger.log(
                    f"Чек @{bot}: не удалось за {CHECK_MAX_ATTEMPTS} попытки"
                    f"{' — ' + extra if extra else ''}"
                    f"{' (передаю другому аккаунту)' if self.hub else ''}",
                    error="нет ответа")
            if self.hub is not None:
                self.hub.finish_check(self.label, key, final=(outcome != "retry"))
        finally:
            self.in_progress.discard(key)

    # ------------------------------------------------------------------
    _last_buttons = ""

    @contextlib.contextmanager
    def _subscribe(self, uid):
        q = asyncio.Queue(maxsize=20)
        self._waiters.setdefault(uid, set()).add(q)
        try:
            yield q
        finally:
            ws = self._waiters.get(uid)
            if ws:
                ws.discard(q)
                if not ws:
                    self._waiters.pop(uid, None)

    @staticmethod
    def _floor(since):
        return since.replace(microsecond=0) - datetime.timedelta(seconds=1)

    async def _wait(self, q, entity, since, timeout: float):
        """Первое СВЕЖЕЕ входящее сообщение бота: из события (мгновенно) или,
        если событие потерялось, из страховочного опроса."""
        floor = self._floor(since)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            left = deadline - loop.time()
            if left <= 0:
                return None
            try:
                m = await asyncio.wait_for(q.get(), min(CHECK_POLL_FALLBACK_SEC, left))
            except asyncio.TimeoutError:
                m = None
                try:
                    for last in await self.client.get_messages(entity, limit=1):
                        if not last.out:
                            m = last
                except Exception as e:
                    logger.debug("[%s] чек: опрос ответа: %r", self.label, e)
            if m is not None and not m.out and (m.edit_date or m.date) >= floor and (m.text or m.buttons):
                return m

    async def _try_claim(self, bot: str, payload: str):
        entity = await self._entity(bot)
        uid = self._ids.get(bot)
        is_target = bot == TARGET_BOT.lower()
        async with AsyncExitStack() as stack:
            if is_target:
                # тот же чат/состояние, что у заданий и промо — идём через лок
                await stack.enter_async_context(self.core.bot_lock.acquire(high_priority=True))
            q = stack.enter_context(self._subscribe(uid))  # подписка ДО запроса: ответ не потеряется

            since = utcnow()
            await safe_call(
                lambda: self.client(StartBotRequest(bot=entity, peer=entity, start_param=payload)),
                action="start_check",
            )
            reply = await self._wait(q, entity, since, CHECK_REPLY_TIMEOUT_SEC)
            if reply is None:
                return "retry", ""
            return await self._advance(q, entity, reply)

    @staticmethod
    def _find_claim_button(msg):
        """Кнопка «Получить/Забрать…» — только не-ссылочная (callback)."""
        for row in (getattr(msg, "buttons", None) or []):
            for b in row:
                if getattr(b, "url", None):
                    continue
                low = (b.text or "").lower()
                if any(w in low for w in CHECK_CLAIM_WORDS):
                    return b
        return None

    async def _advance(self, q, entity, reply):
        """Идём по ответам бота: жмём «Получить» (при необходимости — после
        подписки на спонсоров) до результата. -> (outcome, text)."""
        text = reply.text or ""
        outcome = classify_reply(text)
        self._last_buttons = describe_buttons(reply)
        seen_targets = set()
        for _ in range(CHECK_CLAIM_STEPS):
            if outcome in ("ok", "rejected"):
                return outcome, text

            joined, fresh = await self._join_sponsors(reply, seen_targets)
            btn = self._find_claim_button(reply)
            if btn is None and joined:
                btn = find_button(reply, ["подпис", "провер", "check", "готов", "я подписал"])
            if btn is None:
                break
            if fresh:
                await human_delay(0.8, 1.5)  # Telegram должен учесть новое вступление

            since = utcnow()
            alert, _ans = await self.core.click_button(btn)
            if alert:
                text, outcome = alert, classify_reply(alert)
                if outcome != "unknown":
                    return outcome, text
            nxt = await self._wait(q, entity, since, CHECK_REPLY_TIMEOUT_SEC)
            if nxt is not None:
                reply = nxt
                text = nxt.text or ""
                outcome = classify_reply(text)
                self._last_buttons = describe_buttons(nxt)
            elif not alert:
                break  # кнопку нажали, а бот молчит
        return outcome, text

    async def _join_sponsors(self, reply, seen: set):
        """-> (подписаны_на_спонсоров, вступили_только_что). Каждый канал за
        один чек обрабатывается один раз."""
        joined = fresh = False
        for row in (getattr(reply, "buttons", None) or []):
            for b in row:
                url = getattr(b, "url", None)
                if not url:
                    continue
                t = parse_tme_target(url)
                if not t or t["kind"] not in ("channel", "invite"):
                    continue
                if t["target"] in seen:
                    joined = True
                    continue
                seen.add(t["target"])
                status = await self.core.safe_join(t["target"])
                joined = joined or status in ("ok", "already")
                fresh = fresh or status == "ok"
        return joined, fresh
