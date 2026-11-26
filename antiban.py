"""
Анти-бан прослойка.

ЛЮБОЙ вызов Telegram API (клик по кнопке, отправка сообщения, вступление в канал)
может прилететь с FloodWaitError, если мы стучимся слишком часто. Через safe_call()
проходят все «живые» действия аккаунта. Что изменилось по сравнению с первой версией:

  * safe_call знает про AccountProtection (protection.py): перед каждым действием ждёт
    общий КД аккаунта и свою очередь в RateLimiter, а FloodWait регистрирует в общем
    состоянии — после него ВСЕ некритичные модули этого аккаунта притормаживают,
    а не только та корутина, что поймала ошибку;
  * ожидание FloodWait больше не держит PriorityLock: лок отдаётся на время паузы
    (промокоды и чеки не стоят в очереди), потом берётся обратно с прежним приоритетом;
  * PeerFlood (спам-блок на запись) — отдельный, длинный КД, а не «ещё одна попытка»;
  * RateLimiter теперь реально используется: несколько окон (всплеск / минута / час),
    приоритетные действия (промо/чеки) его почти не ждут.

Без protection (старые вызовы safe_call(...)) поведение прежнее: ждём и повторяем.
"""
import asyncio
import logging
import random
import time
from collections import deque

from telethon import errors as _tgerr
from telethon.errors import FloodWaitError, SlowModeWaitError

logger = logging.getLogger(__name__)

# Классы, которых может не быть в установленной версии Telethon, берём мягко.
PeerFloodError = getattr(_tgerr, "PeerFloodError", None)
FloodPremiumWaitError = getattr(_tgerr, "FloodPremiumWaitError", None)

FLOOD_ERRORS = tuple(c for c in (FloodWaitError, FloodPremiumWaitError) if c is not None)
SLOWMODE_ERRORS = (SlowModeWaitError,)
WAIT_ERRORS = FLOOD_ERRORS + SLOWMODE_ERRORS

# Если Telegram просит подождать дольше этого — обычная операция не ждёт «в закрытую»:
# она сдаётся (КД аккаунта при этом остаётся и пережидается планировщиком).
MAX_FLOOD_WAIT_SEC = 600
# Приоритетные действия (промокод/чек) ждут FloodWait не дольше этого: код всё равно сгорит.
HIGH_MAX_FLOOD_WAIT_SEC = 20


class TooManyFloodWaitsError(Exception):
    pass


class CooldownActiveError(TooManyFloodWaitsError):
    """У аккаунта идёт КД после FloodWait/PeerFlood: действие не выполняется.
    remaining — сколько секунд осталось. Циклы заданий/кликера/ежедневки ловят это
    исключение и просто переносят запуск на конец КД."""

    def __init__(self, remaining: float, why: str = ""):
        self.remaining = max(0.0, float(remaining))
        super().__init__(f"КД аккаунта ещё {int(self.remaining)}с" + (f" ({why})" if why else ""))


class OperationFrozen(Exception):
    """Операция зависла (нет прогресса дольше лимита) и была принудительно прервана."""


def _family(action: str) -> str:
    return (action or "").split(":", 1)[0]


async def safe_call(coro_factory, *, action: str = "", max_retries: int = 3, logger_fn=None,
                    protection=None, high=None, kind: str = "write"):
    """Оборачивает вызов Telegram API.

    coro_factory — функция БЕЗ аргументов, возвращающая корутину:
        await safe_call(lambda: client.send_message(...), protection=prot)
    (уже созданную корутину на ретрае не перезапустить).

    kind: 'write' — действие, которое считается в лимитере и ждёт общий КД;
          'read'  — чтение (без лимитера, ждёт только блокировку своего метода);
          'log'   — служебная отправка логов: ничего не ждёт, но FloodWait копится в «нагреве».
    high: None — определить автоматически (держим ли мы лок бота с высоким приоритетом).
    """
    attempt = 0
    while True:
        attempt += 1
        if protection is not None:
            await protection.before(action, high=high, kind=kind)
        try:
            result = await coro_factory()
            if protection is not None:
                protection.note_ok(action)
            return result
        except PeerFloodError_tuple() as e:
            if protection is not None:
                await protection.on_peer_flood(action, e)
            raise TooManyFloodWaitsError(f"{action}: PeerFlood (спам-ограничение аккаунта)") from e
        except WAIT_ERRORS as e:
            wait_sec = int(getattr(e, "seconds", 30) or 30)
            slow = isinstance(e, SLOWMODE_ERRORS)
            is_high = protection.is_high(high) if protection is not None else False
            msg = f"⏳ FloodWait на '{action}': {wait_sec}с (попытка {attempt}/{max_retries})"
            logger.warning(msg)
            if logger_fn:
                await logger_fn(msg)

            if protection is not None:
                protection.on_flood(action, wait_sec, slowmode=slow or kind == "log", high=is_high)
            limit = HIGH_MAX_FLOOD_WAIT_SEC if is_high else MAX_FLOOD_WAIT_SEC
            if wait_sec > limit or attempt >= max_retries:
                raise TooManyFloodWaitsError(f"{action}: {wait_sec}с — сдаюсь") from e
            pause = wait_sec + random.uniform(1, 3)
            if protection is not None:
                await protection.sleep(pause, high=is_high)   # лок бота на время паузы отдаётся
            else:
                await asyncio.sleep(pause)


def PeerFloodError_tuple():
    """except-кортеж, безопасный даже если класса PeerFloodError нет в этой версии Telethon."""
    return (PeerFloodError,) if PeerFloodError is not None else (_NeverRaised,)


class _NeverRaised(Exception):
    pass


class RateLimiter:
    """Скользящее ограничение частоты исходящих действий аккаунта по нескольким окнам сразу
    (например, 8 за 10 с, 30 за минуту, 600 за час).

    reserve() не спит сама — возвращает, сколько секунд нужно подождать, и сразу бронирует
    слот на это время. Так ожидание можно сделать там, где умеют отдавать PriorityLock
    (см. protection.sleep), и очередь ожидающих не «слипается» (в старой версии лимитер спал
    внутри своего asyncio.Lock и не перепроверял окно после сна).
    """

    def __init__(self, max_actions: int = None, per_seconds: float = None, windows=None):
        if windows is None:
            windows = [(max_actions, per_seconds)] if max_actions and per_seconds else []
        self.windows = [(max(1, int(n)), float(s)) for n, s in windows]
        self._stamps = deque()
        self._horizon = max((s for _, s in self.windows), default=0.0)

    def set_windows(self, windows):
        self.windows = [(max(1, int(n)), float(s)) for n, s in windows]
        self._horizon = max((s for _, s in self.windows), default=0.0)

    def _trim(self, now: float):
        edge = now - self._horizon - 1.0
        while self._stamps and self._stamps[0] < edge:
            self._stamps.popleft()

    def reserve(self, *, high: bool = False, high_max_wait: float = 3.0, factor: float = 1.0) -> float:
        """-> сколько секунд ждать перед действием (0 — можно сразу).
        high: приоритетное действие — окна для него шире в 3 раза и ждёт оно не дольше high_max_wait.
        factor: множитель окон (<1 — строже, например в режиме восстановления)."""
        now = time.monotonic()
        if not self.windows:
            return 0.0
        self._trim(now)
        mult = 3.0 if high else 1.0
        t = now
        for _ in range(8):   # сходимся: сдвиг по одному окну может нарушить другое
            moved = False
            for n, per in self.windows:
                cap = max(1, int(n * mult * factor))
                inside = [x for x in self._stamps if x > t - per]
                if len(inside) >= cap:
                    need = inside[len(inside) - cap] + per + 0.05
                    if need > t:
                        t = need
                        moved = True
            if not moved:
                break
        wait = max(0.0, t - now)
        if high and wait > high_max_wait:
            wait = 0.0
            t = now
        self._stamps.append(t)
        return wait

    def load(self) -> dict:
        """Сколько действий в каждом окне сейчас (для админки)."""
        now = time.monotonic()
        self._trim(now)
        return {int(per): sum(1 for x in self._stamps if now - per < x <= now) for _, per in self.windows}

    async def acquire(self, *, high: bool = False):
        """Совместимость со старым интерфейсом: подождать и пройти."""
        wait = self.reserve(high=high)
        if wait > 0:
            await asyncio.sleep(wait)
