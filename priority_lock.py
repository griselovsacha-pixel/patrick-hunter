"""
PriorityLock — лок с приоритетом для переписки с TARGET_BOT.

Промокоды должны обрабатываться максимально быстро, даже если прямо сейчас
идёт долгий цикл заданий. Обычный asyncio.Lock отдаёт лок строго в порядке
очереди (FIFO) — промокод мог реально ждать очень долго. Здесь high-priority
ожидающие (промокоды) встают в очередь ПЕРЕД любыми low-priority ожидающими
(задания/ежедневка/кликер), как только лок освобождается — то есть максимум
ждём завершения ТЕКУЩЕГО шага, а не всей очереди обычных задач.
"""
import asyncio
from contextlib import asynccontextmanager


class PriorityLock:
    def __init__(self):
        self._locked = False
        self._high_waiting = 0
        self._cv = asyncio.Condition()

    @asynccontextmanager
    async def acquire(self, high_priority: bool = False):
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
        try:
            yield
        finally:
            async with self._cv:
                self._locked = False
                self._cv.notify_all()
