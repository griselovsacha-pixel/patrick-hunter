"""
PromoHunter — детект промокода в постах канала и его активация.

Что изменилось по сравнению с прошлой версией (почему коды "не ловились"):
  1. Ожидание ответов бота теперь не висит до таймаута из-за сравнения
     дат с точностью до секунды (см. core.wait_fresh) — на каждом из ~5
     шагов активации терялись 8-20 секунд.
  2. Логи в Telegram/Supabase больше не стоят на пути активации
     (см. tg_logger.py): раньше перед первой попыткой был сетевой запрос,
     а при FloodWait в группе логов активация могла уснуть на минуты.
  3. Любая ошибка попытки (таймаут клика, RPC-ошибка) теперь ведёт к
     повтору; раньше исключение убивало ВСЮ активацию, и остальные 7
     попыток не выполнялись.
  4. Отказ бота («неверный/использован») больше не считается успехом и не
     пишется в статистику как «активирован +2⭐».
  5. Пост приходит не только событием, но и опросом канала (watcher.py),
     а найденный одним аккаунтом код сразу уходит остальным (hub.py).
"""
import asyncio
import logging

from config import (
    TARGET_BOT, PROMO_KEYWORDS, PROMO_REJECT_KEYWORDS,
    PROMO_MAX_ATTEMPTS, PROMO_RETRY_DELAY_RANGE,
)
from utils import (
    find_promo_in_text, extract_start_links, button_texts, find_button, human_delay,
    BoundedSet, parse_tme_target,
)
from antiban import TooManyFloodWaitsError
from core import utcnow
from checks import CHECK_WORDS

logger = logging.getLogger(__name__)

# Одна попытка (весь путь /start → профиль → промо → ввод кода) не должна
# держать лок дольше этого: иначе зависший шаг блокирует аккаунт.
PROMO_ATTEMPT_TIMEOUT_SEC = 45

OK, REJECTED, RETRY = "ok", "rejected", "retry"


class PromoHunter:
    def __init__(self, client, core, tg_logger, profile_stats, label: str, db=None):
        self.client = client
        self.core = core
        self.tg_logger = tg_logger
        self.profile_stats = profile_stats
        self.label = label
        self.db = db

        # Подключаются в account.py после сборки аккаунта.
        self.hub = None
        self.checks = None

        self.activated_promos = BoundedSet(100)
        self.promo_in_progress = set()
        self._tasks = set()
        self._unrecognized_seen = BoundedSet(100)

    # ------------------------------------------------------------------
    # Вход: пост канала (из событий или из опроса — см. watcher.py)
    # ------------------------------------------------------------------
    async def on_post(self, message):
        raw = message.raw_text or ""
        low = raw.lower()
        logger.info(f"📨 [{self.label}] Канал: {raw[:120]}")
        handled = False

        # Чеки — самое срочное: обрабатываем первыми. Чек забирает один аккаунт
        # (кто первым увидел пост — см. hub.claim_check), остальные тихо пропускают.
        if self.checks:
            ctx = low + " " + button_texts(message)
            for bot, payload in extract_start_links(message):
                if self.checks.is_check_link(bot, payload, ctx):
                    handled = True
                    self.checks.submit(bot, payload)

        code = find_promo_in_text(raw)
        if code:
            handled = True
            logger.info(f"🔔 [{self.label}] Промокод найден: {code}")
            if self.db:
                self._spawn(self.db.event("promo_found", promo_code=code))
            if self.hub:
                self.hub.share_promo(self.label, code)
            self.submit(code)

        if not handled and self._looks_relevant(low):
            key = (message.id, hash(raw))
            if key not in self._unrecognized_seen:
                self._unrecognized_seen.add(key)
                await self.tg_logger.log(
                    f"Пост про промо/чек, но ничего не распознано. Текст целиком:\n{raw[:500]}",
                    error="не распознано",
                )

    @staticmethod
    def _looks_relevant(low: str) -> bool:
        return any(w in low for w in PROMO_KEYWORDS) or any(w in low for w in CHECK_WORDS)

    # ------------------------------------------------------------------
    def _spawn(self, coro):
        """create_task с удержанием ссылки (иначе задачу может собрать GC) и
        логированием необработанных исключений."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(t):
            self._tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.error(f"[{self.label}] фоновая задача: {t.exception()!r}")

        task.add_done_callback(_done)
        return task

    def submit(self, code: str):
        """Поставить активацию кода в работу (дубли отсекаются)."""
        key = code.lower()
        if key in self.activated_promos or key in self.promo_in_progress:
            return
        self._spawn(self.activate(code))

    async def activate(self, code: str):
        key = code.lower()
        if key in self.activated_promos or key in self.promo_in_progress:
            return
        prot = self.core.protection
        # дневной бюджет «горячих» активаций (в ультра-прогреве — малый; в обычном режиме без лимита)
        if prot is not None and not prot.budget_try("hot", high=True):
            await self.tg_logger.log(f"🛡 Промокод `{code}` пропущен: дневной лимит активаций защиты аккаунта исчерпан")
            return
        max_attempts = prot.promo_attempts(PROMO_MAX_ATTEMPTS) if prot is not None else PROMO_MAX_ATTEMPTS
        retry_range = prot.promo_retry_range(PROMO_RETRY_DELAY_RANGE) if prot is not None else PROMO_RETRY_DELAY_RANGE
        self.promo_in_progress.add(key)
        await self.tg_logger.log(f"🚀 Активирую промокод: `{code}`")
        outcome = RETRY
        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    outcome = await asyncio.wait_for(self._attempt(code), PROMO_ATTEMPT_TIMEOUT_SEC)
                except TooManyFloodWaitsError as e:
                    await self.tg_logger.log(f"Промокод `{code}`: слишком много FloodWait", error=str(e))
                    outcome = RETRY
                    break
                except asyncio.TimeoutError:
                    logger.warning(f"⚠️ [{self.label}] Промокод {code}: попытка #{attempt} — таймаут")
                    outcome = RETRY
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"⚠️ [{self.label}] Промокод {code}: попытка #{attempt} — {e!r}")
                    outcome = RETRY

                if outcome != RETRY:
                    break
                logger.warning(f"⚠️ [{self.label}] Промокод {code}: попытка #{attempt} неудачна")
                if attempt < max_attempts:
                    await self.core.delay(*retry_range, fast=True)

            if outcome == OK:
                self.activated_promos.add(key)
                self.profile_stats.promos_activated_today.append(code)
                if self.db:
                    await self.db.event("promo_activated", promo_code=code, stars=2.0)
                self._spawn(self._post_success_refresh())
            elif outcome == REJECTED:
                # Бот явно отказал — больше не пробуем, но и «активированным» не считаем.
                self.activated_promos.add(key)
                if self.db:
                    await self.db.event("promo_failed", promo_code=code, error_reason="rejected by bot")
            else:
                self.profile_stats.add_error(f"промо {code}: все попытки неудачны")
                await self.tg_logger.log(
                    f"Промокод `{code}`: все {max_attempts} попыток неудачны", error="исчерпаны попытки")
                if self.db:
                    await self.db.event("promo_failed", promo_code=code, error_reason="all attempts failed")
        finally:
            self.promo_in_progress.discard(key)

    async def _attempt(self, code: str) -> str:
        """Одна попытка: OK / REJECTED (явный отказ бота — ретраить
        бессмысленно) / RETRY (что-то не ответило — пробуем снова)."""
        core = self.core
        async with core.bot_lock.acquire(high_priority=True):
            msg = await core._open_unlocked()
            profile_btn = find_button(msg, ["профил", "profile"])
            if not profile_btn:
                return RETRY

            since = utcnow()
            await core.click_button(profile_btn)
            # Ждём именно экран, где есть кнопка «Промокод» (а не любое сообщение).
            msg = await core.wait_fresh(
                since, timeout=6, fallback=False,
                predicate=lambda m: find_button(m, ["промо"]) is not None,
            )
            promo_btn = find_button(msg, ["промо"])
            if not promo_btn:
                return RETRY

            since = utcnow()
            await core.click_button(promo_btn)
            prompt = await core.wait_fresh(since, timeout=6, fallback=False)
            if not prompt:
                return RETRY

            since = utcnow()
            await core.api(lambda: self.client.send_message(TARGET_BOT, code), action="send_promo_code", high=True)
            resp = await core.wait_fresh(since, timeout=8, fallback=False)
            if not resp:
                return RETRY

            resp_text = resp.text or ""
            if any(w in resp_text.lower() for w in PROMO_REJECT_KEYWORDS):
                await self.tg_logger.log(f"Промокод `{code}` отклонён ботом: {resp_text[:150]}", error="отклонён")
                return REJECTED

            await self._satisfy_sponsor_buttons(resp)
            await self.tg_logger.log(f"✅ Промокод `{code}` активирован!\n📩 {resp_text[:100]}")
            return OK

    async def _satisfy_sponsor_buttons(self, resp):
        """Если бот просит подписаться на спонсоров — вступаем и жмём проверку."""
        if not resp.buttons:
            return
        for row in resp.buttons:
            for b in row:
                url = getattr(b, "url", None)
                b_text = b.text.lower()
                if url:
                    t = parse_tme_target(url)
                    if t and t["kind"] in ("channel", "invite"):
                        await self.core.safe_join(t["target"])
                if any(w in b_text for w in ["подписал", "проверить", "готово", "я подписал", "check"]):
                    await self.core.delay(2, 3, fast=True)
                    await self.core.click_button(b)

    async def _post_success_refresh(self):
        """Обновляем реальный баланс ПОСЛЕ активации и вне горячего пути
        (раньше это делалось внутри лока и задерживало следующий код)."""
        try:
            async with self.core.bot_lock.acquire():
                msg = await self.core._open_unlocked()
                btn = find_button(msg, ["профил", "profile"])
                if not btn:
                    return
                since = utcnow()
                await self.core.click_button(btn)
                resp = await self.core.wait_fresh(since, timeout=6)
                if resp:
                    await self.profile_stats.update_from_profile_text(resp.text or "")
        except Exception as e:
            logger.warning(f"[{self.label}] обновление баланса после промо: {e!r}")
