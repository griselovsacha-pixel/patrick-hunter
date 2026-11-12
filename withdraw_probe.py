"""
Безопасная «проба вывода»: узнать, как бот отвечает на кнопку вывода, НЕ выводя звёзды.

Из «Обучения» известно: экран вывода (кнопка «Вывод звезд», data=change_stars) требует
«минимум 5 приглашённых друзей, активировавших бота» + подписку на канал; суммы — кнопки
gift_<сумма>_<вариант> (15/25/50/100 ⭐), Premium — gift_premium / gift_premium_3months.
Что бот отвечает на нажатие суммы — неизвестно (возможно, подтверждение, возможно, сразу вывод).

Проба нажимает ТОЛЬКО самую маленькую сумму (gift_15_1) и ТОЛЬКО если по профилю бот
гарантированно откажет: баланс < 15 ИЛИ активировавших друзей < 5. Иначе — отказ, ничего
не нажимается. Любую кнопку подтверждения после ответа бота проба не трогает.
Ответ (алерт или новый экран) попадает в лог-группу и в «Обучение».
"""
import re

from core import utcnow
from utils import find_button_by_data

MIN_AMOUNT = 15.0
MIN_FRIENDS = 5


def _num(rx, text):
    m = re.search(rx, text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


async def probe_withdraw(account) -> str:
    core, log = account.core, account.tg_logger
    async with core.bot_lock.acquire():
        msg = await core._open_unlocked()
        btn = find_button_by_data(msg, exact=("profile:show",))
        if not btn:
            return "не нашёл кнопку профиля"
        since = utcnow()
        await core.click_button(btn)
        prof = await core.wait_fresh(since, timeout=10)
        text = (prof.text or "") if prof else ""
        balance = _num(r"Баланс:\s*([\d.,]+)", text)
        activated = _num(r"Активировали бота:\s*(\d+)", text)
        if balance is None or activated is None:
            return "не смог прочитать баланс/друзей из профиля — проба отменена"
        if balance >= MIN_AMOUNT and activated >= MIN_FRIENDS:
            res = (f"отказ: условия вывода выполнены (баланс {balance}, активировавших друзей {int(activated)}) — "
                   f"нажатие суммы могло бы реально вывести звёзды")
            await log.log(f"🧪 Проба вывода: {res}")
            return res

        msg = await core._open_unlocked()
        btn = find_button_by_data(msg, exact=("change_stars",))
        if not btn:
            return "не нашёл кнопку «Вывод звезд»"
        since = utcnow()
        await core.click_button(btn)
        wd = await core.wait_fresh(since, timeout=10)
        gift = find_button_by_data(wd, exact=("gift_15_1",)) if wd else None
        if not gift:
            return "не нашёл кнопку gift_15_1 на экране вывода"

        since = utcnow()
        alert, _ = await core.click_button(gift)
        resp = None if alert else await core.wait_fresh(since, timeout=8)
        if alert:
            out = f"алерт бота: {alert}"
        elif resp:
            labels = " | ".join(b.text for row in (resp.buttons or []) for b in row)[:120]
            out = f"новый экран: {(resp.text or '')[:200]}\n🔘 {labels}"
        else:
            out = "бот не ответил"
        res = (f"нажал gift_15_1 (баланс {balance}, активировавших друзей {int(activated)} — бот должен отказать)\n"
               f"↳ {out}")
        await log.log(f"🧪 Проба вывода: {res}")
        return res
