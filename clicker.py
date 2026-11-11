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
        self.on_gate = None  # callable: просьба к TaskRunner выполнить задания (см. account.py)

    def _now_local(self):
        return datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)

    async def run_forever(self):
        while True:
            try:
                now = self._now_local()

                if now.hour < CLICKER_START_HOUR:
                    wake_at = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
                    await asyncio.sleep((wake_at - now).total_seconds())
                    continue

                times_today = random.randint(CLICKER_MIN_PER_DAY, CLICKER_MAX_PER_DAY)
                self.target_today = times_today

                window_sec = (CLICKER_END_HOUR - CLICKER_START_HOUR) * 3600
                part_sec = window_sec // times_today
                day_start = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)

                click_times = []
                for i in range(times_today):
                    part_start = day_start + datetime.timedelta(seconds=i * part_sec)
                    rand_sec = random.randint(0, int(part_sec * 0.8))
                    click_time = part_start + datetime.timedelta(seconds=rand_sec + random.randint(0, 300))
                    midnight = day_start + datetime.timedelta(hours=16)
                    if click_time > midnight:
                        click_time = midnight - datetime.timedelta(seconds=random.randint(60, 600))
                    click_times.append(click_time)
                click_times.sort()

                for i, click_time in enumerate(click_times):
                    now = self._now_local()
                    if click_time <= now:
                        # Пропущенный слот (сервис перезапустился посреди дня на Render) —
                        # нагоняем, небольшими паузами. Кулдаун между кликами у бота
                        # ~6 минут, поэтому _click_slot сам ждёт его, а не сжигает слоты.
                        await asyncio.sleep(random.uniform(3, 15))
                    else:
                        await asyncio.sleep((click_time - now).total_seconds())
                    await self._click_slot(i + 1, times_today)

                now = self._now_local()
                tomorrow_8 = (now + datetime.timedelta(days=1)).replace(
                    hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
                await asyncio.sleep((tomorrow_8 - now).total_seconds())

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
                    await self.tg_logger.log(f"🎮 Клик #{index}/{total}: бот не ответил на /start", error="нет ответа")
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
                        f"🎮 Клик #{index}/{total}: кнопка кликера не найдена\n🧾 {(msg.text or '')[:60]}\n🔘 {labels}",
                        error="нет кнопки")
                    return "nobtn", 0

                since = utcnow()
                alert_text, _ = await self.core.click_button(clicker_btn)

                if alert_text:
                    kind = classify_click_alert(alert_text)
                    if kind == "cooldown":
                        wait = parse_cooldown_seconds(alert_text)
                        await self.tg_logger.log(f"🎮 Клик #{index}/{total}: кулдаун — {alert_text}")
                        return "cooldown", wait
                    if kind == "gate":
                        await self.tg_logger.log(f"🎮 Клик #{index}/{total}: бот просит выполнить задания — {alert_text}")
                        return "gate", 0
                    if kind != "ok":
                        await self.tg_logger.log(f"🎮 Клик #{index}/{total}: непонятный ответ бота — {alert_text}")
                        return "unknown", 0
                    # kind == "ok" — клик засчитан, идём дальше
                else:
                    resp = await self.core.wait_fresh(since, timeout=6)
                    if resp and "проверка" in (resp.text or "").lower():
                        if await self.core.captcha.solve_if_needed(resp):
                            await self.core.wait_fresh(utcnow(), timeout=6)

                self.profile_stats.clicker_clicks_today += 1
                if self.db:
                    await self.db.event("click", stars=0.10)
                await self.tg_logger.log(
                    f"🎮 Клик #{index}/{total} засчитан" + (f": {alert_text}" if alert_text else ""))

                if self.profile_stats.clicker_clicks_today % 10 == 0:
                    await self._refresh_balance()
                return "ok", 0

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.label}] Ошибка клика #{index}: {e}")
            self.profile_stats.add_error(f"кликер#{index}: {str(e)[:40]}")
            await self.tg_logger.log(f"🎮 Клик #{index}/{total}: исключение", error=str(e))
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
