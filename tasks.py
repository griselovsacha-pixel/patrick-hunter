"""
TaskRunner — цикл выполнения заданий бота (подписки, запуск ботов, мини-аппы).

Что изменилось по результатам аудита логов (раньше логи «врали»):
  * «Задание выполнено» писалось по факту нажатия кнопок. Теперь итог берётся
    из ОТВЕТА БОТА на «Проверить» (всплывающее окно): засчитано / не засчитано /
    ответ не распознан — и только засчитанные идут в статистику и БД.
  * Меню бота (новости, магазин, чат) обрабатывалось как «задание» — аккаунт
    вступал в чаты бота и запускал его магазин. Теперь задание — только
    сообщение с маркерами «Новое задание»/«Награда» (config.TASK_MARKERS).
  * Следующее задание бот часто РЕДАКТИРУЕТ в том же сообщении (тот же id) —
    раньше это принималось за «зациклилось» и новое задание пропускалось вслепую.
    Теперь сравниваем отпечаток (id + текст + кнопки).
  * Инвайт-ссылки с «-» обрезались (+RfN) — вступление падало, а в логе было
    «подписался». Теперь см. utils.parse_tme_target и core.safe_join (статусы).
  * Запуск бота по ссылке с ?start=payload слал голый /start.
  * «Все задания выполнены» срабатывало по слову «респект» из поздравления и
    бросало остаток задач (см. config.ALL_DONE_PHRASES).
  * Цикл заданий просыпается по просьбе кликера («выполни задание») —
    request_run(), а не раз в 2–4 часа.
"""
import asyncio
import logging
import random
import re
import time

from config import (
    ALL_DONE_PHRASES, CONFIRM_WORDS, SKIP_TASK_WORDS,
    TASK_WAKE_MIN_GAP_SEC, TASK_STUCK_RETRY_RANGE, TARGET_BOT,
)
from utils import (
    is_webapp_button, human_delay, random_task_interval, parse_tme_target,
    is_task_message, parse_reward, classify_task_alert, message_signature,
    find_button,
)
from core import utcnow

logger = logging.getLogger(__name__)

MAX_TASKS_PER_CYCLE = 30


class TaskRunner:
    def __init__(self, client, core, tg_logger, profile_stats, label: str, db=None):
        self.client = client
        self.core = core
        self.tg_logger = tg_logger
        self.profile_stats = profile_stats
        self.label = label
        self.db = db
        self.start_offset = 0  # выставляется в account.py (индекс аккаунта * TASK_START_STAGGER_SEC)
        self._last_cycle_start = 0.0
        self._running = False

    # ------------------------------------------------------------------
    # Пробуждение по просьбе кликера
    # ------------------------------------------------------------------
    def request_run(self, reason: str = "") -> bool:
        """Попросить внеочередной цикл заданий. Не чаще раза в
        TASK_WAKE_MIN_GAP_SEC и не во время идущего цикла (защита от FloodWait)."""
        if self._running:
            return True  # уже идёт — кликеру достаточно дождаться его конца
        if self._last_cycle_start and time.monotonic() - self._last_cycle_start < TASK_WAKE_MIN_GAP_SEC:
            return False
        self.core.tasks_cycle_done.clear()  # кликер должен дождаться именно НОВОГО цикла
        self.core.tasks_wanted.set()
        return True

    async def _sleep_or_wake(self, seconds: float) -> bool:
        ev = self.core.tasks_wanted
        try:
            await asyncio.wait_for(ev.wait(), timeout=seconds)
            woke = True
        except asyncio.TimeoutError:
            woke = False
        ev.clear()
        return woke

    # ------------------------------------------------------------------
    # Одно задание
    # ------------------------------------------------------------------
    async def _click_skip(self, msg) -> bool:
        btn = find_button(msg, ["пропустить"])
        if not btn:
            return False
        try:
            await btn.click()
        except Exception as e:
            logger.warning(f"⚠️ [{self.label}] Клик Пропустить: {e}")
            return False
        await human_delay(2, 3)
        return True

    async def _refetch(self, msg):
        """Актуальная версия сообщения (бот мог его отредактировать)."""
        try:
            fresh = await self.client.get_messages(TARGET_BOT, ids=msg.id)
            return fresh or msg
        except Exception:
            return msg

    async def _do_actions(self, task_msg, text):
        """Выполняет то, что просит задание. -> (ok_parts, failed_parts, is_miniapp)."""
        ok_parts, failed = [], []
        is_miniapp = False
        handled_targets = set()

        async def act(target_info, source_url=None):
            kind, target = target_info["kind"], target_info["target"]
            key = (kind, target.lower(), target_info.get("start"))
            if key in handled_targets:
                return
            handled_targets.add(key)
            if kind in ("channel", "invite"):
                status = await self.core.safe_join(target)
                if status in ("ok", "already"):
                    ok_parts.append(f"подписался на {target[:18]}")
                else:
                    failed.append(f"вступление {target[:18]}: {status}")
            elif kind == "bot":
                if await self.core.start_bot(target, target_info.get("start")):
                    extra = " (с параметром)" if target_info.get("start") else ""
                    ok_parts.append(f"запустил бота @{target}{extra}")
                else:
                    failed.append(f"запуск @{target}")

        # 1) мини-аппы: ссылка startapp или web_view-кнопка
        startapp_url, webview_btn = None, None
        for row in (task_msg.buttons or []):
            for btn in row:
                url = getattr(btn, "url", None)
                if url and "startapp" in url.lower() and not startapp_url:
                    startapp_url = url
                raw = getattr(btn, "button", None)
                if type(raw).__name__ in ("KeyboardButtonWebView", "KeyboardButtonSimpleWebView") and not webview_btn:
                    webview_btn = btn
        if startapp_url or webview_btn:
            is_miniapp = True
            if startapp_url:
                opened = await self.core.handle_startapp(startapp_url)
            else:
                opened = await self.core.handle_webview_button(task_msg, webview_btn)
            label = (startapp_url or "web_view")[:45]
            (ok_parts if opened else failed).append(f"мини-апп {label}" + ("" if opened else " не открылся"))

        # 2) ссылки-кнопки (каналы / боты)
        found_button_link = False
        for row in (task_msg.buttons or []):
            for btn in row:
                btn_text = btn.text.lower()
                if any(w in btn_text for w in CONFIRM_WORDS) or any(
                        w in btn_text for w in ("пропустить", "меню", "назад", "главное")):
                    continue
                url = getattr(btn, "url", None)
                if not url or is_webapp_button(btn):
                    continue
                found_button_link = True
                t = parse_tme_target(url)
                if t and t["kind"] != "app":
                    try:
                        await act(t, url)
                    except Exception as e:
                        failed.append(f"{url[:30]}: {e}")
                else:
                    # внешний сайт: открыть/проверить сами не можем — но часть заданий
                    # «перейди по ссылке» бот засчитывает по кнопке «Проверить»; решает ответ бота
                    ok_parts.append(f"внешняя ссылка {url[:40]}")

        # 3) если кнопок-ссылок нет — ссылки и @упоминания из текста
        if not found_button_link and not is_miniapp:
            for url in re.findall(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", text):
                t = parse_tme_target(url)
                if t and t["kind"] != "app":
                    await act(t, url)
            for username in re.findall(r"@([a-zA-Z0-9_]{5,})", text):
                if username.lower().endswith("bot"):
                    await act({"kind": "bot", "target": username, "start": None})
                else:
                    await act({"kind": "channel", "target": username, "start": None})

        return ok_parts, failed, is_miniapp

    async def _confirm(self, task_msg, did_something: bool):
        """Нажимает «Проверить» и читает ответ бота. -> (verdict, alert_text, had_button)."""
        btn = find_button(task_msg, CONFIRM_WORDS)
        if not btn:
            return "unknown", "", False

        since = utcnow()
        alert, _ = await self.core.click_button(btn)
        verdict = classify_task_alert(alert) if alert else "unknown"

        # Не засчитано сразу после вступления — подписка могла «не успеть»: одна повторная проверка
        if verdict == "fail" and did_something:
            await self.core.yield_sleep(6, 10)
            cur = await self._refetch(task_msg)
            if (cur.text or "") == (task_msg.text or ""):  # задание не сменилось
                btn2 = find_button(cur, CONFIRM_WORDS)
                if btn2:
                    since = utcnow()
                    alert2, _ = await self.core.click_button(btn2)
                    if alert2:
                        alert, verdict = alert2, classify_task_alert(alert2)

        # Капча после подтверждения
        fresh = await self.core.wait_fresh(since, timeout=5, fallback=False)
        if fresh and "проверка" in (fresh.text or "").lower():
            if await self.core.captcha.solve_if_needed(fresh):
                await self.core.wait_fresh(utcnow(), timeout=5, fallback=False)
        return verdict, alert or "", True

    async def process_one_task(self, task_msg) -> tuple:
        """-> (result, desc, reward). result:
        'all_done' | 'not_task' | 'done' (бот засчитал) | 'unverified' (ответ не
        распознан) | 'failed' (бот не засчитал) | 'skip' (пропущено)."""
        text = task_msg.text or ""
        low = text.lower()

        if any(phrase in low for phrase in ALL_DONE_PHRASES):
            return "all_done", text, None
        if not is_task_message(task_msg):
            return "not_task", text[:100], None

        reward = parse_reward(text)

        # Платные задания (депозит и т.п.) выполнить нельзя — пропускаем
        if any(w in low for w in SKIP_TASK_WORDS):
            await self.tg_logger.log(f"⏭️ Задание требует оплаты/депозита — пропускаю\n📄 {text[:100]}")
            if await self._click_skip(task_msg):
                return "skip", "платное задание", reward
            await self.tg_logger.log("Платное задание без кнопки Пропустить", error="ручное вмешательство")
            return "failed", "платное задание без кнопки Пропустить", reward

        ok_parts, failed, is_miniapp = await self._do_actions(task_msg, text)
        desc = ", ".join(ok_parts) if ok_parts else "ничего не удалось выполнить"
        if failed:
            desc += " | не вышло: " + "; ".join(failed)[:120]

        if not ok_parts:
            # Нечего подтверждать (или всё упало) — пробуем пропустить сразу
            if await self._click_skip(task_msg):
                return "skip", desc, reward
            return "failed", desc, reward

        # Пауза перед проверкой (во время неё лок отдан: промокод не ждёт)
        if not is_miniapp:
            await self.core.yield_sleep(6, 12)

        verdict, alert, had_btn = await self._confirm(task_msg, did_something=True)
        if verdict == "ok":
            return "done", desc, reward
        if verdict == "fail":
            return "failed", f"{desc} — бот: {alert[:70]}", reward
        note = "ответ бота не распознан" + (f": {alert[:60]}" if alert else "")
        if not had_btn:
            note = "нет кнопки подтверждения"
        return "unverified", f"{desc} ({note})", reward

    # ------------------------------------------------------------------
    # Поиск следующего задания
    # ------------------------------------------------------------------
    async def _next_task(self, prev_sig, since, timeout: float = 14.0):
        """Ждём сообщение-задание, отличное от предыдущего (новое или отредактированное).
        -> ('task', msg) | ('all_done', msg) | ('same', msg) | ('none', None)."""
        floor = self.core._floor(since)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        same_msg = None
        while True:
            async for m in self.client.iter_messages(TARGET_BOT, limit=6):
                if m.out:
                    continue
                eff = m.edit_date or m.date
                low = (m.text or "").lower()
                if eff >= floor and any(p in low for p in ALL_DONE_PHRASES):
                    return "all_done", m
                if is_task_message(m):
                    sig = message_signature(m)
                    if prev_sig is None or sig != prev_sig:
                        if eff >= floor or prev_sig is None:
                            return "task", m
                    else:
                        same_msg = m
                    break  # решает самое свежее сообщение-задание
            if loop.time() >= deadline:
                break
            await asyncio.sleep(1.2)
        if same_msg is not None:
            return "same", same_msg
        return "none", None

    # ------------------------------------------------------------------
    # Основной цикл
    # ------------------------------------------------------------------
    async def run_forever(self):
        await asyncio.sleep(30 + self.start_offset)  # даём аккаунту "отлежаться"; сдвиг разводит аккаунты по времени
        while True:
            sleep_for = None
            try:
                sleep_for = await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[{self.label}] Ошибка заданий: {e}")
                self.profile_stats.add_error(f"tasks: {str(e)[:50]}")
                await self.tg_logger.log("Ошибка цикла заданий", error=str(e))
                sleep_for = 600
            finally:
                self._running = False
                self.core.tasks_cycle_done.set()

            woke = await self._sleep_or_wake(sleep_for or random_task_interval())
            if woke:
                await self.tg_logger.log("⏰ Цикл заданий запущен досрочно (кликеру нужно выполнить задание)")

    async def _run_cycle(self) -> float:
        """Один проход по заданиям. Возвращает, сколько секунд спать до следующего."""
        self._running = True
        self._last_cycle_start = time.monotonic()
        self.core.tasks_cycle_done.clear()
        self.core.tasks_wanted.clear()  # просьба, пришедшая до начала цикла, уже выполняется
        await self.tg_logger.log("📋 Начинаю выполнение заданий...")

        async with self.core.bot_lock.acquire():
            msg = await self.core._open_unlocked()
            if not msg or not msg.buttons:
                await self.tg_logger.log("Бот не ответил на /start", error="нет ответа")
                return random.uniform(*TASK_STUCK_RETRY_RANGE)

            btn = find_button(msg, ["задани"])
            if not btn:
                await self.tg_logger.log("Кнопка заданий не найдена", error="нет кнопки")
                return random.uniform(*TASK_STUCK_RETRY_RANGE)
            since = utcnow()
            await btn.click()

        counters = {"done": 0, "unverified": 0, "failed": 0, "skip": 0}
        prev_sig = None
        stuck = False
        not_task_streak = 0
        errors_in_row = 0
        reason_end = "лимит"

        for i in range(MAX_TASKS_PER_CYCLE):
            try:
                async with self.core.bot_lock.acquire():
                    kind, task_msg = await self._next_task(prev_sig, since)

                    if kind == "all_done":
                        reason_end = "бот: заданий больше нет"
                        break
                    if kind == "none":
                        reason_end = "бот больше не показывает заданий"
                        break
                    if kind == "same":
                        # Бот не сменил задание после нашей попытки — пропускаем его
                        if await self._click_skip(task_msg):
                            counters["skip"] += 1
                            since = utcnow()
                            continue
                        await self.tg_logger.log(
                            f"Задание не сменилось и кнопки Пропустить нет\n📄 {(task_msg.text or '')[:100]}",
                            error="цикл")
                        stuck = True
                        reason_end = "задание застряло"
                        break

                    prev_sig = message_signature(task_msg)
                    since = utcnow()
                    result, desc, reward = await self.process_one_task(task_msg)
                errors_in_row = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[{self.label}] Ошибка на задании #{i + 1}: {e}")
                self.profile_stats.add_error(f"tasks#{i + 1}: {str(e)[:40]}")
                errors_in_row += 1
                if errors_in_row >= 3:
                    stuck = True
                    reason_end = "серия ошибок"
                    break
                await human_delay(3, 6)
                continue

            if result == "all_done":
                reason_end = "бот: заданий больше нет"
                break
            if result == "not_task":
                not_task_streak += 1
                if not_task_streak >= 2:
                    reason_end = "дальше не задания (меню бота)"
                    break
                continue
            not_task_streak = 0

            if result == "done":
                counters["done"] += 1
                stars = reward if reward is not None else 0.4
                await self.tg_logger.log(f"✅ Задание засчитано ботом (+{stars}⭐)\n📌 {desc}")
                if self.db:
                    await self.db.event("task", stars=stars, task_type="task")
                    if "мини-апп" in desc:
                        await self.db.event("mini_app", stars=0.0, task_type="mini_app")
                await human_delay(4, 9)
            elif result == "unverified":
                counters["unverified"] += 1
                await self.tg_logger.log(f"❔ Задание отправлено, но бот не подтвердил\n📌 {desc}")
                await human_delay(3, 6)
            elif result == "failed":
                counters["failed"] += 1
                await self.tg_logger.log(f"❌ Задание не засчитано\n📌 {desc}")
                await human_delay(2, 4)
            elif result == "skip":
                counters["skip"] += 1
                await human_delay(2, 4)

        self.profile_stats.tasks_completed_today += counters["done"]
        self.profile_stats.tasks_skipped_today += counters["skip"]
        try:
            async with self.core.bot_lock.acquire():
                await self._refresh_balance_unlocked()
        except Exception as e:
            logger.warning(f"[{self.label}] Баланс после заданий: {e}")

        if stuck:
            interval = random.uniform(*TASK_STUCK_RETRY_RANGE)
        else:
            interval = random_task_interval()
        h, m = int(interval) // 3600, (int(interval) % 3600) // 60
        await self.tg_logger.log(
            f"🏁 Цикл заданий завершён ({reason_end}): ✅{counters['done']} засчитано, "
            f"❔{counters['unverified']} без подтверждения, ❌{counters['failed']} не засчитано, "
            f"⏭️{counters['skip']} пропущено\n⏳ Следующая проверка через {h}ч {m}м"
        )
        return interval

    async def _refresh_balance_unlocked(self):
        """После цикла заходим в Профиль и берём реальный баланс (лок уже взят)."""
        msg = await self.core._open_unlocked()
        btn = find_button(msg, ["профил", "profile"])
        if not btn:
            return
        since = utcnow()
        await self.core.click_button(btn)
        resp = await self.core.wait_fresh(since, timeout=6)
        if resp:
            await self.profile_stats.update_from_profile_text(resp.text or "")
