"""
Clicker — 27-30 случайных кликов в день в окне 8:00-00:00.
Каждые 10 кликов заходим в Профиль и обновляем реальный баланс (см.
profile_stats.py) вместо оценки "с потолка".
"""
import asyncio
import datetime
import logging
import random

from config import (
    TIMEZONE_OFFSET, CLICKER_MIN_PER_DAY, CLICKER_MAX_PER_DAY,
    CLICKER_START_HOUR, CLICKER_END_HOUR, TARGET_BOT,
)
from utils import human_delay, classify_click_alert, parse_cooldown_seconds
from core import utcnow

logger = logging.getLogger(__name__)


class Clicker:
    def __init__(self, client, core, tg_logger, profile_stats, label: str, db=None):
        self.client = client
        self.core = core
        self.tg_logger = tg_logger
        self.profile_stats = profile_stats
        self.label = label
        self.db = db
        self.target_today = 0
        self._manual = asyncio.Event()
        self.sched = None  # scheduler.JobScheduler, ставится из account.py
        self.on_gate = None  # callable: просьба к TaskRunner выполнить задания (см. account.py)

    def _now_local(self):
        return datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)

    def request(self) -> bool:
        """Ручной клик из админки (вне плана дня)."""
        self._manual.set()
        return True

    def _tag(self, index, total) -> str:
        return "ручной" if index == "ручной" else f"#{index}/{total}"

    async def _wait_until(self, when_local):
        """Спим до времени слота; ручной клик из админки выполняется по ходу ожидания."""
        while True:
            secs = (when_local - self._now_local()).total_seconds()
            if secs <= 0:
                return
            await self.sched.set_next("clicker", secs)
            try:
                await asyncio.wait_for(self._manual.wait(), timeout=secs)
            except asyncio.TimeoutError:
                continue
            self._manual.clear()
            await self._manual_click()

    async def _manual_click(self):
        self.sched.begin("clicker")
        status = "error"
        try:
            status = await self._click_slot("ручной", self.target_today or 0)
        finally:
            await self.sched.end("clicker", status, "ручной клик")

    def _make_plan(self, now):
        times_today = random.randint(CLICKER_MIN_PER_DAY, CLICKER_MAX_PER_DAY)
        window_sec = (CLICKER_END_HOUR - CLICKER_START_HOUR) * 3600
        part_sec = window_sec // times_today
        day_start = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
        midnight = day_start + datetime.timedelta(hours=CLICKER_END_HOUR - CLICKER_START_HOUR)
        times = []
        for i in range(times_today):
            part_start = day_start + datetime.timedelta(seconds=i * part_sec)
            t = part_start + datetime.timedelta(seconds=random.randint(0, int(part_sec * 0.8)) + random.randint(0, 300))
            if t > midnight:
                t = midnight - datetime.timedelta(seconds=random.randint(60, 600))
            times.append(t)
        times.sort()
        return {"date": now.strftime("%Y-%m-%d"), "times": [t.isoformat() for t in times], "done": 0}

    async def run_forever(self):
        # После рестарта план дня берём из БД и НЕ нагоняем пропущенные слоты пачкой
        # (раньше это был залп кликов -> FloodWait). Пропущенные просто засчитываются пропущенными.
        await self.sched.load()
        first = True
        while True:
            try:
                now = self._now_local()
                if now.hour < CLICKER_START_HOUR:
                    await self._wait_until(now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0))
                    continue

                plan = self.sched.get_data("clicker")
                if plan.get("date") != now.strftime("%Y-%m-%d") or not plan.get("times"):
                    plan = self._make_plan(now)
                    await self.sched.set_data("clicker", plan)
                times = [datetime.datetime.fromisoformat(t) for t in plan["times"]]
                self.target_today = len(times)
                self.profile_stats.clicker_clicks_today = max(self.profile_stats.clicker_clicks_today, plan["done"])

                if first:
                    first = False
                    # пропущенные за простой слоты (позже 5 минут назад) — пропускаем, не догоняем
                    idx = plan["done"]
                    while idx < len(times) and times[idx] < now - datetime.timedelta(minutes=5):
                        idx += 1
                    skipped = idx - plan["done"]
                    if skipped:
                        await self.tg_logger.log(f"🎮 Пропущено слотов за время простоя: {skipped} (не нагоняю, чтобы не словить FloodWait)")
                        plan["done"] = idx
                        await self.sched.set_data("clicker", plan)
                    # первый после рестарта слот — с разбросом и сдвигом на аккаунт, не мгновенно
                    if plan["done"] < len(times) and times[plan["done"]] <= now:
                        times[plan["done"]] = now + datetime.timedelta(
                            seconds=random.uniform(240, 600) + self.sched.index * 240)

                while plan["done"] < len(times):
                    await self._wait_until(times[plan["done"]])
                    self.sched.begin("clicker")
                    status = "error"
                    try:
                        status = await self._click_slot(plan["done"] + 1, len(times))
                        if status == "limit":
                            # бот: «Ты слишком много кликал! Приходи завтра» — остаток слотов дня бессмыслен
                            plan["done"] = len(times) - 1
                            await self.tg_logger.log("🎮 Остальные клики на сегодня отменены — до завтра")
                    finally:
                        plan["done"] += 1
                        await self.sched.set_data("clicker", plan)
                        await self.sched.end("clicker", status, f"клик {plan['done']}/{len(times)}")

                now = self._now_local()
                tomorrow = (now + datetime.timedelta(days=1)).replace(
                    hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
                await self._wait_until(tomorrow)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[{self.label}] Ошибка кликера: {e}")
                await asyncio.sleep(3600)

    async def _click_slot(self, index: int, total: int):
        """Один слот: клик + ожидание кулдауна / выполнение заданий и одна повторная попытка."""
        status, wait = await self._do_one_click(index, total)
        if status == "cooldown" and 0 < wait <= 900:
            await asyncio.sleep(wait + random.uniform(5, 25))
            status, wait = await self._do_one_click(index, total)
        elif status == "gate":
            await self._wait_for_tasks()
            status, wait = await self._do_one_click(index, total)
        return status

    async def _wait_for_tasks(self):
        """Бот не даёт кликать, пока не выполнены задания: просим TaskRunner запустить
        цикл сейчас и ждём его конца (не дольше 40 минут). Лок бота тут НЕ держим."""
        requested = False
        if self.on_gate:
            try:
                requested = self.on_gate("клик: бот просит выполнить задания")
            except Exception as e:
                logger.warning(f"[{self.label}] on_gate: {e}")
        if not requested:
            await self.tg_logger.log("🎮 Задания запускались недавно — клик повторю после паузы")
            await asyncio.sleep(random.uniform(120, 240))
            return
        try:
            await asyncio.wait_for(self.core.tasks_cycle_done.wait(), 2400)
        except asyncio.TimeoutError:
            pass
        await human_delay(5, 12)

    async def _do_one_click(self, index: int, total: int):
        """-> (status, wait_sec). status: 'ok' | 'cooldown' | 'gate' | 'nobtn' | 'error' | 'unknown'.

        Раньше ЛЮБОЕ всплывающее окно логировалось как «кулдаун» и клик не
        засчитывался: успешный клик («✅ Ты получил(а) 0.10 ⭐️») не попадал ни в
        статистику, ни в БД. Теперь окно классифицируется (utils.classify_click_alert)."""
        try:
            async with self.core.bot_lock.acquire():
                # _open_unlocked сам решает капчу и проходит гейт спонсоров
                msg = await self.core._open_unlocked()
                if not msg or not msg.buttons:
                    await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: бот не ответил на /start", error="нет ответа")
                    return "error", 0

                clicker_btn = None
                for row in msg.buttons:
                    for btn in row:
                        if any(w in btn.text.lower() for w in ["кликер", "clicker", "farm", "✨"]):
                            clicker_btn = btn
                            break
                    if clicker_btn:
                        break

                if not clicker_btn:
                    labels = " | ".join(b.text for row in msg.buttons for b in row)[:120]
                    await self.tg_logger.log(
                        f"🎮 Клик {self._tag(index, total)}: кнопка кликера не найдена\n🧾 {(msg.text or '')[:60]}\n🔘 {labels}",
                        error="нет кнопки")
                    return "nobtn", 0

                since = utcnow()
                alert_text, _ = await self.core.click_button(clicker_btn)

                if alert_text:
                    kind = classify_click_alert(alert_text)
                    if kind == "cooldown":
                        wait = parse_cooldown_seconds(alert_text)
                        await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: кулдаун — {alert_text}")
                        return "cooldown", wait
                    if kind == "limit":
                        await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: дневной лимит кликов достигнут — {alert_text}")
                        return "limit", 0
                    if kind == "gate":
                        await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: бот просит выполнить задания — {alert_text}")
                        return "gate", 0
                    if kind != "ok":
                        await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: непонятный ответ бота — {alert_text}")
                        return "unknown", 0
                    # kind == "ok" — клик засчитан, идём дальше
                else:
                    # Алерта нет: по данным «Обучения» бот в этом случае показывает либо
                    # капчу («ПРОВЕРКА НА РОБОТА», captcha:clicker:N), либо рекламный экран
                    # (NFT/ГДЗ) вместо клика. Раньше это считалось «клик засчитан» — ложно.
                    resp = await self.core.wait_fresh(since, timeout=6)
                    first = ((resp.text or "").strip().splitlines() or [""])[0][:60] if resp else ""
                    if resp and "проверка" in (resp.text or "").lower():
                        solved = await self.core.captcha.solve_if_needed(resp)
                        await self.tg_logger.log(
                            f"🎮 Клик {self._tag(index, total)}: капча " + ("решена" if solved else "НЕ решена")
                            + ", награда не подтверждена ботом — в статистику не добавляю")
                        return "unknown", 0
                    await self.tg_logger.log(
                        f"🎮 Клик {self._tag(index, total)}: клик не засчитан — бот показал экран «{first}» вместо награды")
                    return "unknown", 0

                self.profile_stats.clicker_clicks_today += 1
                if self.db:
                    await self.db.event("click", stars=0.10)
                await self.tg_logger.log(
                    f"🎮 Клик {self._tag(index, total)} засчитан" + (f": {alert_text}" if alert_text else ""))

                if self.profile_stats.clicker_clicks_today % 10 == 0:
                    await self._refresh_balance()
                return "ok", 0

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.label}] Ошибка клика #{index}: {e}")
            self.profile_stats.add_error(f"кликер#{index}: {str(e)[:40]}")
            await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: исключение", error=str(e))
            return "error", 0

    def _target_bot(self):
        return TARGET_BOT

    async def _refresh_balance(self):
        """Вызывается ТОЛЬКО изнутри уже удержанного bot_lock (см. _do_one_click) —
        поэтому строго _open_unlocked(), а не open_bot() (иначе самодедлок)."""
        msg = await self.core._open_unlocked()
        if not msg or not msg.buttons:
            return
        for row in msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in ["профил", "profile"]):
                    since = utcnow()
                    await btn.click()
                    resp = await self.core.wait_fresh(since, timeout=6)
                    if resp:
                        await self.profile_stats.update_from_profile_text(resp.text or "")
                    return
