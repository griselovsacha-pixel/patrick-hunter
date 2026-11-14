"""
Hub — общий для всех аккаунтов процесса.

Каждый аккаунт сам следит за каналом (события + опрос). Хаб делает две вещи:

1. Промокоды: как только ЛЮБОЙ аккаунт увидел код, он сразу сообщает об этом
   остальным (share_promo) — они не ждут своего события/опроса. Дубли отсекаются
   на стороне аккаунта (promo.submit).

2. Чеки: забирает чек ОДИН аккаунт — тот, который первым увидел пост (claim_check
   — синхронный и потому атомарный в одном event loop). Остальные молча пропускают.
   Если победитель не смог (бот не ответил, FloodWait, упал прокси) — finish_check
   отдаёт чек следующему аккаунту, который ещё не пробовал.

Хаб ничего не блокирует: сбой/зависание одного аккаунта другим не мешает.
"""
import logging
import os
from collections import OrderedDict

logger = logging.getLogger(__name__)

_CHECK_MEMORY = 300  # сколько последних чеков помним
# CHECK_ONE_ACCOUNT_PER_CHECK=1 (по умолчанию): чек забирает один аккаунт, второй — только если первый не смог.
# =0: каждый аккаунт забирает чек сам (каждый аккаунт — отдельный пользователь, чек можно взять с каждого).
ONE_PER_CHECK = os.getenv("CHECK_ONE_ACCOUNT_PER_CHECK", "1") != "0"


class Hub:
    def __init__(self):
        self.accounts = []
        # (bot, payload) -> {"owner": label|None, "done": bool, "failed": set[label]}
        self._checks: "OrderedDict[tuple, dict]" = OrderedDict()

    def register(self, account):
        self.accounts.append(account)
        account.promo.hub = self
        account.checks.hub = self

    # --- промокоды -----------------------------------------------------
    def share_promo(self, origin_label: str, code: str):
        for acc in self.accounts:
            if acc.label != origin_label and acc.ready:
                try:
                    acc.promo.submit(code)
                except Exception as e:
                    logger.warning(f"hub: share_promo → {acc.label}: {e!r}")

    # --- чеки: один аккаунт на чек -------------------------------------
    def claim_check(self, label: str, key: tuple) -> bool:
        """True — этот аккаунт теперь единственный, кто забирает чек."""
        if not ONE_PER_CHECK:
            return True
        st = self._checks.get(key)
        if st is None:
            st = {"owner": None, "done": False, "failed": set()}
            self._checks[key] = st
            while len(self._checks) > _CHECK_MEMORY:
                self._checks.popitem(last=False)
        if st["done"] or label in st["failed"]:
            return False
        if st["owner"] not in (None, label):
            return False
        st["owner"] = label
        return True

    def finish_check(self, label: str, key: tuple, *, final: bool):
        """final=True — чек обработан (успех/отказ бота/ответ получен): больше
        никто не трогает. final=False — аккаунт не смог дойти до бота: отдаём
        чек другому аккаунту, который ещё не пробовал."""
        if not ONE_PER_CHECK:
            return
        st = self._checks.get(key)
        if st is None or st["owner"] != label:
            return
        if final:
            st["done"] = True
            return
        st["owner"] = None
        st["failed"].add(label)
        bot, payload = key
        for acc in self.accounts:
            if acc.label in st["failed"] or not acc.ready:
                continue
            try:
                logger.info(f"hub: чек @{bot} не удался на {label} → пробует {acc.label}")
                acc.checks.submit(bot, payload)
            except Exception as e:
                logger.warning(f"hub: чек → {acc.label}: {e!r}")
            if st["owner"] is not None:  # submit успешно занял чек
                break
