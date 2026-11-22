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

# warmup / settings / lease — служебные записи: дата начала прогрева, настройки аккаунта из админки
# (режим прокси, длительность прогрева), «аренда» сессии (защита от двух инстансов).
JOBS = ("daily", "tasks", "clicker", "crawler", "warmup", "settings", "lease")
HIDDEN_JOBS = ("warmup", "settings", "lease")
WARMUP_MAX_DAYS = 60
WARMUP_MIN_FACTOR = float(os.getenv("WARMUP_MIN_FACTOR", "0.2"))   # доля обычной нагрузки в первый день прогрева


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
        digits = "".join(ch for ch in label if ch.isdigit())
        self.number = int(digits or "1")
        # Прогрев теперь включается из админки (хранится в БД). Переменная
        # ACCOUNT_<N>_WARMUP_DAYS осталась как значение ПО УМОЛЧАНИЮ, пока в админке ничего не задано.
        try:
            self._env_warmup_days = max(0, int(os.getenv(f"ACCOUNT_{self.number}_WARMUP_DAYS", "0") or 0))
        except ValueError:
            self._env_warmup_days = 0

    # ---------- БД ----------
    async def load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            rows = await self.db.job_load() or []
        except Exception as e:
            logger.warning("[%s] расписание: не удалось прочитать БД (%s) — живём в памяти", self.label, e)
            rows = []
        for r in rows:
            st = self.state.get(r.get("job"))
            if st is None:
                continue
            st["next"] = _parse(r.get("next_run_at"))
            st["last"] = _parse(r.get("last_run_at"))
            st["status"] = r.get("last_status")
            st["message"] = r.get("last_message")
            st["data"] = r.get("data") or {}
        await self._warmup_boot()

    async def _save(self, job) -> bool:
        """True — запись попала в БД. Любой сбой БД не мешает работе (состояние живёт в памяти)."""
        st = self.state[job]
        try:
            return bool(await self.db.job_save(
                job,
                st["next"].isoformat() if st["next"] else None,
                st["last"].isoformat() if st["last"] else None,
                st["status"], st["message"], st["data"],
            ))
        except Exception as e:
            logger.debug("[%s] расписание: не сохранить %s: %s", self.label, job, e)
            return False

    # ---------- настройки аккаунта (из админки, переживают рестарт) ----------
    def setting(self, key, default=None):
        v = (self.state["settings"]["data"] or {}).get(key)
        return default if v is None else v

    async def set_settings(self, **kw) -> bool:
        """Меняет настройки аккаунта (None — удалить ключ). -> True, если сохранено в БД."""
        data = dict(self.state["settings"]["data"] or {})
        for k, v in kw.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        self.state["settings"]["data"] = data
        return await self._save("settings")

    # ---------- прогрев ----------
    @property
    def warmup_days(self) -> int:
        """Длительность прогрева в днях: из админки (БД), иначе из env. 0 — выключен."""
        v = self.setting("warmup_days")
        if v is not None:
            try:
                return max(0, min(int(v), WARMUP_MAX_DAYS))
            except (TypeError, ValueError):
                pass
        return self._env_warmup_days

    async def _warmup_boot(self):
        if self.warmup_days <= 0:
            return
        data = self.state["warmup"]["data"] or {}
        if not data.get("start"):
            data = {"start": _now().isoformat(), "days": self.warmup_days}
            self.state["warmup"]["data"] = data
            await self._save("warmup")
            logger.info("[%s] прогрев: старт, %d дн.", self.label, self.warmup_days)

    async def start_warmup(self, days: int) -> bool:
        """Из админки: включить прогрев на N дней, отсчёт — с этой минуты. days=0 — выключить.
        Нагрузка подхватывается на лету: задания — со следующего цикла, клики — со следующего слота."""
        days = max(0, min(int(days), WARMUP_MAX_DAYS))
        self.state["warmup"]["data"] = {"start": _now().isoformat(), "days": days} if days else {}
        await self._save("warmup")
        ok = await self.set_settings(warmup_days=days)
        logger.info("[%s] прогрев из админки: %s", self.label, f"{days} дн." if days else "выключен")
        return ok

    def warmup_factor(self) -> float:
        """Доля обычной нагрузки: 1.0 — полная (прогрев выключен или закончен)."""
        if self.warmup_days <= 0:
            return 1.0
        start = _parse((self.state["warmup"]["data"] or {}).get("start"))
        if not start:
            return WARMUP_MIN_FACTOR
        days = (_now() - start).total_seconds() / 86400
        if days >= self.warmup_days:
            return 1.0
        return WARMUP_MIN_FACTOR + (1.0 - WARMUP_MIN_FACTOR) * max(days, 0) / self.warmup_days

    def slowdown(self) -> float:
        """Во сколько раз реже запускать периодические задачи (1.0 — как обычно)."""
        return 1.0 + 2.0 * (1.0 - self.warmup_factor())

    def warmup_info(self) -> dict:
        f = self.warmup_factor()
        start = _parse((self.state["warmup"]["data"] or {}).get("start"))
        day = int((_now() - start).total_seconds() // 86400) + 1 if start else 1
        ends = start + datetime.timedelta(days=self.warmup_days) if (start and self.warmup_days) else None
        return {"enabled": self.warmup_days > 0, "active": f < 1.0, "days": self.warmup_days,
                "day": min(day, self.warmup_days) if self.warmup_days else 0,
                "factor": round(f, 2), "start": start.isoformat() if start else None,
                "ends_at": ends.isoformat() if ends else None,
                "from_admin": self.setting("warmup_days") is not None}

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
        if self.warmup_factor() < 1.0 and why != "по сохранённому расписанию":
            delay *= 1.0 + 5.0 * (1.0 - self.warmup_factor())
            why += ", прогрев — позже"
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
            if job in HIDDEN_JOBS:
                continue
            out[job] = {
                "next": st["next"].isoformat() if st["next"] else None,
                "last": st["last"].isoformat() if st["last"] else None,
                "status": st["status"], "message": st["message"],
                "running": st["running"],
                "manual": job in self.handlers,
            }
        return out
