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
from utils import human_delay
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
                        # Пропущенный слот (например, сервис перезапустился
                        # посреди дня на Render) — раньше он тут просто
                        # молча терялся (continue), и реальное число кликов
                        # за день оказывалось меньше 27-30. Теперь нагоняем
                        # сразу, с небольшой случайной паузой между
                        # пропущенными кликами, чтобы не бить их все разом.
                        await asyncio.sleep(random.uniform(3, 15))
                        await self._do_one_click(i + 1, times_today)
                        continue
                    await asyncio.sleep((click_time - now).total_seconds())
                    await self._do_one_click(i + 1, times_today)

                now = self._now_local()
                tomorrow_8 = (now + datetime.timedelta(days=1)).replace(
                    hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
                await asyncio.sleep((tomorrow_8 - now).total_seconds())

            except Exception as e:
                logger.error(f"[{self.label}] Ошибка кликера: {e}")
                await asyncio.sleep(3600)

    async def _do_one_click(self, index: int, total: int):
        try:
            async with self.core.bot_lock.acquire():
                since = utcnow()
                await self.client.send_message(self._target_bot(), '/start')
                msg = await self.core.wait_fresh(since, timeout=8)
                if not msg or not msg.buttons:
                    await self.tg_logger.log(f"🎮 Клик #{index}/{total}: бот не ответил на /start", error="нет ответа")
                    return

                if "проверка" in (msg.text or "").lower():
                    if not await self.core.captcha.solve_if_needed(msg):
                        await self.tg_logger.log(f"🎮 Клик #{index}/{total}: капча не решена", error="капча")
                        return
                    since = utcnow()
                    msg = await self.core.wait_fresh(since, timeout=6)

                clicker_btn = None
                if msg and msg.buttons:
                    for row in msg.buttons:
                        for btn in row:
                            if any(w in btn.text.lower() for w in ["кликер", "clicker", "farm", "✨"]):
                                clicker_btn = btn
                                break
                        if clicker_btn:
                            break

                if not clicker_btn:
                    await self.tg_logger.log(f"🎮 Клик #{index}/{total}: кнопка кликера не найдена", error="нет кнопки")
                    return

                # ТОТ ЖЕ ФИКС, что и в daily.py: бот может ответить на клик
                # через всплывающее окно (кулдаун), а не новым сообщением.
                # Раньше это игнорировалось — клик считался "успешным" по
                # факту нажатия, даже если реально бот отказал по кулдауну.
                # Именно это и выглядело как "клики не работают" — они
                # реально не засчитывались, но код бодро писал bы "собран".
                alert_text, _ = await self.core.click_button(clicker_btn)

                if alert_text:
                    await self.tg_logger.log(f"🎮 Клик #{index}/{total}: кулдаун — {alert_text}")
                    return  # не засчитываем клик — бот его не принял

                resp = await self.core.wait_fresh(since, timeout=6)
                if resp and "проверка" in (resp.text or "").lower():
                    if await self.core.captcha.solve_if_needed(resp):
                        since = utcnow()
                        resp = await self.core.wait_fresh(since, timeout=6)

                self.profile_stats.clicker_clicks_today += 1
                if self.db:
                    await self.db.event("click", stars=0.10)
                await self.tg_logger.log(f"🎮 Клик #{index}/{total} собран")

                if self.profile_stats.clicker_clicks_today % 10 == 0:
                    await self._refresh_balance()

        except Exception as e:
            logger.error(f"[{self.label}] Ошибка клика #{index}: {e}")
            self.profile_stats.add_error(f"кликер#{index}: {str(e)[:40]}")
            await self.tg_logger.log(f"🎮 Клик #{index}/{total}: исключение", error=str(e))

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
