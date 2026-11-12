"""
«Обход бота» — автоматически проходит по кнопкам @patrickstarsrobot вглубь и
собирает всё в «Обучение» (knowledge.py): экраны, тексты, кнопки, что открывает
каждая кнопка, всплывающие алерты.

БЕЗОПАСНОСТЬ (важно, на аккаунте реальные звёзды):
  * нажимаются только inline-кнопки с callback; ссылки (url) и мини-аппы не открываются;
  * не нажимаются кнопки, которые могут что-то тратить/менять: суммы вывода
    («15 ⭐»), «Подарить другу», Premium, покупки/оплата, PvP/игры, NFT-кейсы,
    «Проверить»/«Пропустить» заданий, ежедневный бонус, клик и т.п. Они записываются
    в «Обучение» как «не нажимал: причина» — чтобы ты видел, что там есть;
  * бюджет: не больше CRAWL_MAX_STEPS переходов за обход и глубина CRAWL_MAX_DEPTH;
  * лок бота берётся только на один переход (открыть → пройти путь → нажать),
    между переходами кликер/задания/промокоды работают как обычно.
Дополнительные стоп-слова: env CRAWL_SKIP_EXTRA="слово1,слово2".
Обход идёт на ОДНОМ аккаунте (первом): раз в CRAWL_INTERVAL_DAYS и по кнопке в админке.
"""
import asyncio
import logging
import os
import re
import time
from collections import deque

from core import utcnow
from knowledge import screen_fp, button_info
from utils import human_delay

logger = logging.getLogger(__name__)

MAX_STEPS = int(os.getenv("CRAWL_MAX_STEPS", "70"))
MAX_DEPTH = int(os.getenv("CRAWL_MAX_DEPTH", "4"))
INTERVAL_DAYS = float(os.getenv("CRAWL_INTERVAL_DAYS", "7"))
FIRST_DELAY_SEC = int(os.getenv("CRAWL_FIRST_DELAY_SEC", "900"))

# (регэксп по подписи в нижнем регистре, причина)
_SKIP_RULES = [
    (r"\d+\s*(⭐|★|звёзд|звезд)", "сумма/подарок за звёзды — может сразу вывести"),
    (r"подарить|подарок", "подарок за звёзды"),
    (r"premium|премиум", "покупка Premium"),
    (r"куп(ить|и)|оплат|пополн|депозит|donat|донат", "оплата/покупка"),
    (r"вывест|вывод\s*\d|подтверд|да,|✅\s*да", "подтверждение вывода/действия"),
    (r"pvp|пвп|игр|ставк|казино|кейс|nft|открыть кейс", "игры/ставки/кейсы тратят звёзды"),
    (r"проверить|проверка|пропустить|выполнил", "кнопки задания (меняют статус задания)"),
    (r"бонус|ежедневн|забрать|получить|промокод", "забор награды/ввод кода"),
    (r"кликер|клик|✨", "клик (кулдаун, им занимается кликер)"),
    (r"удалить|сброс|отмен|отключ", "потенциально разрушающее действие"),
]
_BACK = re.compile(r"назад|вернуться|главн|в меню|^меню$|◀|⬅|🔙|🏠")


# Реальные callback-data бота (из «Обучения»). Навигация назад и опасные/служебные.
_BACK_DATA = {"main_menu"}
_SKIP_DATA_PREFIX = (
    ("daily:", "ежедневный бонус (кулдаун)"),
    ("clicker:", "клик (кулдаун, им занимается кликер)"),
    ("promo:", "ввод промокода переводит бота в режим ожидания текста"),
    ("transfer:", "перевод звёзд другу"),
    ("captcha:", "капча"),
    ("show_reinvite_friends", "награда за старых друзей"),
)


def skip_reason(label: str, data: str = ""):
    for pref, why in _SKIP_DATA_PREFIX:
        if (data or "").startswith(pref):
            return why
    low = (label or "").lower()
    for rx, why in _SKIP_RULES:
        if re.search(rx, low):
            return why
    extra = [w.strip().lower() for w in os.getenv("CRAWL_SKIP_EXTRA", "").split(",") if w.strip()]
    for w in extra:
        if w in low:
            return f"стоп-слово «{w}» (CRAWL_SKIP_EXTRA)"
    return None


def _callback_buttons(msg):
    out = []
    try:
        for row in (msg.buttons or []):
            for b in row:
                if type(getattr(b, "button", None)).__name__ == "KeyboardButtonCallback":
                    out.append(b)
    except Exception:
        pass
    return out


class Crawler:
    def __init__(self, core, tg_logger, knowledge, label: str):
        self.core = core
        self.tg_logger = tg_logger
        self.knowledge = knowledge
        self.label = label
        self._wake = asyncio.Event()
        self.status = {"running": False, "last_run": None, "last_summary": None, "progress": ""}

    def request(self) -> bool:
        if self.status["running"]:
            return False
        self._wake.set()
        return True

    async def run_forever(self):
        try:
            await asyncio.wait_for(self._wake.wait(), FIRST_DELAY_SEC)
        except asyncio.TimeoutError:
            pass
        while True:
            self._wake.clear()
            try:
                await self.crawl()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[%s] crawler: %s", self.label, e)
                self.status["running"] = False
                await self.tg_logger.log("🎓 Обход бота: ошибка", error=str(e)[:200])
            try:
                await asyncio.wait_for(self._wake.wait(), INTERVAL_DAYS * 86400)
            except asyncio.TimeoutError:
                pass

    async def _goto(self, path):
        """Открыть бота, пройти по подписям кнопок path. -> (msg, alert, error)."""
        async with self.core.bot_lock.acquire():
            msg = await self.core._open_unlocked()
            if not msg:
                return None, None, "бот не ответил"
            alert = None
            for i, label in enumerate(path):
                btn = next((b for b in _callback_buttons(msg) if b.text == label), None)
                if not btn:
                    return None, None, f"кнопка «{label}» пропала"
                since = utcnow()
                alert, _ = await self.core.click_button(btn)
                if alert:
                    # алерт не меняет экран; на промежуточном шаге путь дальше не идёт
                    return (msg, alert, None) if i == len(path) - 1 else (None, alert, "алерт на пути")
                nxt = await self.core.wait_fresh(since, timeout=8, fallback=False)
                if nxt:
                    msg = nxt
                if i < len(path) - 1:
                    await human_delay(1.5, 3)
            return msg, alert, None

    async def crawl(self):
        st = self.status
        st.update(running=True, progress="старт")
        t0 = time.monotonic()
        await self.tg_logger.log("🎓 Обход бота: начинаю (только безопасные кнопки)")

        seen = set()          # fp экранов, уже раскрытых
        planned = set()       # (fp родителя, подпись) уже поставленные в очередь
        steps = clicked = skipped = alerts = 0
        queue = deque([([], None)])  # (путь из подписей, fp родителя)

        while queue and steps < MAX_STEPS:
            path, parent_fp = queue.popleft()
            steps += 1
            st["progress"] = f"шаг {steps}, очередь {len(queue)}"
            try:
                msg, alert, err = await self._goto(path)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[%s] crawler goto %s: %s", self.label, path, e)
                continue
            if path:
                clicked += 1
            if alert:
                alerts += 1
            if err or msg is None:
                continue

            fp = screen_fp(msg)
            if fp in seen or fp == parent_fp:
                await human_delay(2, 4)
                continue
            seen.add(fp)
            if len(path) >= MAX_DEPTH:
                continue

            for btn in _callback_buttons(msg):
                label = btn.text
                data = button_info(btn).get("data", "")
                if data in _BACK_DATA or _BACK.search(label.lower()):
                    continue
                why = skip_reason(label, data)
                if why:
                    skipped += 1
                    self.knowledge.on_skipped(msg, btn, why)
                    continue
                key = (fp, label)
                if key in planned:
                    continue
                planned.add(key)
                queue.append((path + [label], fp))
            await human_delay(3, 6)

        st.update(running=False, progress="", last_run=time.strftime("%d.%m %H:%M"))
        summary = (f"экранов {len(seen)}, переходов {steps}, нажато {clicked}, алертов {alerts}, "
                   f"пропущено опасных кнопок {skipped}, {int(time.monotonic() - t0)} с")
        if queue:
            summary += f" (бюджет {MAX_STEPS} шагов исчерпан, в очереди ещё {len(queue)})"
        st["last_summary"] = summary
        await self.tg_logger.log(f"🎓 Обход бота завершён: {summary}")
