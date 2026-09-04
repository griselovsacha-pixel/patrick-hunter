"""
PromoHunter — детект промокода в канале и его активация.

Скорость решается ДВУМЯ вещами одновременно:
  1. Детект событийный (events.NewMessage/MessageEdited) — реагируем в
     момент публикации поста, без опроса.
  2. Активация идёт с high_priority=True на bot_lock — не стоит в очереди
     за циклом заданий (см. priority_lock.py), плюс несколько быстрых
     попыток подряд (PROMO_MAX_ATTEMPTS) с маленькой паузой вместо
     "1 раз и сдался" или "спам без пауз и мгновенный бан".
"""
import datetime
import asyncio
import re
import logging

from telethon import events

from config import (
    CHANNEL_SOURCE, TARGET_BOT, PROMO_KEYWORDS,
    PROMO_MAX_ATTEMPTS, PROMO_RETRY_DELAY_RANGE,
)
from utils import find_promo_in_text, human_delay
from antiban import safe_call, TooManyFloodWaitsError
from core import utcnow

logger = logging.getLogger(__name__)


class PromoHunter:
    def __init__(self, client, core, tg_logger, profile_stats, label: str):
        self.client = client
        self.core = core
        self.tg_logger = tg_logger
        self.profile_stats = profile_stats
        self.label = label

        self.activated_promos = set()
        self.promo_in_progress = set()
        self._channel_handler = None

    def register_channel_handler(self):
        @self.client.on(events.NewMessage(chats=CHANNEL_SOURCE))
        @self.client.on(events.MessageEdited(chats=CHANNEL_SOURCE))
        async def handle_channel(event):
            raw = event.raw_text or ""
            logger.info(f"📨 [{self.label}] Канал: {raw[:120]}")

            code = find_promo_in_text(raw)
            if code:
                logger.info(f"🔔 [{self.label}] Промокод найден: {code}")
                asyncio.create_task(self.activate(code))
            elif any(w in raw.lower() for w in PROMO_KEYWORDS):
                await self.tg_logger.log(f"Пост с промо, но код не распознан: {raw[:200]}", error="код не найден")

        self._channel_handler = handle_channel

    async def activate(self, code: str):
        if code.lower() in self.activated_promos or code.lower() in self.promo_in_progress:
            return

        self.promo_in_progress.add(code.lower())
        await self.tg_logger.log(f"🚀 Активирую промокод: `{code}`")
        try:
            success = False
            for attempt in range(1, PROMO_MAX_ATTEMPTS + 1):
                try:
                    success = await self._attempt(code)
                except TooManyFloodWaitsError as e:
                    await self.tg_logger.log(f"Промокод `{code}`: слишком много FloodWait", error=str(e))
                    break
                if success:
                    break
                logger.warning(f"⚠️ [{self.label}] Промокод {code}: попытка #{attempt} неудачна")
                if attempt < PROMO_MAX_ATTEMPTS:
                    await human_delay(*PROMO_RETRY_DELAY_RANGE)

            if success:
                self.activated_promos.add(code.lower())
                self.profile_stats.promos_activated_today.append(code)
                if len(self.activated_promos) > 50:
                    self.activated_promos = set(list(self.activated_promos)[-20:])
            else:
                self.profile_stats.add_error(f"промо {code}: все попытки неудачны")
                await self.tg_logger.log(f"Промокод `{code}`: все {PROMO_MAX_ATTEMPTS} попыток неудачны", error="исчерпаны попытки")
        finally:
            self.promo_in_progress.discard(code.lower())

    async def _attempt(self, code: str) -> bool:
        """Одна попытка активации. True = терминальный исход (успех ИЛИ
        явный отказ бота типа 'неверный/просрочен') — ретраить бессмысленно."""
        async with self.core.bot_lock.acquire(high_priority=True):
            msg = await self.core._open_unlocked()
            if not msg or not msg.buttons:
                return False

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
                return False

            msg = await self.core.wait_fresh(since, timeout=8)
            if not msg or not msg.buttons:
                return False

            promo_clicked = False
            since = utcnow()
            for row in msg.buttons:
                for btn in row:
                    if "промо" in btn.text.lower():
                        await btn.click()
                        promo_clicked = True
                        break
                if promo_clicked:
                    break
            if not promo_clicked:
                return False

            msg2 = await self.core.wait_fresh(since, timeout=8)
            if not msg2:
                return False

            since = utcnow()
            await safe_call(lambda: self.client.send_message(TARGET_BOT, code), action="send_promo_code")
            resp = await self.core.wait_fresh(since, timeout=8)
            if not resp:
                return False

            resp_text = resp.text or ""

            if any(w in resp_text.lower() for w in
                   ["неверн", "не найден", "истёк", "истек", "уже использован", "invalid", "expired"]):
                await self.tg_logger.log(f"Промокод `{code}` отклонён ботом: {resp_text[:150]}", error="отклонён")
                return True  # терминально — повторять бессмысленно

            if resp.buttons:
                for r in resp.buttons:
                    for b in r:
                        url = getattr(b, 'url', None)
                        b_text = b.text.lower()
                        if url:
                            tme = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                            if tme and not tme.group(1).lower().endswith('bot'):
                                await self.core.safe_join(tme.group(1))
                        if any(w in b_text for w in ["подписал", "проверить", "готово", "я подписал", "check"]):
                            await human_delay(2, 3)
                            await b.click()

            await self.tg_logger.log(f"✅ Промокод `{code}` активирован!\n📩 {resp_text[:100]}")

            # Обновляем реальный баланс из профиля после успеха.
            # ВАЖНО: лок уже взят этим же async with выше — self.core.open_bot()
            # тоже берёт лок и НЕ реентерабелен, вызов был бы самодедлоком
            # (бот бы навсегда завис после первого успешного промокода).
            # Поэтому здесь строго _open_unlocked(), а не open_bot().
            profile_msg = await self.core._open_unlocked()
            await self._refresh_balance(profile_msg)
            return True

    async def _refresh_balance(self, msg):
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
