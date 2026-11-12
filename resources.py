"""
Замер ресурсов процесса и расчёт, сколько аккаунтов можно подключить.

Раз в минуту пишем: RSS (МБ), CPU (% одного ядра), число готовых аккаунтов и
asyncio-задач. Кольцо в памяти (≈66 ч) + раз в 10 минут точка уходит в БД
(resource_samples, хранится 30 дней) — после рестартов история не теряется.

База (base_rss) — RSS процесса ДО запуска аккаунтов (python + FastAPI + aiohttp).
Тогда «цена одного аккаунта» = (RSS с N аккаунтами − base) / N: честнее, чем
гадать по процентам с графика Render. Расчёт даёт три числа: по памяти в покое,
по памяти на пиках (старт, обход бота) и по CPU.

Пределы (env): RAM_LIMIT_MB=512, CPU_LIMIT_CORES=0.1 (Render free), RESOURCE_SAFE_RATIO=0.85.
"""
import asyncio
import collections
import datetime
import logging
import os
import statistics
import time

import aiohttp

from memguard import rss_mb
from supabase_db import supabase_headers

logger = logging.getLogger(__name__)

RAM_LIMIT_MB = float(os.getenv("RAM_LIMIT_MB", "512"))
CPU_LIMIT_CORES = float(os.getenv("CPU_LIMIT_CORES", "0.1"))
SAFE_RATIO = float(os.getenv("RESOURCE_SAFE_RATIO", "0.85"))
SAMPLE_EVERY_SEC = 60
PERSIST_EVERY = 10          # каждая 10-я точка (≈10 минут) пишется в БД
KEEP_DAYS = 30


class ResourceSampler:
    def __init__(self, get_accounts):
        self.get_accounts = get_accounts
        self.base_rss = rss_mb()             # вызывать ДО запуска аккаунтов
        self.ring = collections.deque(maxlen=4000)
        self._last_cpu = time.process_time()
        self._last_wall = time.monotonic()
        self._n = 0
        self._last_cleanup = 0.0

    # ---------- сбор ----------
    def sample_now(self) -> dict:
        now_cpu, now_wall = time.process_time(), time.monotonic()
        dwall = max(now_wall - self._last_wall, 1e-6)
        cpu_pct = (now_cpu - self._last_cpu) / dwall * 100.0
        self._last_cpu, self._last_wall = now_cpu, now_wall
        accounts = [a for a in self.get_accounts() if getattr(a, "ready", False)]
        try:
            tasks = len(asyncio.all_tasks())
        except RuntimeError:
            tasks = 0
        s = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "rss": round(rss_mb(), 1),
            "cpu": round(cpu_pct, 2),
            "accounts": len(accounts),
            "tasks": tasks,
        }
        self.ring.append(s)
        return s

    async def run(self):
        while True:
            await asyncio.sleep(SAMPLE_EVERY_SEC)
            try:
                s = self.sample_now()
                self._n += 1
                if self._n % PERSIST_EVERY == 0:
                    await self._persist(s)
                if time.monotonic() - self._last_cleanup > 86400:
                    self._last_cleanup = time.monotonic()
                    await self._cleanup()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("resources: %s", e)

    # ---------- БД (REST) ----------
    async def _rest(self, method, path, **kw):
        url, key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            return None
        headers = {**supabase_headers(key), "Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as ses:
            async with ses.request(method, url.rstrip("/") + path, headers=headers, **kw) as r:
                body = await r.text()
                if r.status >= 400:
                    raise RuntimeError(f"HTTP {r.status}: {body[:200]}")
                return body

    async def _persist(self, s):
        await self._rest("POST", "/rest/v1/resource_samples", json={
            "ts": s["ts"], "rss_mb": s["rss"], "cpu_pct": s["cpu"],
            "accounts": s["accounts"], "tasks": s["tasks"],
        })

    async def _cleanup(self):
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=KEEP_DAYS)).isoformat()
        await self._rest("DELETE", "/rest/v1/resource_samples", params={"ts": f"lt.{cutoff}"})

    async def load_history(self):
        """После рестарта подтянуть последние сутки из БД (для графика и расчёта)."""
        try:
            since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()
            body = await self._rest("GET", "/rest/v1/resource_samples", params={
                "select": "ts,rss_mb,cpu_pct,accounts,tasks", "ts": f"gte.{since}",
                "order": "ts.asc", "limit": "2000"})
            import json
            rows = json.loads(body or "[]")
        except Exception as e:
            logger.debug("resources.load_history: %s", e)
            return
        old = [{"ts": r["ts"], "rss": float(r["rss_mb"]), "cpu": float(r["cpu_pct"]),
                "accounts": int(r["accounts"]), "tasks": int(r["tasks"]), "persisted": True} for r in rows]
        self.ring = collections.deque(old + list(self.ring), maxlen=4000)

    # ---------- анализ ----------
    def analyze(self) -> dict:
        samples = list(self.ring)
        cur = samples[-1] if samples else self.sample_now()
        n = cur["accounts"]
        out = {
            "limits": {"ram_mb": RAM_LIMIT_MB, "cpu_cores": CPU_LIMIT_CORES, "safe_ratio": SAFE_RATIO},
            "base_rss_mb": round(self.base_rss, 1),
            "current": cur,
            "samples": len(samples),
            "notes": [],
        }
        if n <= 0:
            out["notes"].append("Нет запущенных аккаунтов — считать «цену аккаунта» не по чему.")
            return out

        same = [s for s in samples if s["accounts"] == n]
        settled = same[8:] if len(same) > 12 else same   # без первых минут после старта/подключения
        if not settled:
            settled = same
        rss_steady = statistics.median(s["rss"] for s in settled)
        rss_peak = max(s["rss"] for s in samples if s["accounts"] >= 1)
        cpu_avg = statistics.mean(s["cpu"] for s in settled)
        cpu_peak = max(s["cpu"] for s in samples if s["accounts"] >= 1)

        per_steady = max((rss_steady - self.base_rss) / n, 1.0)
        per_peak = max((rss_peak - self.base_rss) / n, per_steady)
        safe_mb = RAM_LIMIT_MB * SAFE_RATIO
        max_by_steady = int((safe_mb - self.base_rss) // per_steady)
        max_by_peak = int((safe_mb - self.base_rss) // per_peak)

        cpu_limit_pct = CPU_LIMIT_CORES * 100.0
        per_cpu = max(cpu_avg / n, 0.01)
        max_by_cpu = int((cpu_limit_pct * 0.6) // per_cpu)

        recommended = max(min(max_by_peak, max_by_cpu), n)
        out.update({
            "accounts": n,
            "rss_steady_mb": round(rss_steady, 1),
            "rss_peak_mb": round(rss_peak, 1),
            "per_account_steady_mb": round(per_steady, 1),
            "per_account_peak_mb": round(per_peak, 1),
            "cpu_avg_pct": round(cpu_avg, 2),
            "cpu_peak_pct": round(cpu_peak, 1),
            "cpu_limit_pct": cpu_limit_pct,
            "per_account_cpu_pct": round(per_cpu, 2),
            "max_by_memory_steady": max_by_steady,
            "max_by_memory_peak": max_by_peak,
            "max_by_cpu": max_by_cpu,
            "recommended_total": recommended,
            "can_add": max(recommended - n, 0),
        })
        if len(settled) < 15:
            out["notes"].append("Мало данных после старта (меньше ~15 минут) — оценка грубая, подожди час-два.")
        if n < 3:
            out["notes"].append(
                "Аккаунтов мало: «цена аккаунта» считается как (RSS − база)/N, и ошибка базы заметна. "
                "После подключения 3–4 аккаунтов оценка станет точнее.")
        if cpu_peak > cpu_limit_pct * 3:
            out["notes"].append(
                "Пики CPU сильно выше лимита (0.1 ядра): при старте/обходе сервис может тормозить. "
                "Не запускай обход бота и деплой одновременно.")
        out["notes"].append(
            "Лимит по Telegram (FloodWait/бан при многих аккаунтах с одного IP) этим расчётом НЕ учтён — "
            "он, как правило, наступает раньше памяти.")
        return out

    def history(self, hours: int = 24) -> list:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
        out = []
        for s in self.ring:
            try:
                t = datetime.datetime.fromisoformat(str(s["ts"]).replace("Z", "+00:00"))
            except Exception:
                continue
            if t >= cutoff:
                out.append(s)
        step = max(len(out) // 400, 1)   # не больше ~400 точек на график
        return out[::step]
