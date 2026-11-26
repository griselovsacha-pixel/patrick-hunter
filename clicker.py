"""
Clicker — 27-30 случайных кликов в день в окне 8:00-00:00.
Каждые 10 кликов заходим в Профиль и обновляем реальный баланс (см.
profile_stats.py) вместо оценки "с потолка".
"""
import asyncio
import datetime
import logging
import random
import time

from config import (
    TIMEZONE_OFFSET, CLICKER_MIN_PER_DAY, CLICKER_MAX_PER_DAY,
    CLICKER_START_HOUR, CLICKER_END_HOUR, TARGET_BOT,
)
from utils import classify_click_alert, parse_cooldown_seconds
from core import utcnow
from antiban import CooldownActiveError, OperationFrozen
from knowledge import is_ad

logger = logging.getLogger(__name__)

# КД бота дольше этого — слот не теряем, а переносим на конец КД (раньше при КД > 15 мин повтор
# вообще не планировался и слот молча пропадал).
SHORT_COOLDOWN_MAX_SEC = 900
MAX_SLOT_RESCHEDULES = 3
# Слот, который «опоздал» (ждали КД/задания/затишье) больше чем на это — не выполняем залпом
# вместе со следующими, а переносим с разбросом.
LATE_SLOT_GRACE_SEC = 300
LATE_SLOT_RESPACE_RANGE = (240, 600)


class Clicker:
    def __init__(self, client, core, tg_logger, profile_stats, label: str, db=None):
        self.client = client
        self.core = core
        self.tg_logger = tg_logger
        self.profile_stats = profile_stats
        self.label = label
        self.db = db
        self.target_today = 0
        self._manual = asyncio.Event()
        self.sched = None  # scheduler.JobScheduler, ставится из account.py
        self._retry_wait = 0.0   # длинный КД бота, с которым слот надо перенести
        self.on_gate = None  # callable: просьба к TaskRunner выполнить задания (см. account.py)

    def _now_local(self):
        return datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)

    def request(self) -> bool:
        """Ручной клик из админки (вне плана дня)."""
        self._manual.set()
        return True

    def _tag(self, index, total) -> str:
        return "ручной" if index == "ручной" else f"#{index}/{total}"

    async def _wait_until(self, when_local):
        """Спим до времени слота; ручной клик из админки выполняется по ходу ожидания."""
        while True:
            secs = (when_local - self._now_local()).total_seconds()
            if secs <= 0:
                return
            await self.sched.set_next("clicker", secs)
            try:
                await asyncio.wait_for(self._manual.wait(), timeout=secs)
            except asyncio.TimeoutError:
                continue
            self._manual.clear()
            await self._manual_click()

    async def _manual_click(self):
        self.sched.begin("clicker")
        status = "error"
        try:
            status = await self._run_slot("ручной", self.target_today or 0)
        finally:
            await self.sched.end("clicker", status, "ручной клик")

    def _slot_active(self, i: int, total: int) -> bool:
        """Прогрев: из `total` слотов дня выполняем долю warmup_factor, равномерно по дню."""
        prot = self.core.protection
        f = prot.load_factor() if prot is not None else self.sched.warmup_factor()
        if f >= 1.0:
            return True
        return int((i + 1) * f + 1e-9) > int(i * f + 1e-9)

    def _make_plan(self, now):
        # План дня всегда полный; прогрев «прореживает» слоты на лету (см. _slot_active), поэтому его
        # можно включить/выключить из админки в любой момент — без пересоздания плана.
        times_today = random.randint(CLICKER_MIN_PER_DAY, CLICKER_MAX_PER_DAY)
        window_sec = (CLICKER_END_HOUR - CLICKER_START_HOUR) * 3600
        part_sec = window_sec // times_today
        day_start = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
        midnight = day_start + datetime.timedelta(hours=CLICKER_END_HOUR - CLICKER_START_HOUR)
        times = []
        for i in range(times_today):
            part_start = day_start + datetime.timedelta(seconds=i * part_sec)
            t = part_start + datetime.timedelta(seconds=random.randint(0, int(part_sec * 0.8)) + random.randint(0, 300))
            if t > midnight:
                t = midnight - datetime.timedelta(seconds=random.randint(60, 600))
            times.append(t)
        times.sort()
        return {"date": now.strftime("%Y-%m-%d"), "times": [t.isoformat() for t in times], "done": 0}

    async def run_forever(self):
        # После рестарта план дня берём из БД и НЕ нагоняем пропущенные слоты пачкой
        # (раньше это был залп кликов -> FloodWait). Пропущенные просто засчитываются пропущенными.
        await self.sched.load()
        first = True
        while True:
            try:
                now = self._now_local()
                if now.hour < CLICKER_START_HOUR:
                    await self._wait_until(now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0))
                    continue

                plan = self.sched.get_data("clicker")
                if plan.get("date") != now.strftime("%Y-%m-%d") or not plan.get("times"):
                    plan = self._make_plan(now)
                    await self.sched.set_data("clicker", plan)
                times = [datetime.datetime.fromisoformat(t) for t in plan["times"]]
                self.target_today = sum(1 for k in range(len(times)) if self._slot_active(k, len(times)))
                self.profile_stats.clicker_clicks_today = max(self.profile_stats.clicker_clicks_today, plan["done"])

                if first:
                    first = False
                    # пропущенные за простой слоты (позже 5 минут назад) — пропускаем, не догоняем
                    idx = plan["done"]
                    while idx < len(times) and times[idx] < now - datetime.timedelta(minutes=5):
                        idx += 1
                    skipped = idx - plan["done"]
                    if skipped:
                        await self.tg_logger.log(f"🎮 Пропущено слотов за время простоя: {skipped} (не нагоняю, чтобы не словить FloodWait)")
                        plan["done"] = idx
                        await self.sched.set_data("clicker", plan)
                    # первый после рестарта слот — с разбросом и сдвигом на аккаунт, не мгновенно
                    if plan["done"] < len(times) and times[plan["done"]] <= now:
                        times[plan["done"]] = now + datetime.timedelta(
                            seconds=random.uniform(240, 600) + self.sched.index * 240)

                day_end = datetime.datetime.strptime(plan["date"], "%Y-%m-%d") + datetime.timedelta(hours=CLICKER_END_HOUR)
                while plan["done"] < len(times):
                    # прогрев: пропускаем лишние слоты (не ждём их времени)
                    thinned = 0
                    while plan["done"] < len(times) and not self._slot_active(plan["done"], len(times)):
                        plan["done"] += 1
                        thinned += 1
                    if thinned:
                        self.target_today = max(1, len(times) - thinned)
                        await self.sched.set_data("clicker", plan)
                    if plan["done"] >= len(times):
                        break
                    idx = plan["done"]
                    await self._wait_until(times[idx])
                    # ультра-прогрев (затишье/тихие часы) и КД аккаунта: слот не выполняется, пока нельзя
                    await self._wait_unblocked()

                    # Слот опоздал (ждали КД/задания/затишье): не выполняем его залпом вместе со следующими,
                    # а переносим с разбросом — как человек, который «дошёл» до клика позже.
                    now = self._now_local()
                    late = (now - times[idx]).total_seconds()
                    if late > LATE_SLOT_GRACE_SEC:
                        new_t = now + datetime.timedelta(seconds=random.uniform(*LATE_SLOT_RESPACE_RANGE))
                        if new_t >= day_end:
                            await self.tg_logger.log(f"🎮 Остаток слотов дня ({len(times) - idx}) не успеть до конца окна — пропускаю")
                            plan["done"] = len(times)
                            await self.sched.set_data("clicker", plan)
                            break
                        times[idx] = new_t
                        plan["times"] = [t.isoformat() for t in times]
                        await self.sched.set_data("clicker", plan)
                        await self.tg_logger.log(
                            f"🎮 Слот {idx + 1}/{len(times)} просрочен на {int(late // 60)} мин — перенесён на "
                            f"{new_t.strftime('%H:%M')} (без залпа)")
                        continue

                    self.sched.begin("clicker")
                    status = "error"
                    done_flag = True
                    try:
                        status = await self._run_slot(idx + 1, len(times))
                        if status == "limit":
                            # бот: «Ты слишком много кликал! Приходи завтра» — остаток слотов дня бессмыслен
                            plan["done"] = len(times) - 1
                            await self.tg_logger.log("🎮 Остальные клики на сегодня отменены — до завтра")
                        elif status == "retry_later":
                            # КД бота/аккаунта дольше 15 минут: слот переносим на его конец, а не теряем
                            resched = plan.setdefault("resched", {})
                            n = int(resched.get(str(idx), 0)) + 1
                            new_t = self._now_local() + datetime.timedelta(seconds=self._retry_wait + random.uniform(30, 180))
                            if n <= MAX_SLOT_RESCHEDULES and new_t < day_end:
                                resched[str(idx)] = n
                                times[idx] = new_t
                                prev = new_t
                                for j in range(idx + 1, len(times)):   # следующие слоты — не раньше, чем через 4-10 мин
                                    if times[j] < prev + datetime.timedelta(minutes=4):
                                        prev = prev + datetime.timedelta(seconds=random.uniform(240, 600))
                                        times[j] = prev
                                    else:
                                        break
                                plan["times"] = [t.isoformat() for t in times]
                                done_flag = False
                                await self.tg_logger.log(
                                    f"🎮 Слот {idx + 1}/{len(times)}: КД {int(self._retry_wait // 60)} мин — "
                                    f"перенесён на {new_t.strftime('%H:%M')} (попытка {n}/{MAX_SLOT_RESCHEDULES})")
                            else:
                                await self.tg_logger.log(f"🎮 Слот {idx + 1}/{len(times)}: КД слишком долгий — слот пропущен")
                    finally:
                        if done_flag:
                            plan["done"] += 1
                        await self.sched.set_data("clicker", plan)
                        await self.sched.end("clicker", status, f"клик {plan['done']}/{len(times)}")

                now = self._now_local()
                tomorrow = (now + datetime.timedelta(days=1)).replace(
                    hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
                await self._wait_until(tomorrow)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[{self.label}] Ошибка кликера: {e}")
                await asyncio.sleep(3600)

    async def _run_slot(self, index: int, total: int):
        """Слот под детектором зависаний; КД аккаунта (FloodWait) -> перенос слота."""
        prot = self.core.protection
        try:
            if prot is not None:
                return await prot.run_op("clicker", lambda: self._click_slot(index, total), stall_sec=900)
            return await self._click_slot(index, total)
        except CooldownActiveError as e:
            self._retry_wait = e.remaining
            return "retry_later"
        except OperationFrozen as e:
            await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)} завис и был прерван", error=str(e)[:120])
            return "error"

    async def _wait_unblocked(self) -> bool:
        """Ждём, пока ультра-режим (затишье/тихие часы) или КД аккаунта не разрешат клик.
        Ручной клик из админки выполняется по ходу ожидания. -> True, если ждали."""
        prot = self.core.protection
        if prot is None:
            return False
        waited = False
        while True:
            w = prot.job_block_seconds("clicker")
            if w <= 0:
                return waited
            waited = True
            await self.sched.set_next("clicker", w)
            try:
                await asyncio.wait_for(self._manual.wait(), timeout=min(w, 1800))
            except asyncio.TimeoutError:
                continue
            self._manual.clear()
            await self._manual_click()

    async def _click_slot(self, index: int, total: int):
        """Один слот: клик + ожидание кулдауна / выполнение заданий и одна повторная попытка."""
        status, wait = await self._do_one_click(index, total)
        if status == "ad":
            # Бот показал рекламную вставку вместо всплывающего ответа (по «Обучению» — ~9% кликов).
            # По ежедневке видно, что действие при этом БЫВАЕТ засчитано (потом приходит кулдаун).
            # Нажимаем ещё раз: «подожди 00:xx» значит, что первый клик уже засчитан; «получил» — что
            # засчитан этот. В обоих случаях слот дня выполнен ровно один раз.
            await self.core.delay(3, 7)
            status, wait = await self._do_one_click(index, total)
            if status == "cooldown":
                await self._record_click(index, total, "засчитан (бот показал рекламу вместо ответа, кулдаун подтвердил)")
                return "ok"
            if status == "ad":
                status = "unknown"
        if status == "cooldown" and 0 < wait <= SHORT_COOLDOWN_MAX_SEC:
            await self._idle_sleep(wait + random.uniform(5, 25))   # КД бота точный — без масштабирования пауз
            status, wait = await self._do_one_click(index, total)
        elif status == "cooldown" and wait > SHORT_COOLDOWN_MAX_SEC:
            # длинный КД бота: слот переносится run_forever на конец КД
            self._retry_wait = wait
            return "retry_later"
        elif status == "gate":
            await self._wait_for_tasks()
            status, wait = await self._do_one_click(index, total)
        return status

    async def _record_click(self, index, total, note: str = ""):
        self.profile_stats.clicker_clicks_today += 1
        if self.db:
            await self.db.event("click", stars=0.10)
        await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)} " + (note or "засчитан"))
        if self.profile_stats.clicker_clicks_today % 10 == 0:
            try:
                async with self.core.bot_lock.acquire():
                    await self._refresh_balance()
            except Exception as e:
                logger.warning(f"[{self.label}] баланс после клика: {e}")

    async def _wait_for_tasks(self):
        """Бот не даёт кликать, пока не выполнены задания: просим TaskRunner запустить
        цикл сейчас и ждём его конца (не дольше 40 минут). Лок бота тут НЕ держим."""
        requested = False
        if self.on_gate:
            try:
                requested = self.on_gate("клик: бот просит выполнить задания")
            except Exception as e:
                logger.warning(f"[{self.label}] on_gate: {e}")
        prot = self.core.protection
        if not requested:
            await self.tg_logger.log("🎮 Задания запускались недавно — клик повторю после паузы")
            await self._idle_sleep(random.uniform(120, 240))
            return
        op = prot._cur_op() if prot is not None else None
        if op is not None:
            op["idle"] += 1   # штатное долгое ожидание цикла заданий — это не зависание
        try:
            await asyncio.wait_for(self.core.tasks_cycle_done.wait(), 2400)
        except asyncio.TimeoutError:
            pass
        finally:
            if op is not None:
                op["idle"] = max(0, op["idle"] - 1)
                op["last"] = time.monotonic()
        await self.core.delay(5, 12)

    async def _idle_sleep(self, sec: float):
        prot = self.core.protection
        if prot is not None:
            await prot.sleep(sec)
        else:
            await asyncio.sleep(sec)

    async def _do_one_click(self, index: int, total: int):
        """-> (status, wait_sec). status: 'ok' | 'cooldown' | 'gate' | 'nobtn' | 'error' | 'unknown'.

        Раньше ЛЮБОЕ всплывающее окно логировалось как «кулдаун» и клик не
        засчитывался: успешный клик («✅ Ты получил(а) 0.10 ⭐️») не попадал ни в
        статистику, ни в БД. Теперь окно классифицируется (utils.classify_click_alert)."""
        try:
            async with self.core.bot_lock.acquire():
                # _open_unlocked сам решает капчу и проходит гейт спонсоров
                msg = await self.core._open_unlocked()
                if not msg or not msg.buttons:
                    await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: бот не ответил на /start", error="нет ответа")
                    return "error", 0

                clicker_btn = None
                for row in msg.buttons:
                    for btn in row:
                        if any(w in btn.text.lower() for w in ["кликер", "clicker", "farm", "✨"]):
                            clicker_btn = btn
                            break
                    if clicker_btn:
                        break

                if not clicker_btn:
                    labels = " | ".join(b.text for row in msg.buttons for b in row)[:120]
                    await self.tg_logger.log(
                        f"🎮 Клик {self._tag(index, total)}: кнопка кликера не найдена\n🧾 {(msg.text or '')[:60]}\n🔘 {labels}",
                        error="нет кнопки")
                    return "nobtn", 0

                since = utcnow()
                alert_text, _ = await self.core.click_button(clicker_btn)

                if alert_text:
                    kind = classify_click_alert(alert_text)
                    if kind == "cooldown":
                        wait = parse_cooldown_seconds(alert_text)
                        await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: кулдаун — {alert_text}")
                        return "cooldown", wait
                    if kind == "limit":
                        await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: дневной лимит кликов достигнут — {alert_text}")
                        return "limit", 0
                    if kind == "gate":
                        await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: бот просит выполнить задания — {alert_text}")
                        return "gate", 0
                    if kind != "ok":
                        await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: непонятный ответ бота — {alert_text}")
                        return "unknown", 0
                    # kind == "ok" — клик засчитан, идём дальше
                else:
                    # Алерта нет: по данным «Обучения» бот в этом случае показывает либо
                    # капчу («ПРОВЕРКА НА РОБОТА», captcha:clicker:N), либо рекламный экран
                    # (NFT/ГДЗ) вместо клика. Раньше это считалось «клик засчитан» — ложно.
                    resp = await self.core.wait_fresh(since, timeout=6)
                    first = ((resp.text or "").strip().splitlines() or [""])[0][:60] if resp else ""
                    low_resp = (resp.text or "").lower() if resp else ""
                    if resp and "новые задания" in low_resp and "доступны" in low_resp:
                        # «💫 Возможно, тебе уже доступны новые задания!» — это тот же «выполни задания»
                        await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: бот просит выполнить задания")
                        return "gate", 0
                    if resp and is_ad(resp):
                        await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: вместо ответа реклама «{first}» — перепроверю")
                        return "ad", 0
                    if resp and "проверка" in (resp.text or "").lower():
                        solved = await self.core.captcha.solve_if_needed(resp)
                        await self.tg_logger.log(
                            f"🎮 Клик {self._tag(index, total)}: капча " + ("решена" if solved else "НЕ решена")
                            + ", награда не подтверждена ботом — в статистику не добавляю")
                        return "unknown", 0
                    await self.tg_logger.log(
                        f"🎮 Клик {self._tag(index, total)}: клик не засчитан — бот показал экран «{first}» вместо награды")
                    return "unknown", 0

                self.profile_stats.clicker_clicks_today += 1
                if self.db:
                    await self.db.event("click", stars=0.10)
                await self.tg_logger.log(
                    f"🎮 Клик {self._tag(index, total)} засчитан" + (f": {alert_text}" if alert_text else ""))

                if self.profile_stats.clicker_clicks_today % 10 == 0:
                    await self._refresh_balance()
                return "ok", 0

        except asyncio.CancelledError:
            raise
        except CooldownActiveError:
            raise
        except Exception as e:
            logger.error(f"[{self.label}] Ошибка клика #{index}: {e}")
            self.profile_stats.add_error(f"кликер#{index}: {str(e)[:40]}")
            await self.tg_logger.log(f"🎮 Клик {self._tag(index, total)}: исключение", error=str(e))
            return "error", 0

    def _target_bot(self):
        return TARGET_BOT

    async def _refresh_balance(self):
        """Вызывается ТОЛЬКО изнутри уже удержанного bot_lock (см. _do_one_click) —
        поэтому строго _open_unlocked(), а не open_bot() (иначе самодедлок)."""
        msg = await self.core._open_unlocked()
        if not msg or not msg.buttons:
            return
        for row in msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in ["профил", "profile"]):
                    since = utcnow()
                    await self.core.click_raw(btn)
                    resp = await self.core.wait_fresh(since, timeout=6)
                    if resp:
                        await self.profile_stats.update_from_profile_text(resp.text or "")
                    return
