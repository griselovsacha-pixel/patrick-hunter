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
JOBS = ("daily", "tasks", "clicker", "crawler", "warmup", "settings", "lease", "protection")
HIDDEN_JOBS = ("warmup", "settings", "lease", "protection")
WARMUP_MAX_DAYS = 60
WARMUP_MIN_FACTOR = float(os.getenv("WARMUP_MIN_FACTOR", "0.2"))   # доля обычной нагрузки в первый день прогрева

# ---------- УЛЬТРА-прогрев ----------
# Для самых хрупких аккаунтов (виртуальные/азиатские номера, например Тайвань, свежие сессии, номера из
# «серых» источников): они замораживаются за малейший всплеск. Цель режима — не заработок, а выживание.
#   1) «Затишье» (lurk) — первые дни аккаунт только ЧИТАЕТ канал: ни заданий, ни кликов, ни ежедневки,
#      ни правки профиля. Промо/чеки — только по малому дневному лимиту.
#   2) Затем нагрузка растёт ПО КРИВОЙ (медленно в начале, быстрее к концу), а не линейно.
#   3) Всё медленнее: интервалы ×(1 + 6·(1−f)), строгий лимитер, дневные бюджеты вступлений/запусков ботов.
#   4) «Тихие часы» — ночью (по местному времени аккаунта) активности нет.
#   5) Строгий прокси: аварийный переход на IP Render ЗАПРЕЩЁН (смена страны IP — главный триггер заморозки).
WARMUP_MODES = ("normal", "ultra")
ULTRA_MAX_DAYS = 90
ULTRA_DEFAULT_DAYS = 30
ULTRA_MIN_FACTOR = float(os.getenv("ULTRA_MIN_FACTOR", "0.05"))
ULTRA_CURVE_POWER = float(os.getenv("ULTRA_CURVE_POWER", "1.7"))
ULTRA_SLOWDOWN_K = float(os.getenv("ULTRA_SLOWDOWN_K", "6"))
NORMAL_SLOWDOWN_K = 2.0
LURK_BLOCKED_JOBS = ("tasks", "clicker", "daily", "crawler")


def _parse_quiet(value: str):
    try:
        a, b = str(value).replace(" ", "").split("-", 1)
        a, b = int(a) % 24, int(b) % 24
        return (a, b) if a != b else None
    except Exception:
        return None


# Тихие часы ультра-режима: «с-по» по местному времени аккаунта (ULTRA_QUIET_HOURS=1-8; пусто — выключено).
ULTRA_QUIET = _parse_quiet(os.getenv("ULTRA_QUIET_HOURS", "1-8"))
DEFAULT_TZ_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "3"))


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
        self._env_warmup_mode = (os.getenv(f"ACCOUNT_{self.number}_WARMUP_MODE", "normal") or "normal").strip().lower()

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
    def warmup_mode(self) -> str:
        """'normal' | 'ultra'. Из админки (БД), иначе ACCOUNT_<N>_WARMUP_MODE из env."""
        m = (self.state["warmup"]["data"] or {}).get("mode") or self.setting("warmup_mode") or self._env_warmup_mode
        return m if m in WARMUP_MODES else "normal"

    @property
    def warmup_days(self) -> int:
        """Длительность прогрева в днях: из админки (БД), иначе из env. 0 — выключен."""
        cap = ULTRA_MAX_DAYS if self.warmup_mode == "ultra" else WARMUP_MAX_DAYS
        v = self.setting("warmup_days")
        if v is not None:
            try:
                return max(0, min(int(v), cap))
            except (TypeError, ValueError):
                pass
        return min(self._env_warmup_days, cap)

    @property
    def ultra(self) -> bool:
        """Ультра-режим включён для аккаунта (даже если прогрев уже закончился)."""
        return self.warmup_mode == "ultra" and self.warmup_days > 0

    @property
    def ultra_active(self) -> bool:
        """Ультра-режим включён И прогрев ещё идёт — именно тогда действуют все ультра-ограничения."""
        return self.ultra and self.warmup_factor() < 1.0

    def _warmup_start(self):
        return _parse((self.state["warmup"]["data"] or {}).get("start"))

    def warmup_elapsed_days(self) -> float:
        start = self._warmup_start()
        return max((_now() - start).total_seconds() / 86400, 0.0) if start else 0.0

    def lurk_days(self) -> float:
        """Длительность «затишья» ультра-режима, дней (≈10% прогрева, но 2..5)."""
        if not self.ultra:
            return 0.0
        d = (self.state["warmup"]["data"] or {}).get("lurk_days")
        if d is not None:
            try:
                return max(0.0, min(float(d), self.warmup_days / 2))
            except (TypeError, ValueError):
                pass
        return float(min(5, max(2, round(self.warmup_days * 0.1))))

    def lurking(self) -> bool:
        return self.ultra_active and self.warmup_elapsed_days() < self.lurk_days()

    def lurk_remaining(self) -> float:
        """Секунд до конца «затишья» (0 — не идёт)."""
        if not self.lurking():
            return 0.0
        return max(0.0, (self.lurk_days() - self.warmup_elapsed_days()) * 86400)

    def phase(self) -> str:
        """off | lurk | ramp | done."""
        if self.warmup_days <= 0:
            return "off"
        if self.warmup_factor() >= 1.0:
            return "done"
        return "lurk" if self.lurking() else "ramp"

    def tz_offset(self) -> int:
        """Часовой пояс аккаунта (для тихих часов): настройка из админки или общий TIMEZONE_OFFSET."""
        v = self.setting("tz_offset")
        try:
            return int(v) if v is not None else DEFAULT_TZ_OFFSET
        except (TypeError, ValueError):
            return DEFAULT_TZ_OFFSET

    def local_now(self) -> datetime.datetime:
        return _now() + datetime.timedelta(hours=self.tz_offset())

    def quiet_remaining(self) -> float:
        """Секунд до конца тихих часов (0 — сейчас не тихие или не ультра)."""
        if not (ULTRA_QUIET and self.ultra_active):
            return 0.0
        a, b = ULTRA_QUIET
        now = self.local_now()
        h = now.hour
        inside = (a <= h < b) if a < b else (h >= a or h < b)
        if not inside:
            return 0.0
        end = now.replace(hour=b, minute=0, second=0, microsecond=0)
        if end <= now:
            end += datetime.timedelta(days=1)
        return (end - now).total_seconds() + random.uniform(60, 2400)   # просыпаемся не ровно в «8:00»

    def block_seconds(self, job: str) -> float:
        """Сколько секунд задаче `job` НЕЛЬЗЯ стартовать из-за ультра-режима (затишье / тихие часы)."""
        if job not in LURK_BLOCKED_JOBS:
            return 0.0
        return max(self.lurk_remaining(), self.quiet_remaining())

    async def _warmup_boot(self):
        if self.warmup_days <= 0:
            return
        data = self.state["warmup"]["data"] or {}
        if not data.get("start"):
            data = {"start": _now().isoformat(), "days": self.warmup_days, "mode": self.warmup_mode}
            self.state["warmup"]["data"] = data
            await self._save("warmup")
            logger.info("[%s] прогрев (%s): старт, %d дн.", self.label, self.warmup_mode, self.warmup_days)

    async def start_warmup(self, days: int, mode: str = "normal") -> bool:
        """Из админки: включить прогрев на N дней, отсчёт — с этой минуты. days=0 — выключить.
        mode: 'normal' (линейный, от 20%) | 'ultra' (затишье + медленная кривая, для хрупких аккаунтов).
        Нагрузка подхватывается на лету: задания — со следующего цикла, клики — со следующего слота."""
        mode = mode if mode in WARMUP_MODES else "normal"
        cap = ULTRA_MAX_DAYS if mode == "ultra" else WARMUP_MAX_DAYS
        days = max(0, min(int(days), cap))
        self.state["warmup"]["data"] = ({"start": _now().isoformat(), "days": days, "mode": mode} if days else {})
        await self._save("warmup")
        ok = await self.set_settings(warmup_days=days, warmup_mode=mode if days else None)
        logger.info("[%s] прогрев из админки: %s", self.label,
                    f"{mode}, {days} дн." if days else "выключен")
        return ok

    def warmup_factor(self) -> float:
        """Доля обычной нагрузки: 1.0 — полная (прогрев выключен или закончен)."""
        if self.warmup_days <= 0:
            return 1.0
        start = self._warmup_start()
        ultra = self.warmup_mode == "ultra"
        floor = ULTRA_MIN_FACTOR if ultra else WARMUP_MIN_FACTOR
        if not start:
            return floor
        days = (_now() - start).total_seconds() / 86400
        if days >= self.warmup_days:
            return 1.0
        if not ultra:
            return floor + (1.0 - floor) * max(days, 0) / self.warmup_days
        lurk = min(5, max(2, round(self.warmup_days * 0.1)))
        d = (self.state["warmup"]["data"] or {}).get("lurk_days")
        try:
            lurk = max(0.0, min(float(d), self.warmup_days / 2)) if d is not None else lurk
        except (TypeError, ValueError):
            pass
        if days < lurk:
            return floor
        x = (days - lurk) / max(self.warmup_days - lurk, 0.001)
        return floor + (1.0 - floor) * (min(max(x, 0.0), 1.0) ** ULTRA_CURVE_POWER)

    def slowdown_for(self, f: float) -> float:
        k = ULTRA_SLOWDOWN_K if self.ultra_active else NORMAL_SLOWDOWN_K
        return 1.0 + k * (1.0 - f)

    def slowdown(self) -> float:
        """Во сколько раз реже запускать периодические задачи (1.0 — как обычно)."""
        return self.slowdown_for(self.warmup_factor())

    def warmup_info(self) -> dict:
        f = self.warmup_factor()
        start = self._warmup_start()
        day = int((_now() - start).total_seconds() // 86400) + 1 if start else 1
        ends = start + datetime.timedelta(days=self.warmup_days) if (start and self.warmup_days) else None
        mode = self.warmup_mode
        return {"enabled": self.warmup_days > 0, "active": f < 1.0, "days": self.warmup_days,
                "day": min(day, self.warmup_days) if self.warmup_days else 0,
                "factor": round(f, 2), "start": start.isoformat() if start else None,
                "ends_at": ends.isoformat() if ends else None,
                "from_admin": self.setting("warmup_days") is not None,
                "mode": mode if self.warmup_days > 0 else None,
                "phase": self.phase(),
                "lurk_days": self.lurk_days() if self.ultra else 0,
                "lurk_left_sec": int(self.lurk_remaining()),
                "quiet": f"{ULTRA_QUIET[0]:02d}-{ULTRA_QUIET[1]:02d}" if (ULTRA_QUIET and self.ultra) else None,
                "quiet_left_sec": int(self.quiet_remaining()),
                "slowdown": round(self.slowdown(), 2),
                "tz_offset": self.tz_offset()}

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
            delay *= 1.0 + (15.0 if self.ultra_active else 5.0) * (1.0 - self.warmup_factor())
            why += ", ультра-прогрев — позже" if self.ultra_active else ", прогрев — позже"
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
