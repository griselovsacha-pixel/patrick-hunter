"""
DailyClaimer — сбор ежедневного бонуса.

Отдельно обрабатывает всплывашку "поставь свою личную ссылку в описание
профиля, чтобы получать +1⭐ каждый день": проставляет REFERRAL_LINK в bio
через BioManager, парсит из текста всплывашки "через сколько можно забрать"
и планирует следующую попытку ровно на это время.
"""
import asyncio
import datetime
import logging

from utils import human_delay, parse_next_daily_seconds, looks_like_bio_link_popup, solve_math
from core import utcnow

logger = logging.getLogger(__name__)


class DailyClaimer:
    def __init__(self, client, core, tg_logger, profile_stats, bio_manager, label: str, db=None):
        self.client = client
        self.core = core
        self.tg_logger = tg_logger
        self.profile_stats = profile_stats
        self.bio_manager = bio_manager
        self.label = label
        self.db = db

    async def collect_once(self) -> int:
        """Возвращает количество секунд до следующей попытки."""
        try:
            await self.tg_logger.log("📅 Начинаю сбор ежедневки...")

            for attempt in range(3):
                if attempt > 0:
                    await asyncio.sleep(10)

                async with self.core.bot_lock.acquire():
                    msg = await self.core._open_unlocked()
                    if not msg or not msg.buttons:
                        self.profile_stats.add_error("Ежедневка: бот не ответил")
                        continue

                    profile_clicked = False
                    since = utcnow()
                    for row in msg.buttons:
                        for btn in row:
                            if any(w in btn.text.lower() for w in ["профил", "profile"]):
                                await btn.click()
                                profile_clicked = True
                                break
                        if profile_clicked:
                            break
                    if not profile_clicked:
                        self.profile_stats.add_error("Ежедневка: профиль не найден")
                        continue

                    msg = await self.core.wait_fresh(since, timeout=8)
                    if msg:
                        await self.profile_stats.update_from_profile_text(msg.text or "")
                    if not msg or not msg.buttons:
                        self.profile_stats.add_error("Ежедневка: нет ответа после профиля")
                        continue

                    daily_clicked = False
                    daily_btn = None
                    for row in msg.buttons:
                        for btn in row:
                            if any(w in btn.text.lower() for w in ["еже", "daily", "бонус"]):
                                daily_btn = btn
                                daily_clicked = True
                                break
                        if daily_clicked:
                            break
                    if not daily_clicked:
                        self.profile_stats.add_error("Ежедневка: кнопка не найдена")
                        continue

                    # ГЛАВНЫЙ ФИКС: бот отвечает на эту кнопку через
                    # ВСПЛЫВАЮЩЕЕ ОКНО (Telegram callback-алерт), а не новым
                    # сообщением — см. скриншоты "Бонус можно получить раз в
                    # 24 часа! Осталось: Xч Yм". Раньше мы этот алерт вообще
                    # не читали, ждали новое сообщение (которое никогда не
                    # придёт), проваливались в фолбэк-опрос истории и там
                    # случайно ловили СТАРЫЙ текст экрана Профиля — из-за
                    # этого рождался ложный детект "всплывашка про био" при
                    # каждом клике, даже когда реальная причина — обычный
                    # суточный кулдаун.
                    since = utcnow()
                    alert_text, _ = await self.core.click_button(daily_btn)

                    if alert_text:
                        return await self._handle_alert(alert_text)

                    # Алерта не было (редкий случай, обычно сразу после
                    # рестарта — Telegram не всегда возвращает алерт на
                    # первый клик после реконнекта). Раньше здесь стояла
                    # проверка looks_like_bio_link_popup(text), но она
                    # оказалась ложной: text в этом фолбэке — это СТАРОЕ
                    # сообщение (экран Профиля), а не реальный ответ бота,
                    # и статичная подсказка "поставь ссылку в био" есть на
                    # этом экране ВСЕГДА. Из-за этого ссылка переставлялась
                    # заново на каждый такой случай, хотя она уже стояла.
                    # Теперь просто честно логируем и коротко ретраим —
                    # без спекулятивных действий по непроверенному тексту.
                    msg = await self.core.wait_fresh(since, timeout=8)
                    if not msg:
                        self.profile_stats.add_error("Ежедневка: нет ответа после нажатия")
                        continue

                    text = msg.text or ""

                    if looks_like_bio_link_popup(text):
                        await self.tg_logger.log(
                            f"⚠️ Не получил алерт-ответ на клик ежедневки, "
                            f"похоже на старый экран Профиля (не действие, просто лог):\n{text[:200]}"
                        )
                        wait_sec = 5 * 60
                        await self.tg_logger.log(f"⏳ Не смог понять ответ, перепроверю через {wait_sec // 60}м")
                        return wait_sec

                    if any(w in text.lower() for w in ["уже", "завтра", "получил", "следующ", "приходи", "через"]):
                        wait_sec = parse_next_daily_seconds(text)
                        h, m = wait_sec // 3600, (wait_sec % 3600) // 60
                        await self.tg_logger.log(f"ℹ️ Ежедневка уже забрана, следующая через {h}ч {m}м")
                        return wait_sec

                    if not msg.buttons:
                        self.profile_stats.add_error(f"Ежедневка: нет кнопок: {text[:80]}")
                        continue

                    if "проверка" in text.lower() or any(op in text for op in ["+", "-", "*", "/"]):
                        since2 = utcnow()
                        solved = await self.core.captcha.solve_if_needed(msg)
                        if solved:
                            resp = await self.core.wait_fresh(since2, timeout=8)
                            return await self._finish_collect(resp)
                        else:
                            self.profile_stats.add_error("Ежедневка: капча не решена")
                            continue

                    res = solve_math(text)
                    if res is not None:
                        clicked_math = False
                        for row in msg.buttons:
                            for btn in row:
                                if btn.text.strip() == str(res):
                                    since2 = utcnow()
                                    await btn.click()
                                    clicked_math = True
                                    break
                            if clicked_math:
                                break
                        if clicked_math:
                            resp = await self.core.wait_fresh(since2, timeout=8)
                            return await self._finish_collect(resp)
                        self.profile_stats.add_error(f"Ежедневка: ответ {res} не найден")
                        continue

                    self.profile_stats.add_error(f"Ежедневка: непонятный ответ: {text[:60]}")
                    continue

            await self.tg_logger.log("Ежедневка: все 3 попытки провалились", error="3 попытки")
            return 3600

        except Exception as e:
            logger.error(f"[{self.label}] Ошибка ежедневки: {e}")
            self.profile_stats.add_error(f"Ежедневка exception: {str(e)[:50]}")
            await self.tg_logger.log("Ошибка ежедневки", error=str(e))
            return 3600

    async def _handle_alert(self, alert_text: str) -> int:
        """Реальный ответ бота на кнопку ежедневки, полученный как
        всплывающее окно (см. click_button). Бывает ДВУХ типов, и это
        разные ситуации:

        1. Кулдаун: "Бонус можно получить раз в 24 часа! Осталось: Xч Yм"
           — ничего делать не нужно, просто ждём это время.
        2. Нет ссылки в био: "Сначала поставьте свою личную ссылку в
           описание или измените настройки конфиденциальности" — вот тут
           ДЕЙСТВИТЕЛЬНО нужно ставить ссылку (и перепроверять чаще, а не
           ждать полные сутки).

        Раньше это не различалось: код либо не читал алерт вообще (и путал
        с ним старый текст экрана Профиля), либо читал, но не проверял,
        ПРО ЧТО алерт, и одинаково молча ждал 24ч в обоих случаях."""
        if looks_like_bio_link_popup(alert_text):
            await self.tg_logger.log(f"📎 Бот просит ссылку в био (реальный алерт):\n{alert_text}")
            ok = await self.bio_manager.ensure_referral_bio()
            wait_sec = 10 * 60
            status = "поставил, перепроверю" if ok else "не смог поставить, попробую снова"
            await self.tg_logger.log(f"⏳ Ссылку в био {status} через {wait_sec // 60}м")
            return wait_sec

        await self.tg_logger.log(f"⏳ Ежедневка на кулдауне (реальный алерт бота):\n{alert_text}")
        return parse_next_daily_seconds(alert_text)

    async def _finish_collect(self, resp) -> int:
        resp_text = (resp.text or "") if resp else ""
        wait_sec = parse_next_daily_seconds(resp_text) if resp_text else 24 * 3600
        h, m = wait_sec // 3600, (wait_sec % 3600) // 60
        self.profile_stats.daily_collected_today = True
        if self.db:
            await self.db.event("daily_bonus", stars=1.0)
        if resp:
            await self.profile_stats.update_from_profile_text(resp_text)
        await self.tg_logger.log(f"📅 Ежедневка собрана!\n📩 {resp_text[:150]}\n⏳ Следующая через {h}ч {m}м")
        return wait_sec

    async def run_forever(self):
        while True:
            wait_sec = await self.collect_once()
            await asyncio.sleep(wait_sec)

