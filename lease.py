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

Атомарность. Раньше «прочитал → проверил → записал» были тремя отдельными запросами: два инстанса,
стартующие одновременно, оба видели «свободно» и оба занимали сессию; а через MAX_WAIT_SEC аренда
захватывалась принудительно, даже если старый инстанс ЖИВ (heartbeat свежий) — это прямой путь к
AuthKeyDuplicatedError. Теперь:
  * строка создаётся INSERT ... ON CONFLICT DO NOTHING, а обновляется условным UPDATE
    (WHERE data->>boot = <прочитанный> AND data->>seq = <прочитанный>) — одна атомарная операция БД,
    из двух гонщиков выигрывает ровно один;
  * heartbeat — тоже условный UPDATE по СВОЕМУ boot: если аренду перехватили, запись не пройдёт,
    и аккаунт останавливается;
  * если чужая аренда остаётся СВЕЖЕЙ дольше MAX_WAIT_SEC (старый инстанс жив), сессию НЕ захватываем —
    SessionBusyError, и main.run_account повторит запуск позже (лучше простой, чем потерянная сессия).
"""
import asyncio
import logging
import os
import random
import time
import uuid

logger = logging.getLogger(__name__)

BOOT_ID = uuid.uuid4().hex[:10]          # один на процесс
HEARTBEAT_SEC = 15
STALE_SEC = int(os.getenv("LEASE_STALE_SEC", "60"))
MAX_WAIT_SEC = int(os.getenv("LEASE_MAX_WAIT_SEC", "150"))
FALLBACK_WAIT_SEC = int(os.getenv("LEASE_FALLBACK_WAIT_SEC", "20"))
ENABLED = os.getenv("SESSION_LEASE", "1") != "0"


class SessionBusyError(Exception):
    """Сессию всё ещё держит живой другой инстанс — запускаться нельзя."""


class SessionLease:
    def __init__(self, db, label: str, on_lost=None):
        self.db = db
        self.label = label
        self.on_lost = on_lost        # async callable(reason) — вызвать, если сессию забрал другой инстанс
        self._task = None
        self.lost = False
        self._held = False     # аренда нами занята (только тогда её можно освобождать/продлевать)
        self._seq = None       # номер нашей последней записи (для условного обновления)

    async def _read(self):
        row = await self.db.job_get("lease")
        return (row or {}).get("data") or {}, row is not None

    async def _cas_write(self, expect_boot, expect_seq, **extra) -> bool:
        """Атомарная запись аренды: проходит, только если запись не менялась с момента чтения."""
        seq = int(expect_seq or 0) + 1
        data = {"boot": BOOT_ID, "ts": time.time(), "seq": seq, **extra}
        ok = await self.db.job_cas("lease", expect_boot, expect_seq, data)
        if ok:
            self._seq = seq
        return ok

    async def _insert_first(self) -> bool:
        data = {"boot": BOOT_ID, "ts": time.time(), "seq": 1}
        ok = await self.db.job_insert_new("lease", data)
        if ok:
            self._seq = 1
        return ok

    @staticmethod
    def _foreign_fresh(data: dict) -> bool:
        if not data or data.get("boot") == BOOT_ID or data.get("released"):
            return False
        return time.time() - float(data.get("ts") or 0) < STALE_SEC

    async def acquire(self):
        """Дождаться, пока сессию не держит другой инстанс, и занять её атомарно."""
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
                data, exists = await self._read()
            except Exception as e:
                logger.warning("[%s] аренда сессии: не прочитать БД (%s) — стартую без ожидания", self.label, e)
                return
            if self._foreign_fresh(data):
                if time.monotonic() > deadline:
                    # живой чужой инстанс: принудительный захват = два клиента на одном ключе = потеря сессии
                    raise SessionBusyError(
                        f"сессию держит другой живой инстанс ({MAX_WAIT_SEC} с) — запуск отложен")
                if not waited:
                    waited = True
                    logger.warning("[%s] аренда сессии: сессию держит другой инстанс (старый деплой?) — жду", self.label)
                await asyncio.sleep(5)
                continue
            try:
                if not exists:
                    won = await self._insert_first()
                else:
                    won = await self._cas_write(data.get("boot"), data.get("seq"))
            except Exception as e:
                logger.warning("[%s] аренда сессии: не записать отметку: %s — стартую без аренды", self.label, e)
                return
            if won:
                break
            # проиграли гонку другому инстансу: перечитываем и решаем заново
            logger.warning("[%s] аренда сессии: параллельный захват, перепроверяю", self.label)
            await asyncio.sleep(random.uniform(1, 3))
        self._held = True
        self.lost = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            try:
                if await self._cas_write(BOOT_ID, self._seq):
                    continue
                # условное обновление не прошло: аренду кто-то изменил — смотрим, кто
                data, _exists = await self._read()
                if data.get("boot") == BOOT_ID:
                    self._seq = data.get("seq")     # разошёлся только номер записи — подхватываем и продолжаем
                    continue
                if self._foreign_fresh(data):
                    logger.error("[%s] аренда сессии: сессию забрал другой инстанс — останавливаю аккаунт", self.label)
                    self.lost = True   # аренда уже чужая — при остановке НЕ затираем чужую запись
                    self._held = False
                    if self.on_lost:
                        await self.on_lost("сессию забрал другой инстанс (двойной запуск)")
                    return
                # чужая, но протухшая/освобождённая запись (старый инстанс корректно ушёл) — забираем обратно
                await self._cas_write(data.get("boot"), data.get("seq"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[%s] аренда сессии: отметка: %s", self.label, e)

    async def release(self):
        """Вызывать ПОСЛЕ отключения клиента: новый инстанс сразу займёт сессию.
        Освобождаем только СВОЮ аренду (условно по boot) — чужую живую запись не затираем."""
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
        self._task = None
        if self.lost or not self._held or not ENABLED or not self.db.enabled or not self.db.session:
            return
        self._held = False
        try:
            await asyncio.wait_for(self._cas_write(BOOT_ID, self._seq, released=True), 5)
        except Exception as e:
            logger.debug("[%s] аренда сессии: не освободить: %s", self.label, e)
