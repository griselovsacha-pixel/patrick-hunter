"""
Страховка от OOM на Render free (512 МБ).

Раз в минуту читает RSS процесса. Выше мягкого порога — принудительный
gc.collect(). Выше жёсткого — аккуратный выход, чтобы Render сам перезапустил
сервис (после рестарта аккаунты стартуют заново со сдвигом), вместо того
чтобы OOM-killer убил процесс посреди активации. Пороги — в config.
"""
import asyncio
import gc
import logging
import os

from config import MEMORY_SOFT_LIMIT_MB, MEMORY_HARD_LIMIT_MB

logger = logging.getLogger(__name__)


def rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


async def memory_guard_cycle():
    over_hard = 0
    while True:
        await asyncio.sleep(60)
        rss = rss_mb()
        if rss > MEMORY_SOFT_LIMIT_MB:
            gc.collect()
            rss = rss_mb()
            logger.warning("🧠 Память %.0f МБ (> %d) — gc.collect(), после: %.0f МБ",
                           rss, MEMORY_SOFT_LIMIT_MB, rss)
        if rss > MEMORY_HARD_LIMIT_MB:
            over_hard += 1
            if over_hard >= 2:  # два замера подряд — не случайный всплеск
                logger.error("🧠 Память %.0f МБ (> %d) — перезапуск процесса", rss, MEMORY_HARD_LIMIT_MB)
                os._exit(1)
        else:
            over_hard = 0
