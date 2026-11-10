"""
Hub — общий для всех аккаунтов процесса.

Каждый аккаунт сам следит за каналом и сам активирует (независимо). Хаб лишь
ускоряет: как только ЛЮБОЙ аккаунт увидел код/чек, он сразу сообщает об этом
остальным — они не ждут своего события/опроса. Дубли отсекаются на стороне
аккаунта (promo.submit / checks.submit), так что двойной активации не будет.

Хаб ничего не хранит, кроме списка аккаунтов, и не держит блокировок:
сбой/зависание одного аккаунта другим не мешает.
"""
import logging

logger = logging.getLogger(__name__)


class Hub:
    def __init__(self):
        self.accounts = []

    def register(self, account):
        self.accounts.append(account)
        account.promo.hub = self

    def share_promo(self, origin_label: str, code: str):
        for acc in self.accounts:
            if acc.label != origin_label and acc.ready:
                try:
                    acc.promo.submit(code)
                except Exception as e:
                    logger.warning(f"hub: share_promo → {acc.label}: {e!r}")

    def share_check(self, origin_label: str, bot: str, payload: str):
        for acc in self.accounts:
            if acc.label != origin_label and acc.ready:
                try:
                    acc.checks.submit(bot, payload)
                except Exception as e:
                    logger.warning(f"hub: share_check → {acc.label}: {e!r}")
