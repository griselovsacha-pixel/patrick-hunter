"""
AccountProtection — единый слой защиты ОДНОГО аккаунта от заморозки (Account Protection Manager).

Раньше защита была россыпью локальных механизмов (safe_call здесь, паузы там), и между ними не было
общего состояния: одна корутина ловила FloodWait и спала, а соседние модули того же аккаунта
продолжали слать запросы, RateLimiter существовал, но нигде не использовался. Теперь у каждого
аккаунта есть ОДИН объект защиты, через который проходят все действия (safe_call → before/on_flood):

  1. Состояние аккаунта (машина состояний):
       ACTIVE → COOLDOWN (идёт КД после FloodWait/PeerFlood)
              → RECOVERING (после КД: пониженная нагрузка и строгий лимитер)
              → ACTIVE
       + OFFLINE (нет соединения) и AUTH_ERROR (сессия мертва/забанена).
     КД и «режим восстановления» сохраняются в БД (job_schedule/protection) — рестарт Render их не сбрасывает.
  2. Общий КД: FloodWait на любом действии останавливает ВСЕ некритичные действия аккаунта на это время.
     Промокоды/чеки (высокий приоритет) блокируются только если упёрлись в тот же самый метод.
  3. Эскалация: частые FloodWait → КД длиннее и режим восстановления; серия за сутки → «штрафной» режим
     (нагрузка вдвое ниже). PeerFlood (спам-блок) → КД на часы.
  4. RateLimiter с несколькими окнами (всплеск/минута/час); окна сужаются в прогреве, ультра-прогреве
     и режиме восстановления.
  5. Дневные бюджеты рискованных действий: вступления в каналы, запуск ботов, мини-аппы,
     «горячие» активации (промо+чеки). В ультра-прогреве бюджеты растут вместе с нагрузкой.
  6. Ожидание (КД/лимитер/FloodWait) внутри удерживаемого PriorityLock ОТДАЁТ лок — промокод не стоит в очереди.
  7. Детектор зависаний: операции циклов (задания/ежедневка/слот кликера) идут через run_op();
     если нет ни одного действия/паузы дольше FREEZE_STALL_SEC — операция принудительно прерывается.
  8. Человеческие паузы (delay) масштабируются прогревом, ультра-режимом, режимом восстановления и «нагревом».

Все пороги — env-переменные (значения по умолчанию ниже подобраны под Render free и 5 аккаунтов).
"""
import asyncio
import datetime
import logging
import math
import os
import random
import time

from antiban import (
    RateLimiter, CooldownActiveError, OperationFrozen, TooManyFloodWaitsError,
    HIGH_MAX_FLOOD_WAIT_SEC, _family,
)
from utils import human_delay

logger = logging.getLogger(__name__)

ACTIVE, COOLDOWN, RECOVERING, OFFLINE, AUTH_ERROR = "ACTIVE", "COOLDOWN", "RECOVERING", "OFFLINE", "AUTH_ERROR"
STATE_TITLES = {
    ACTIVE: "работает", COOLDOWN: "КД (FloodWait)", RECOVERING: "восстановление",
    OFFLINE: "нет связи", AUTH_ERROR: "сессия недействительна",
}


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _i(name, default):
    return int(_f(name, default))


# --- флуд и эскалация ---
ACCOUNT_COOLDOWN_MIN_WAIT = _i("PROTECT_ACCOUNT_CD_MIN_WAIT_SEC", 10)   # FloodWait короче — только локальная пауза
FLOODS_PER_HOUR_ESCALATE = _i("PROTECT_FLOODS_HOUR_ESCALATE", 3)       # столько FloodWait за час → длинный КД
ESCALATED_COOLDOWN_SEC = _i("PROTECT_ESCALATED_CD_SEC", 900)
FLOODS_PER_DAY_PENALTY = _i("PROTECT_FLOODS_DAY_PENALTY", 6)           # столько за сутки → штрафной режим на сутки
RECOVERY_AFTER_COOLDOWN_SEC = _i("PROTECT_RECOVERY_SEC", 1800)
RECOVERY_LONG_SEC = _i("PROTECT_RECOVERY_LONG_SEC", 7200)
PEER_FLOOD_COOLDOWN_SEC = _i("PROTECT_PEER_FLOOD_CD_SEC", 6 * 3600)
PEER_FLOOD_RECOVERY_SEC = _i("PROTECT_PEER_FLOOD_RECOVERY_SEC", 48 * 3600)
LONG_COOLDOWN_WAIT_SEC = _i("PROTECT_LONG_CD_NO_WAIT_SEC", 300)        # КД дольше — обычные действия не ждут, а отказывают
RECOVERY_LOAD_FACTOR = _f("PROTECT_RECOVERY_LOAD", 0.5)
ULTRA_RECOVERY_LOAD = _f("PROTECT_ULTRA_RECOVERY_LOAD", 0.3)

# --- лимитер (действий за 10 с / 60 с / 3600 с) ---
LIMITS_FULL = [(8, 10), (30, 60), (600, 3600)]
LIMITS_NORMAL_LOW = [(4, 10), (15, 60), (250, 3600)]    # начало обычного прогрева
LIMITS_ULTRA_LOW = [(2, 10), (6, 60), (40, 3600)]       # начало ультра-прогрева
YIELD_MIN_SEC = 8.0     # ожидание короче — просто спим; длиннее — отдаём лок бота

# --- бюджеты в сутки: (обычный режим, ультра при f=1, минимум ультра, в «затишье») ---
BUDGETS = {
    "join":      (_i("BUDGET_JOIN_DAY", 80),      25, 1, 0),
    "start_bot": (_i("BUDGET_START_BOT_DAY", 100), 25, 2, 0),
    "miniapp":   (_i("BUDGET_MINIAPP_DAY", 40),    8, 1, 0),
    "hot":       (None,                           12, 2, 2),     # промо + чеки: в обычном режиме без лимита
}
BUDGET_TITLES = {"join": "вступления", "start_bot": "запуск ботов", "miniapp": "мини-аппы", "hot": "промо/чеки"}

# --- зависания ---
FREEZE_STALL_SEC = _i("FREEZE_STALL_SEC", 480)
LOCK_HOLD_WARN_SEC = _i("LOCK_HOLD_WARN_SEC", 300)
MONITOR_INTERVAL_SEC = 20
SAVE_EVERY_SEC = 60

# --- пакеты попыток для промо/чеков ---
ULTRA_PROMO_ATTEMPTS = 3
ULTRA_PROMO_RETRY_RANGE = (2.5, 5.0)
ULTRA_CHECK_ATTEMPTS = 2

FLOOD_HISTORY_SEC = 86400


class AccountProtection:
    def __init__(self, label: str, sched, bot_lock, tg_logger=None):
        self.label = label
        self.sched = sched
        self.bot_lock = bot_lock
        self.tg_logger = tg_logger

        self.cooldown_until = 0.0     # epoch
        self.recovering_until = 0.0
        self.penalty_until = 0.0
        self.floods = []              # [{"ts", "sec", "action"}]
        self.blocked = {}             # семейство действия -> epoch, до которого оно заблокировано
        self.last_flood = None
        self.peer_flood_at = 0.0
        self.offline = False
        self.auth_error = None
        self._budget = {"day": "", "n": {}}

        self.limiter = RateLimiter(windows=LIMITS_FULL)
        self.calls = 0
        self.freezes = []             # последние зависания [{"op", "ts"}]
        self._ops = {}                # имя -> {"task", "last", "idle", "frozen"}
        self._dirty = False
        self._last_save = 0.0
        self._prev_state = ACTIVE
        self._monitor_task = None
        self._lock_warned = False

    # ------------------------------------------------------------------
    # Хранение (переживает рестарт)
    # ------------------------------------------------------------------
    async def load(self):
        d = self.sched.get_data("protection") if self.sched else {}
        if not isinstance(d, dict):
            d = {}
        now = time.time()
        self.cooldown_until = float(d.get("cooldown_until") or 0)
        self.recovering_until = float(d.get("recovering_until") or 0)
        self.penalty_until = float(d.get("penalty_until") or 0)
        self.peer_flood_at = float(d.get("peer_flood_at") or 0)
        self.floods = [x for x in (d.get("floods") or []) if now - float(x.get("ts") or 0) < FLOOD_HISTORY_SEC]
        self.last_flood = d.get("last_flood")
        b = d.get("budget") or {}
        if isinstance(b, dict) and b.get("day"):
            self._budget = {"day": b["day"], "n": dict(b.get("n") or {})}
        self._prev_state = self.state
        if self.cooldown_left() > 0:
            logger.warning("[%s] защита: после рестарта КД ещё %d с — соблюдаю", self.label, self.cooldown_left())

    async def save(self, force: bool = False):
        if not self.sched:
            return
        if not force and time.monotonic() - self._last_save < SAVE_EVERY_SEC:
            self._dirty = True
            return
        self._last_save = time.monotonic()
        self._dirty = False
        data = {
            "cooldown_until": self.cooldown_until, "recovering_until": self.recovering_until,
            "penalty_until": self.penalty_until, "peer_flood_at": self.peer_flood_at,
            "floods": self.floods[-60:], "last_flood": self.last_flood, "budget": self._budget,
        }
        try:
            await self.sched.set_data("protection", data)
        except Exception as e:
            logger.debug("[%s] защита: не сохранить состояние: %s", self.label, e)

    def _save_soon(self):
        """Из синхронного кода (on_flood вызывается без await): сохранить в фоне."""
        try:
            asyncio.get_running_loop().create_task(self.save(force=True))
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Состояние
    # ------------------------------------------------------------------
    @staticmethod
    def _now() -> float:
        return time.time()

    def cooldown_left(self) -> float:
        return max(0.0, self.cooldown_until - self._now())

    def recovering_left(self) -> float:
        return max(0.0, self.recovering_until - self._now())

    def penalty_left(self) -> float:
        return max(0.0, self.penalty_until - self._now())

    @property
    def state(self) -> str:
        if self.auth_error:
            return AUTH_ERROR
        if self.offline:
            return OFFLINE
        if self.cooldown_left() > 0:
            return COOLDOWN
        if self.recovering_left() > 0 or self.penalty_left() > 0:
            return RECOVERING
        return ACTIVE

    def set_offline(self, offline: bool):
        if self.offline != offline:
            self.offline = offline
            logger.info("[%s] защита: связь %s", self.label, "потеряна" if offline else "восстановлена")

    def set_auth_error(self, reason: str):
        self.auth_error = reason or "ошибка авторизации"

    def recent_floods(self, window_sec: float = 3600) -> int:
        edge = self._now() - window_sec
        return sum(1 for x in self.floods if float(x.get("ts") or 0) >= edge and not x.get("slow"))

    def heat(self) -> float:
        """«Нагрев» аккаунта: 1.0 — спокойно; растёт с недавними FloodWait, остывает за час."""
        return 1.0 + min(1.0, 0.25 * self.recent_floods(3600))

    # ------------------------------------------------------------------
    # Нагрузка / темп (то, что читают планировщики)
    # ------------------------------------------------------------------
    def load_factor(self) -> float:
        """Доля обычной нагрузки с учётом прогрева И режима восстановления/штрафа."""
        f = self.sched.warmup_factor() if self.sched else 1.0
        if self.recovering_left() > 0 or self.penalty_left() > 0 or self.cooldown_left() > 0:
            cap = ULTRA_RECOVERY_LOAD if (self.sched and self.sched.ultra) else RECOVERY_LOAD_FACTOR
            f = min(f, cap)
        return f

    def slowdown(self) -> float:
        """Во сколько раз реже запускать периодические задачи: прогрев/ультра + восстановление."""
        f = self.load_factor()
        return self.sched.slowdown_for(f) if self.sched else 1.0 + 2.0 * (1.0 - f)

    def min_cycle_tasks(self) -> int:
        return 2 if (self.sched and self.sched.ultra_active) else 4

    def pace_factor(self, fast: bool = False) -> float:
        """Множитель человеческих пауз. fast=True (промо/чеки): только режим/нагрев, без прогрева."""
        pace = self.heat()
        if self.recovering_left() > 0 or self.penalty_left() > 0:
            pace *= 1.5
        if self.sched and self.sched.ultra_active:
            pace *= 1.5 if fast else max(1.0, math.sqrt(self.sched.slowdown()))
        elif not fast and self.sched:
            pace *= max(1.0, self.sched.slowdown() ** 0.5 if self.sched.warmup_factor() < 1.0 else 1.0)
        return min(pace, 5.0)

    def promo_attempts(self, default: int) -> int:
        return min(default, ULTRA_PROMO_ATTEMPTS) if (self.sched and self.sched.ultra_active) else default

    def promo_retry_range(self, default):
        return ULTRA_PROMO_RETRY_RANGE if (self.sched and self.sched.ultra_active) else default

    def check_attempts(self, default: int) -> int:
        return min(default, ULTRA_CHECK_ATTEMPTS) if (self.sched and self.sched.ultra_active) else default

    def job_block_seconds(self, job: str) -> float:
        """Сколько секунд задаче нельзя стартовать: затишье/тихие часы ультра-режима или КД аккаунта."""
        w = self.sched.block_seconds(job) if self.sched else 0.0
        cd = self.cooldown_left()
        if cd > 0 and job in ("tasks", "clicker", "daily", "crawler"):
            w = max(w, cd + random.uniform(10, 90))
        return w

    # ------------------------------------------------------------------
    # Приоритет и ожидание
    # ------------------------------------------------------------------
    def is_high(self, high=None) -> bool:
        if high is not None:
            return bool(high)
        try:
            return self.bot_lock.held_by_current() and self.bot_lock.owner_high
        except Exception:
            return False

    def progress(self):
        """Метка «операция жива»: любое действие/пауза продлевает детектор зависаний."""
        t = self._cur_op()
        if t is not None:
            t["last"] = time.monotonic()

    def _cur_op(self):
        try:
            cur = asyncio.current_task()
        except RuntimeError:
            return None
        for op in self._ops.values():
            if op["task"] is cur:
                return op
        return None

    async def sleep(self, seconds: float, *, high: bool = False):
        """Ожидание (КД/лимитер/FloodWait). Если эта задача держит лок бота и ждать дольше
        YIELD_MIN_SEC — лок отдаётся, чтобы промокоды/чеки не стояли в очереди; потом берётся обратно
        с прежним приоритетом. Во время ожидания детектор зависаний «спит»."""
        if seconds <= 0:
            return
        op = self._cur_op()
        if op is not None:
            op["idle"] += 1
        try:
            if seconds > YIELD_MIN_SEC and self.bot_lock.held_by_current():
                await self.bot_lock.yield_for(seconds, retake_high=self.bot_lock.owner_high)
            else:
                await asyncio.sleep(seconds)
        finally:
            if op is not None:
                op["idle"] = max(0, op["idle"] - 1)
                op["last"] = time.monotonic()

    async def delay(self, lo: float, hi: float, *, fast: bool = False):
        """Человеческая пауза, масштабированная состоянием аккаунта (замена utils.human_delay)."""
        self.progress()
        await self.sleep(random.uniform(lo, hi) * self.pace_factor(fast), high=fast)

    # ------------------------------------------------------------------
    # Перед каждым действием
    # ------------------------------------------------------------------
    def _limits(self):
        f = self.sched.warmup_factor() if self.sched else 1.0
        if f >= 1.0:
            return LIMITS_FULL
        low = LIMITS_ULTRA_LOW if self.sched.ultra_active else LIMITS_NORMAL_LOW
        return [(max(1, round(a + (b - a) * f)), s) for (a, s), (b, _) in zip(low, LIMITS_FULL)]

    async def before(self, action: str, *, high=None, kind: str = "write"):
        """Вызывается из safe_call ПЕРЕД действием: общий КД → блокировка метода → лимитер."""
        self.calls += 1
        self.progress()
        if kind == "log":
            return
        is_high = self.is_high(high)
        fam = _family(action)

        until = self.blocked.get(fam, 0.0)
        if until and until <= self._now():
            self.blocked.pop(fam, None)
            until = 0.0
        own_wait = max(0.0, until - self._now())

        if kind == "read":
            if own_wait > 0:
                if is_high and own_wait > HIGH_MAX_FLOOD_WAIT_SEC:
                    raise CooldownActiveError(own_wait, action)
                await self.sleep(own_wait + random.uniform(0.5, 2), high=is_high)
            return

        if is_high:
            # приоритетные действия не ждут общий КД, а упираются только в свой метод
            if own_wait > HIGH_MAX_FLOOD_WAIT_SEC:
                raise CooldownActiveError(own_wait, action)
            if own_wait > 0:
                await self.sleep(own_wait + random.uniform(0.5, 2), high=True)
        else:
            wait = max(own_wait, self.cooldown_left())
            if wait > LONG_COOLDOWN_WAIT_SEC:
                raise CooldownActiveError(wait, action)
            if wait > 0:
                await self.sleep(wait + random.uniform(1, 4), high=False)

        self.limiter.set_windows(self._limits())
        rec = self.recovering_left() > 0 or self.penalty_left() > 0
        lim_wait = self.limiter.reserve(high=is_high, factor=0.5 if rec else 1.0)
        if lim_wait > 0:
            await self.sleep(lim_wait, high=is_high)

    def note_ok(self, action: str):
        self.progress()

    # ------------------------------------------------------------------
    # Флуд
    # ------------------------------------------------------------------
    def on_flood(self, action: str, seconds: int, *, slowmode: bool = False, high: bool = False):
        """Зарегистрировать FloodWait/SlowMode. SlowMode — это чужой чат, он блокирует только своё действие.
        Обычный FloodWait — «аккаунт перегрет»: общий КД, эскалация, режим восстановления."""
        now = self._now()
        fam = _family(action)
        self.floods.append({"ts": now, "sec": int(seconds), "action": action, "slow": bool(slowmode)})
        self.floods = [x for x in self.floods if now - float(x["ts"]) < FLOOD_HISTORY_SEC][-60:]
        self.last_flood = {"ts": now, "sec": int(seconds), "action": action, "slow": bool(slowmode)}
        self.blocked[fam] = max(self.blocked.get(fam, 0.0), now + seconds)
        if slowmode:
            self._save_soon()
            return

        n_hour, n_day = self.recent_floods(3600), self.recent_floods(86400)
        cooldown = 0.0
        if seconds >= ACCOUNT_COOLDOWN_MIN_WAIT or n_hour >= 2:
            cooldown = seconds + random.uniform(2, 8)
        recovery = 0.0
        if seconds >= 60:
            recovery = RECOVERY_AFTER_COOLDOWN_SEC
        if n_hour >= FLOODS_PER_HOUR_ESCALATE:
            cooldown = max(cooldown, ESCALATED_COOLDOWN_SEC + random.uniform(0, 120))
            recovery = max(recovery, RECOVERY_LONG_SEC)
        if n_day >= FLOODS_PER_DAY_PENALTY:
            self.penalty_until = max(self.penalty_until, now + 86400)
        if cooldown > 0:
            self.cooldown_until = max(self.cooldown_until, now + cooldown)
        if recovery > 0:
            self.recovering_until = max(self.recovering_until, max(self.cooldown_until, now) + recovery)
        logger.warning("[%s] защита: FloodWait %dс на «%s» (за час %d, за сутки %d) → КД %dс, состояние %s",
                       self.label, seconds, action, n_hour, n_day, int(self.cooldown_left()), self.state)
        self._notify(
            f"🛡 FloodWait {seconds}с на «{action}»: общий КД аккаунта {int(self.cooldown_left())}с, "
            f"дальше режим восстановления (нагрузка ниже)"
            + (f"\n⚠️ {n_hour} FloodWait за час — КД увеличен" if n_hour >= FLOODS_PER_HOUR_ESCALATE else "")
            + ("\n⚠️ Штрафной режим на сутки (нагрузка вдвое ниже)" if n_day >= FLOODS_PER_DAY_PENALTY else ""),
            error="FloodWait" if n_hour >= FLOODS_PER_HOUR_ESCALATE else "")
        self._save_soon()

    async def on_peer_flood(self, action: str, exc=None):
        """PeerFlood — Telegram ограничил аккаунт на запись (спам-блок). Самый опасный сигнал."""
        now = self._now()
        self.peer_flood_at = now
        self.cooldown_until = max(self.cooldown_until, now + PEER_FLOOD_COOLDOWN_SEC)
        self.recovering_until = max(self.recovering_until, now + PEER_FLOOD_COOLDOWN_SEC + PEER_FLOOD_RECOVERY_SEC)
        self.penalty_until = max(self.penalty_until, now + PEER_FLOOD_RECOVERY_SEC)
        self.last_flood = {"ts": now, "sec": PEER_FLOOD_COOLDOWN_SEC, "action": action, "peer": True}
        logger.error("[%s] защита: PeerFlood на «%s» — полный КД %d ч", self.label, action, PEER_FLOOD_COOLDOWN_SEC // 3600)
        self._notify(
            f"🚨 PeerFlood на «{action}»: Telegram ограничил аккаунт на запись. Все действия остановлены на "
            f"{PEER_FLOOD_COOLDOWN_SEC // 3600} ч, затем длительное восстановление. Проверьте @SpamBot.",
            error="PeerFlood", pin=True)
        await self.save(force=True)

    def note_flood(self, action: str, seconds: int):
        """Для модулей со своей обработкой FloodWait (watcher): учесть в нагреве, не меняя их ожидание."""
        self.on_flood(action, int(seconds), slowmode=False, high=True)

    def clear(self):
        """Ручной сброс КД/штрафов из админки (оператор знает, что делает)."""
        self.cooldown_until = self.recovering_until = self.penalty_until = 0.0
        self.blocked.clear()
        self.floods = []
        self._save_soon()

    def _notify(self, text: str, error: str = "", pin: bool = False):
        if self.tg_logger is None:
            return
        try:
            asyncio.get_running_loop().create_task(self.tg_logger.log(text, error=error, pin=pin))
        except RuntimeError:
            pass
        except TypeError:
            try:
                asyncio.get_running_loop().create_task(self.tg_logger.log(text, error=error))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Дневные бюджеты рискованных действий
    # ------------------------------------------------------------------
    def _roll_day(self):
        day = self.sched.local_now().strftime("%Y-%m-%d") if self.sched else datetime.date.today().isoformat()
        if self._budget.get("day") != day:
            self._budget = {"day": day, "n": {}}
            self._dirty = True

    def budget_cap(self, kind: str):
        """Дневной лимит действия (None — без лимита)."""
        base = BUDGETS.get(kind)
        if not base:
            return None
        normal, ultra_full, ultra_min, lurk_cap = base
        sched = self.sched
        if sched is not None and sched.ultra_active:
            cap = lurk_cap if sched.lurking() else max(ultra_min, round(ultra_full * sched.warmup_factor()))
        else:
            cap = normal
        if cap is not None and (self.recovering_left() > 0 or self.penalty_left() > 0):
            cap = max(1 if cap else 0, int(cap * 0.5))
        return cap

    def budget_used(self, kind: str) -> int:
        self._roll_day()
        return int(self._budget["n"].get(kind, 0))

    def budget_try(self, kind: str, *, high=None) -> bool:
        """Занять единицу дневного бюджета. False — на сегодня лимит исчерпан (действие пропустить).
        Приоритетным действиям (промо/чеки) бюджет joins/start_bot не мешает, но они тоже считаются."""
        self._roll_day()
        cap = self.budget_cap(kind)
        used = int(self._budget["n"].get(kind, 0))
        if cap is not None and used >= cap and not (self.is_high(high) and kind != "hot"):
            logger.info("[%s] защита: бюджет «%s» на сегодня исчерпан (%d/%d)", self.label, kind, used, cap)
            return False
        self._budget["n"][kind] = used + 1
        self._dirty = True
        return True

    def budget_refund(self, kind: str):
        n = int(self._budget["n"].get(kind, 0))
        if n > 0:
            self._budget["n"][kind] = n - 1

    # ------------------------------------------------------------------
    # Детектор зависаний
    # ------------------------------------------------------------------
    async def run_op(self, name: str, factory, stall_sec: float = None):
        """Выполнить операцию цикла (одно «заходим в бота и делаем дело»). Если у неё нет ни одного
        действия/паузы дольше stall_sec (и она не в штатном ожидании) — прерываем: иначе зависшая
        корутина молча держит лок бота и останавливает аккаунт, а Watchdog видит только соединение."""
        stall = stall_sec or FREEZE_STALL_SEC
        task = asyncio.current_task()
        rec = {"task": task, "last": time.monotonic(), "idle": 0, "frozen": False, "stall": stall}
        self._ops[name] = rec
        try:
            return await factory()
        except asyncio.CancelledError:
            if rec["frozen"]:
                try:
                    task.uncancel()
                except Exception:
                    pass
                self.freezes.append({"op": name, "ts": self._now()})
                self.freezes = self.freezes[-10:]
                raise OperationFrozen(f"{name}: нет прогресса {int(stall)} с — прервано")
            raise
        finally:
            if self._ops.get(name) is rec:
                self._ops.pop(name, None)

    def _check_freezes(self):
        now = time.monotonic()
        for name, op in list(self._ops.items()):
            if op["idle"] > 0 or op["frozen"]:
                continue
            if now - op["last"] > op["stall"]:
                op["frozen"] = True
                logger.error("[%s] защита: «%s» завис (нет прогресса %d с) — прерываю", self.label, name, int(now - op["last"]))
                self._notify(f"🧊 Операция «{name}» зависла (нет прогресса {int(now - op['last'])} с) — принудительно прервана, "
                             f"лок бота освобождён", error="зависание")
                op["task"].cancel()
        held = self.bot_lock.held_seconds()
        if held > LOCK_HOLD_WARN_SEC and not self._lock_warned:
            self._lock_warned = True
            logger.warning("[%s] защита: лок бота удерживается %d с подряд", self.label, int(held))
        elif held < 1:
            self._lock_warned = False

    # ------------------------------------------------------------------
    # Фоновый монитор (запускается из account.py под _supervised)
    # ------------------------------------------------------------------
    async def run_forever(self):
        while True:
            await asyncio.sleep(MONITOR_INTERVAL_SEC)
            try:
                self._check_freezes()
                self._roll_day()
                st = self.state
                if st != self._prev_state:
                    if self._prev_state == COOLDOWN and st in (RECOVERING, ACTIVE):
                        self._notify("🛡 КД аккаунта закончился — работаю с пониженной нагрузкой (восстановление)"
                                     if st == RECOVERING else "🛡 КД аккаунта закончился")
                    elif self._prev_state == RECOVERING and st == ACTIVE:
                        self._notify("🛡 Восстановление завершено — нагрузка обычная")
                    self._prev_state = st
                    self._dirty = True
                if self._dirty:
                    await self.save()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[%s] защита: монитор: %r", self.label, e)

    # ------------------------------------------------------------------
    # Для админки
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        self._roll_day()
        st = self.state
        sched = self.sched
        budgets = {}
        for kind in BUDGETS:
            budgets[kind] = {"title": BUDGET_TITLES[kind], "used": self.budget_used(kind), "cap": self.budget_cap(kind)}
        return {
            "state": st, "state_title": STATE_TITLES[st],
            "cooldown_left": int(self.cooldown_left()),
            "recovering_left": int(self.recovering_left()),
            "penalty_left": int(self.penalty_left()),
            "floods_1h": self.recent_floods(3600), "floods_24h": self.recent_floods(86400),
            "last_flood": self.last_flood,
            "peer_flood": bool(self.peer_flood_at and self._now() - self.peer_flood_at < PEER_FLOOD_COOLDOWN_SEC + PEER_FLOOD_RECOVERY_SEC),
            "load_factor": round(self.load_factor(), 2), "slowdown": round(self.slowdown(), 2),
            "pace": round(self.pace_factor(), 2),
            "limits": [{"max": n, "per_sec": s} for n, s in self._limits()],
            "limiter_load": self.limiter.load(),
            "budgets": budgets,
            "ops": [{"op": k, "idle_sec": int(time.monotonic() - v["last"]), "waiting": v["idle"] > 0} for k, v in self._ops.items()],
            "lock_held_sec": int(self.bot_lock.held_seconds()),
            "freezes": self.freezes[-5:], "calls": self.calls,
            "ultra": bool(sched and sched.ultra), "phase": sched.phase() if sched else "off",
            "auth_error": self.auth_error,
        }
