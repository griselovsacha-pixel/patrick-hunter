"""
SessionLease — «аренда» сессии аккаунта: защита от двух одновременных инстансов.

Зачем. Одну и ту же StringSession нельзя держать подключённой из двух мест одновременно:
Telegram видит два клиента с одним ключом авторизации и может его УБИТЬ
(AuthKeyDuplicatedError) — аккаунт «слетает» и нужна новая сессия. На Render такое
случается при каждом деплое: новый инстанс стартует, пока старый ещё не остановлен.

Как работает. В таблице job_schedule (строка job='lease') хранится {boot, ts}: кто сейчас
держит сессию и когда в последний раз «отмечался» (каждые HEARTBEAT_SEC).
  * при старте новый инстанс ждёт, пока чужая отметка протухнет (STALE_SEC) или старый
    инстанс корректно освободит аренду при остановке;
  * работающий инстанс, увидев чужую СВЕЖУЮ отметку, останавливает аккаунт — лучше простой,
    чем потерянная сессия.
Без БД (нет Supabase) защита сводится к паузе LEASE_FALLBACK_WAIT_SEC перед стартом.
"""
import asyncio
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

BOOT_ID = uuid.uuid4().hex[:10]          # один на процесс
HEARTBEAT_SEC = 15
STALE_SEC = int(os.getenv("LEASE_STALE_SEC", "60"))
MAX_WAIT_SEC = int(os.getenv("LEASE_MAX_WAIT_SEC", "150"))
FALLBACK_WAIT_SEC = int(os.getenv("LEASE_FALLBACK_WAIT_SEC", "20"))
ENABLED = os.getenv("SESSION_LEASE", "1") != "0"


class SessionLease:
    def __init__(self, db, label: str, on_lost=None):
        self.db = db
        self.label = label
        self.on_lost = on_lost        # async callable(reason) — вызвать, если сессию забрал другой инстанс
        self._task = None
        self.lost = False

    async def _read(self):
        row = await self.db.job_get("lease")
        return (row or {}).get("data") or {}

    async def _write(self, **data):
        await self.db.job_save("lease", None, None, "ok", "", {"boot": BOOT_ID, "ts": time.time(), **data})

    @staticmethod
    def _foreign_fresh(data: dict) -> bool:
        if not data or data.get("boot") == BOOT_ID or data.get("released"):
            return False
        return time.time() - float(data.get("ts") or 0) < STALE_SEC

    async def acquire(self):
        """Дождаться, пока сессию не держит другой инстанс, и занять её."""
        if not ENABLED:
            return
        if not self.db.enabled or not self.db.session:
            logger.info("[%s] аренда сессии: БД нет — просто жду %d с (защита от перекрытия деплоев)",
                        self.label, FALLBACK_WAIT_SEC)
            await asyncio.sleep(FALLBACK_WAIT_SEC)
            return
        deadline = time.monotonic() + MAX_WAIT_SEC
        waited = False
        while True:
            try:
                data = await self._read()
            except Exception as e:
                logger.warning("[%s] аренда сессии: не прочитать БД (%s) — стартую без ожидания", self.label, e)
                return
            if not self._foreign_fresh(data):
                break
            if time.monotonic() > deadline:
                logger.error("[%s] аренда сессии: чужая отметка не гаснет %d с — беру принудительно",
                             self.label, MAX_WAIT_SEC)
                break
            if not waited:
                waited = True
                logger.warning("[%s] аренда сессии: сессию держит другой инстанс (старый деплой?) — жду", self.label)
            await asyncio.sleep(5)
        try:
            await self._write()
        except Exception as e:
            logger.warning("[%s] аренда сессии: не записать отметку: %s", self.label, e)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            try:
                data = await self._read()
                if self._foreign_fresh(data):
                    logger.error("[%s] аренда сессии: сессию забрал другой инстанс — останавливаю аккаунт", self.label)
                    self.lost = True   # аренда уже чужая — при остановке НЕ затираем чужую запись
                    if self.on_lost:
                        await self.on_lost("сессию забрал другой инстанс (двойной запуск)")
                    return
                await self._write()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[%s] аренда сессии: отметка: %s", self.label, e)

    async def release(self):
        """Вызывать ПОСЛЕ отключения клиента: новый инстанс сразу займёт сессию."""
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
        self._task = None
        if self.lost or not ENABLED or not self.db.enabled or not self.db.session:
            return
        try:
            await asyncio.wait_for(self._write(released=True), 5)
        except Exception as e:
            logger.debug("[%s] аренда сессии: не освободить: %s", self.label, e)
