"""
Расписание задач аккаунта, которое ПЕРЕЖИВАЕТ перезапуск (Render рестартует сервис
при каждом деплое). Раньше при старте всё запускалось сразу — задания, ежедневка,
серия пропущенных кликов, баланс, био — и это приводило к FloodWait.

Теперь для каждой задачи (daily, tasks, clicker, crawler) в таблице job_schedule
хранится next_run_at. После рестарта:
  * next_run_at в будущем        -> ждём ровно до него (как будто перезапуска не было);
  * next_run_at в прошлом        -> «пропущено за простой»: запускаем не мгновенно, а с
                                    разбросом SCHED_MISSED_MIN..MAX сек + сдвиг на аккаунт;
  * записи нет (первый запуск)   -> первый запуск через случайное окно first_range.
Плюс ручной запуск из админки (handlers + snapshot() для отображения).
Любой сбой БД не мешает работе: расписание тогда просто живёт в памяти.
"""
import datetime
import logging
import os
import random

logger = logging.getLogger(__name__)

MISSED_MIN = int(os.getenv("SCHED_MISSED_MIN_SEC", "180"))
MISSED_MAX = int(os.getenv("SCHED_MISSED_MAX_SEC", "900"))
ACCOUNT_STAGGER = int(os.getenv("SCHED_ACCOUNT_STAGGER_SEC", "240"))

JOBS = ("daily", "tasks", "clicker", "crawler")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


class JobScheduler:
    def __init__(self, db, label: str, index: int = 0):
        self.db = db
        self.label = label
        self.index = index
        self.state = {j: {"next": None, "last": None, "status": None, "message": None,
                          "running": False, "data": {}} for j in JOBS}
        self.handlers = {}   # job -> callable() -> bool (запустить сейчас)
        self._loaded = False

    # ---------- БД ----------
    async def load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            rows = await self.db.job_load() or []
        except Exception as e:
            logger.warning("[%s] расписание: не удалось прочитать БД (%s) — живём в памяти", self.label, e)
            return
        for r in rows:
            st = self.state.get(r.get("job"))
            if st is None:
                continue
            st["next"] = _parse(r.get("next_run_at"))
            st["last"] = _parse(r.get("last_run_at"))
            st["status"] = r.get("last_status")
            st["message"] = r.get("last_message")
            st["data"] = r.get("data") or {}

    async def _save(self, job):
        st = self.state[job]
        try:
            await self.db.job_save(
                job,
                st["next"].isoformat() if st["next"] else None,
                st["last"].isoformat() if st["last"] else None,
                st["status"], st["message"], st["data"],
            )
        except Exception as e:
            logger.debug("[%s] расписание: не сохранить %s: %s", self.label, job, e)

    # ---------- план ----------
    async def plan_initial(self, job: str, first_range=(300, 900)) -> float:
        """Сколько секунд ждать ПЕРЕД первым запуском задачи после старта процесса."""
        await self.load()
        st = self.state[job]
        now = _now()
        offset = self.index * ACCOUNT_STAGGER
        if st["next"] is None:
            delay = random.uniform(*first_range) + offset
            why = "первый запуск"
        elif st["next"] <= now:
            delay = random.uniform(MISSED_MIN, MISSED_MAX) + offset
            why = "пропущено за время простоя — запуск с разбросом"
        else:
            delay = (st["next"] - now).total_seconds()
            why = "по сохранённому расписанию"
        logger.info("[%s] расписание %s: через %d с (%s)", self.label, job, delay, why)
        await self.set_next(job, delay)
        return delay

    async def set_next(self, job: str, seconds: float):
        st = self.state[job]
        st["next"] = _now() + datetime.timedelta(seconds=max(0, seconds))
        await self._save(job)

    async def set_data(self, job: str, data: dict):
        self.state[job]["data"] = data
        await self._save(job)

    def get_data(self, job: str) -> dict:
        return self.state[job]["data"] or {}

    # ---------- статус выполнения ----------
    def begin(self, job: str):
        self.state[job]["running"] = True

    async def end(self, job: str, status: str = "ok", message: str = ""):
        st = self.state[job]
        st["running"] = False
        st["last"] = _now()
        st["status"] = status
        st["message"] = (message or "")[:300]
        await self._save(job)

    # ---------- ручной запуск ----------
    def trigger(self, job: str) -> bool:
        """True — запуск принят. False — задача уже идёт / нет обработчика."""
        st = self.state.get(job)
        h = self.handlers.get(job)
        if st is None or h is None or st["running"]:
            return False
        try:
            return bool(h())
        except Exception as e:
            logger.warning("[%s] ручной запуск %s: %s", self.label, job, e)
            return False

    def snapshot(self) -> dict:
        out = {}
        for job, st in self.state.items():
            out[job] = {
                "next": st["next"].isoformat() if st["next"] else None,
                "last": st["last"].isoformat() if st["last"] else None,
                "status": st["status"], "message": st["message"],
                "running": st["running"],
                "manual": job in self.handlers,
            }
        return out
