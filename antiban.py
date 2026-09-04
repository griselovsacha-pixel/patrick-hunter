"""
Анти-бан прослойка.

Идея простая: ЛЮБОЙ вызов Telegram API (клик по кнопке, отправка сообщения,
вступление в канал) может прилететь с FloodWaitError, если мы стучимся
слишком часто. Если это не обработать — либо упадёт таск, либо (что хуже)
аккаунт словит более серьёзные ограничения при систематическом игноре
FloodWait. Через safe_call() проходят все "живые" действия аккаунта.
"""
import asyncio
import logging
import random

from telethon.errors import FloodWaitError, SlowModeWaitError

logger = logging.getLogger(__name__)

# Если Telegram просит подождать дольше этого — не долбимся в закрытую
# дверь бесконечно, сдаёмся и даём знать наверх (пусть решает вызывающий код).
MAX_FLOOD_WAIT_SEC = 600


class TooManyFloodWaitsError(Exception):
    pass


async def safe_call(coro_factory, *, action: str = "", max_retries: int = 3, logger_fn=None):
    """Оборачивает вызов Telegram API с обработкой FloodWait/SlowModeWait.

    coro_factory: функция БЕЗ аргументов, возвращающая корутину — то есть
    вызывать нужно как `await safe_call(lambda: client.send_message(...))`,
    а не передавать уже awaited корутину (её нельзя перезапустить на ретрае).
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await coro_factory()
        except (FloodWaitError, SlowModeWaitError) as e:
            wait_sec = getattr(e, 'seconds', 30)
            msg = f"⏳ FloodWait на '{action}': ждём {wait_sec}с (попытка {attempt}/{max_retries})"
            logger.warning(msg)
            if logger_fn:
                await logger_fn(msg)
            if wait_sec > MAX_FLOOD_WAIT_SEC or attempt >= max_retries:
                raise TooManyFloodWaitsError(f"{action}: {wait_sec}с — сдаюсь") from e
            await asyncio.sleep(wait_sec + random.uniform(1, 3))
        except Exception:
            raise


class RateLimiter:
    """Простой лимитер: не даёт делать больше N действий за окно времени,
    даже если весь остальной код теоретически мог бы. Общая подстраховка
    поверх пауз/локов, которые уже расставлены по циклам."""

    def __init__(self, max_actions: int, per_seconds: float):
        self.max_actions = max_actions
        self.per_seconds = per_seconds
        self._timestamps = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            self._timestamps = [t for t in self._timestamps if now - t < self.per_seconds]
            if len(self._timestamps) >= self.max_actions:
                wait = self.per_seconds - (now - self._timestamps[0])
                if wait > 0:
                    await asyncio.sleep(wait)
            self._timestamps.append(loop.time())
