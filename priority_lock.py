"""
PriorityLock — лок с приоритетом для переписки с TARGET_BOT.

Промокоды должны обрабатываться максимально быстро, даже если прямо сейчас
идёт долгий цикл заданий. Обычный asyncio.Lock отдаёт лок строго в порядке
очереди (FIFO) — промокод мог реально ждать очень долго. Здесь high-priority
ожидающие (промокоды) встают в очередь ПЕРЕД любыми low-priority ожидающими
(задания/ежедневка/кликер), как только лок освобождается.

Лок у каждого аккаунта СВОЙ (см. account.py) — аккаунты друг друга не ждут.

yield_for(): держатель лока на время долгой паузы (человеческие задержки в
заданиях, ожидание мини-аппа 25-35с) может временно ОТДАТЬ лок, чтобы за это
время успел пройти промокод, а потом снова встать в очередь. Раньше промо
мог ждать до ~40с, пока задание "спит" внутри лока.
"""
import asyncio
import time
from contextlib import asynccontextmanager


class PriorityLock:
    def __init__(self):
        self._locked = False
        self._high_waiting = 0
        self._cv = asyncio.Condition()
        # Кто держит лок и с каким приоритетом — нужно защите аккаунта (protection.py): ожидание
        # FloodWait/КД внутри удерживаемого лока должно ОТДАВАТЬ лок, а не блокировать промокоды.
        self._owner = None
        self._owner_high = False
        self._held_since = None

    @property
    def high_waiting(self) -> int:
        return self._high_waiting

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def owner_high(self) -> bool:
        return self._owner_high

    def held_by_current(self) -> bool:
        """True, если лок сейчас держит именно эта задача."""
        if not self._locked or self._owner is None:
            return False
        try:
            return self._owner is asyncio.current_task()
        except RuntimeError:
            return False

    def held_seconds(self) -> float:
        """Сколько секунд лок удерживается непрерывно (0 — свободен). Для детектора зависаний."""
        return time.monotonic() - self._held_since if (self._locked and self._held_since) else 0.0

    def _mark_owner(self, high: bool):
        # вызывается из задачи-владельца СРАЗУ после _take (внутри _take current_task был бы другим
        # при asyncio.shield, поэтому владельца ставим снаружи)
        self._owner = asyncio.current_task()
        self._owner_high = high
        self._held_since = time.monotonic()

    async def _take(self, high_priority: bool):
        async with self._cv:
            if high_priority:
                self._high_waiting += 1
            try:
                while self._locked or (not high_priority and self._high_waiting > 0):
                    await self._cv.wait()
                self._locked = True
            finally:
                if high_priority:
                    self._high_waiting -= 1

    async def _release(self):
        async with self._cv:
            self._locked = False
            self._owner = None
            self._held_since = None
            self._cv.notify_all()

    @asynccontextmanager
    async def acquire(self, high_priority: bool = False):
        await self._take(high_priority)
        self._mark_owner(high_priority)
        try:
            yield
        finally:
            await self._release()

    async def yield_for(self, seconds: float, retake_high: bool = False):
        """Вызывать ТОЛЬКО из кода, который сейчас держит этот лок.

        Отдаёт лок, спит `seconds`, затем снова берёт его (по умолчанию с низким
        приоритетом — то есть после любых ожидающих промокодов). Если сон прерван
        отменой задачи, лок всё равно перебирается обратно, чтобы внешний
        `async with` корректно его отпустил.

        retake_high=True — для ожидания FloodWait внутри приоритетной операции (промокод):
        она встаёт обратно в очередь со своим высоким приоритетом.
        """
        await self._release()
        try:
            await asyncio.sleep(seconds)
        finally:
            await asyncio.shield(self._take(retake_high))
            self._mark_owner(retake_high)
