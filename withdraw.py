"""
Автовывод звёзд подарком ДРУГУ — запускается только из админки (вкладка «Выводы»).

Сценарий (по данным «Обучения» и описанию владельца):
  /start → «Вывод звезд» (change_stars) → «🎁 Подарить другу» (letsSendAGiftToFriend)
  → кнопка подарка send-gift_<сумма>_<вариант> → бот просит Telegram ID друга
  → отправляем ID (всегда WITHDRAW_FRIEND_ID, по умолчанию 5458870502)
  → новое сообщение «‼️ Ты точно хочешь отправить другу…» с inline-кнопкой
    «✅ Подтвердить» (data=giftsendtofriend_calback:<id>:<сумма_вариант>:<эмодзи>:<сумма>)
  → нажали — вывод считается выполненным.

Защиты (на счету реальные звёзды):
  * по одному выводу за раз на всё приложение;
  * не нажимаем подарок, если баланс (по экрану вывода) меньше его цены;
  * перед «Подтвердить» сверяем, что в запросе бота тот же ID друга и тот же подарок/сумма,
    иначе подтверждение НЕ нажимаем (статус failed);
  * после подтверждения читаем баланс из Профиля: если он не уменьшился на цену подарка,
    вывод помечается «unverified» (проблема) — а не «успех».
Каждый шаг пишется в историю (таблица withdrawals), в лог-группу и в «Обучение» (kind=flow).
Кнопки ищем по callback-data, а не по подписи (подписи у бота меняются).
"""
import asyncio
import datetime
import logging
import os
import re

from antiban import safe_call
from core import utcnow
from utils import find_button_by_data, human_delay

logger = logging.getLogger(__name__)

FRIEND_ID = os.getenv("WITHDRAW_FRIEND_ID", "5458870502")
MIN_ACTIVATED = 5   # условие бота: минимум 5 друзей, активировавших бота
ENABLED = os.getenv("WITHDRAW_ENABLED", "1") != "0"

# Каталог подарков экрана «Подарить другу». Порядок и эмодзи — по экрану бота
# (в тексте кнопок эмодзи — custom emoji, в тексте не видны; соответствие подтверждено
# запросом бота «🧸 за 15.0 ⭐️» для send-gift_15_1, остальное — по раскладке экрана).
# stars — цена при подарке ДРУГУ (экран send-gift_*), self_stars/self_data — вывод СЕБЕ (экран gift_*).
# Premium 3 мес. есть только в режиме «себе»; Premium 6 мес.: 1200⭐ себе / 1700⭐ другу (цены с экранов бота).
_STD = [("15_1", "🧸", "Мишка", 15), ("15_2", "💝", "Сердце", 15), ("25_1", "🌹", "Роза", 25),
        ("25_2", "🎁", "Подарок", 25), ("50_1", "🍾", "Шампанское", 50), ("50_2", "💐", "Букет", 50),
        ("50_3", "🚀", "Ракета", 50), ("50_4", "🎂", "Торт", 50), ("100_1", "🏆", "Кубок", 100),
        ("100_2", "💍", "Кольцо", 100), ("100_3", "💎", "Алмаз", 100)]
GIFTS = [{"code": c, "emoji": e, "name": n, "stars": p, "self_stars": p, "self_data": f"gift_{c}"}
         for c, e, n, p in _STD]
GIFTS += [
    {"code": "premium_3m", "emoji": "👑", "name": "Telegram Premium 3 мес.", "stars": None,
     "self_stars": 900, "self_data": "gift_premium_3months"},
    {"code": "premium", "emoji": "👑", "name": "Telegram Premium 6 мес.", "stars": 1700,
     "self_stars": 1200, "self_data": "gift_premium"},
]
GIFT_BY_CODE = {g["code"]: g for g in GIFTS}


def button_data(code: str) -> str:
    return f"send-gift_{code}"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _balance(text):
    m = re.search(r"Баланс:?\W*([\d]+(?:[.,]\d+)?)", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


# --------------------------------------------------------------------------
# менеджер: один вывод за раз, живой прогресс для админки
# --------------------------------------------------------------------------
class WithdrawManager:
    def __init__(self):
        self.current = None        # dict с живым прогрессом или None
        self.recent = []           # последние завершённые (на случай недоступной БД)
        self._lock = asyncio.Lock()

    def is_running(self) -> bool:
        return self.current is not None

    async def start(self, acc, code: str, mode: str = "friend", friend_id=None) -> dict:
        if not ENABLED:
            raise RuntimeError("Автовывод выключен (WITHDRAW_ENABLED=0)")
        gift = GIFT_BY_CODE.get(code)
        if not gift:
            raise ValueError("Неизвестный подарок")
        if mode not in ("self", "friend"):
            raise ValueError("Режим: self или friend")
        price = gift["self_stars"] if mode == "self" else gift["stars"]
        if not price:
            raise ValueError("Этот подарок недоступен в выбранном режиме")
        target = None
        if mode == "friend":
            target = str(friend_id or FRIEND_ID).strip()
            if not target.isdigit() or not (5 <= len(target) <= 13):
                raise ValueError("ID друга — только цифры (Telegram ID)")
        if self.current is not None:
            raise RuntimeError("Уже идёт другой вывод")
        ps = acc.profile_stats
        if getattr(ps, "balance_updated_at", None) and int(ps.activated or 0) < MIN_ACTIVATED:
            raise RuntimeError(
                f"Бот выводит от {MIN_ACTIVATED} друзей, активировавших бота (сейчас {int(ps.activated or 0)})")
        job = {
            "account": int("".join(ch for ch in acc.label if ch.isdigit()) or "1"),
            "label": acc.label, "gift_code": code, "gift_emoji": gift["emoji"],
            "gift_name": gift["name"], "stars": price, "friend_id": target, "mode": mode,
            "status": "running", "error": None, "steps": [], "bot_response": None,
            "balance_before": None, "balance_after": None,
            "created_at": _now().isoformat(), "finished_at": None, "id": None,
        }
        self.current = job
        try:
            job["id"] = await acc.db.withdraw_create(job)
        except Exception as e:
            logger.warning("withdraw: не записал старт в БД: %s", e)
        asyncio.create_task(self._run(acc, job))
        return job

    async def _run(self, acc, job):
        try:
            await _flow(acc, job, self._persist(acc, job))
        except Exception as e:
            logger.exception("withdraw flow: %s", e)
            job["status"], job["error"] = "failed", f"исключение: {str(e)[:200]}"
        finally:
            job["finished_at"] = _now().isoformat()
            await self._persist(acc, job)()
            await _announce(acc, job)
            self.recent = ([dict(job)] + self.recent)[:50]
            self.current = None

    def _persist(self, acc, job):
        async def save():
            if job.get("id"):
                try:
                    await acc.db.withdraw_update(job["id"], job)
                except Exception as e:
                    logger.debug("withdraw persist: %s", e)
        return save


manager = WithdrawManager()


def _step(job, name, ok=True, detail=""):
    job["steps"].append({"t": _now().strftime("%H:%M:%S"), "step": name, "ok": ok, "detail": (detail or "")[:300]})


def _fail(job, error, step_name=None):
    job["status"], job["error"] = "failed", error
    _step(job, step_name or "ошибка", False, error)


def _target_text(job) -> str:
    return "себе" if job.get("mode") == "self" else f"другу {job.get('friend_id')}"


async def _announce(acc, job):
    icon = {"success": "✅", "unverified": "❔", "failed": "❌"}.get(job["status"], "•")
    bal = ""
    if job["balance_before"] is not None:
        bal = f"\n💰 Баланс: {job['balance_before']} → {job['balance_after'] if job['balance_after'] is not None else '?'}"
    err = f"\n⚠️ {job['error']}" if job.get("error") else ""
    await acc.tg_logger.log(
        f"{icon} Вывод {job['gift_emoji']} {job['gift_name']} {job['stars']}⭐ {_target_text(job)}: "
        f"{job['status']}{bal}{err}")
    if getattr(acc, "knowledge", None):
        acc.knowledge.on_flow(
            f"Вывод {job['gift_emoji']} {job['stars']}⭐ {_target_text(job)} → {job['status']}",
            job["steps"], job["status"], {"gift": job["gift_code"], "error": job.get("error")})


# --------------------------------------------------------------------------
# сам сценарий
# --------------------------------------------------------------------------
async def _flow(acc, job, save):
    core = acc.core
    gift = GIFT_BY_CODE[job["gift_code"]]
    mode = job.get("mode", "friend")
    price = job["stars"]
    friend = str(job["friend_id"] or "")

    async def click(btn):
        return await core.click_button(btn)

    async with core.bot_lock.acquire():
        msg = await core._open_unlocked()
        _step(job, "открыл бота", bool(msg))
        if not msg:
            return _fail(job, "бот не ответил на /start")

        # 1. «Вывод звезд»
        btn = find_button_by_data(msg, exact=("change_stars",))
        if not btn:
            return _fail(job, "нет кнопки «Вывод звезд» на главном экране")
        since = utcnow()
        await click(btn)
        w = await core.wait_fresh(
            since, timeout=15, predicate=lambda m: bool(find_button_by_data(m, exact=("letsSendAGiftToFriend",))))
        if not w:
            return _fail(job, "экран вывода не открылся (бот ответил рекламой/ошибкой?)")
        job["balance_before"] = _balance(w.text)
        if job["balance_before"] is None and getattr(acc.profile_stats, "balance", 0):
            job["balance_before"] = float(acc.profile_stats.balance)   # с экрана баланс не прочитался — берём из последнего профиля
        _step(job, "экран вывода", True, f"баланс {job['balance_before']}")
        await save()
        if job["balance_before"] is not None and job["balance_before"] < price:
            return _fail(job, f"недостаточно звёзд: баланс {job['balance_before']} < {price}")

        if mode == "self":
            return await _flow_self(acc, job, save, w, gift, price)

        # 2. «Подарить другу»
        await human_delay(2, 4)
        since = utcnow()
        await click(find_button_by_data(w, exact=("letsSendAGiftToFriend",)))
        f = await core.wait_fresh(
            since, timeout=15, predicate=lambda m: bool(find_button_by_data(m, prefixes=("send-gift_",))))
        if not f:
            return _fail(job, "экран «Подарить другу» не открылся")
        gbtn = find_button_by_data(f, exact=(button_data(job["gift_code"]),))
        if not gbtn:
            return _fail(job, f"на экране нет подарка {button_data(job['gift_code'])}")
        _step(job, "экран «Подарить другу»", True)

        # 3. подарок
        await human_delay(2, 4)
        since = utcnow()
        alert, _a = await click(gbtn)
        if alert:
            return _fail(job, f"бот отказал на выбор подарка: {alert}", "выбор подарка")
        ask = await core.wait_fresh(since, timeout=15)
        ask_text = (ask.text or "") if ask else ""
        if not ask or "id" not in ask_text.lower():
            return _fail(job, f"бот не попросил ID друга: «{ask_text[:120]}»", "выбор подарка")
        _step(job, "выбрал подарок, бот просит ID", True, ask_text[:80])

        # 4. ID друга
        await human_delay(3, 6)
        since = utcnow()
        await safe_call(lambda: core.client.send_message(core_target(), friend), action="withdraw_send_id")
        conf = await core.wait_fresh(
            since, timeout=20,
            predicate=lambda m: bool(find_button_by_data(m, prefixes=("giftsendtofriend_calback:",)))
            or "❌" in (m.text or "") or "нужно" in (m.text or "").lower())
        if not conf:
            return _fail(job, "бот не ответил на ID друга", "отправка ID")
        cbtn = find_button_by_data(conf, prefixes=("giftsendtofriend_calback:",))
        if not cbtn:
            refusal = " ".join((conf.text or "").split())[:200]
            return _fail(job, f"бот отказал после ID: {refusal}", "отправка ID")
        data = bytes(cbtn.data).decode("utf-8", "ignore")
        conf_text = conf.text or ""
        _step(job, "бот запросил подтверждение", True, conf_text[:100].replace("\n", " "))
        # сверка: тот же друг и тот же подарок (по callback-data кнопки) — иначе НЕ подтверждаем
        if f":{friend}:" not in data or f":{job['gift_code']}:" not in data:
            return _fail(job, f"запрос бота не совпал с ожидаемым (ID/подарок) — подтверждение не нажато: {data[:80]}",
                         "сверка запроса")

        # 5. подтвердить
        await human_delay(3, 6)
        since = utcnow()
        alert, _a = await click(cbtn)
        res = None if alert else await core.wait_fresh(since, timeout=15)
        job["bot_response"] = (alert or (res.text if res else "") or "")[:300]
        if alert and any(w_ in alert.lower() for w_ in ("не ", "ошиб", "недостат", "❌")):
            return _fail(job, f"бот отказал при подтверждении: {alert}", "подтверждение")
        _step(job, "нажал «Подтвердить»", True, job["bot_response"][:100])
        job["status"] = "success"
        await save()

        # 6. проверка по балансу
        await human_delay(3, 6)
        try:
            msg = await core._open_unlocked()
            pbtn = find_button_by_data(msg, exact=("profile:show",)) if msg else None
            if pbtn:
                since = utcnow()
                await click(pbtn)
                prof = await core.wait_fresh(since, timeout=12)
                if prof:
                    await acc.profile_stats.update_from_profile_text(prof.text or "")
                    job["balance_after"] = _balance(prof.text)
                    if job["balance_after"] is None:
                        job["balance_after"] = float(acc.profile_stats.balance)
        except Exception as e:
            _step(job, "проверка баланса", False, str(e)[:100])
        b0, b1 = job["balance_before"], job["balance_after"]
        if b0 is not None and b1 is not None:
            if b0 - b1 >= price - 0.5:
                _step(job, "баланс уменьшился", True, f"{b0} → {b1}")
            else:
                job["status"] = "unverified"
                job["error"] = f"баланс не уменьшился на {price}: {b0} → {b1}"
                _step(job, "баланс не уменьшился", False, job["error"])


async def _flow_self(acc, job, save, w, gift, price):
    """Вывод СЕБЕ: на экране вывода жмём подарок gift_*. Что бот покажет дальше, точно неизвестно
    (в «Обучении» этого шага ещё нет) — поэтому: если придёт экран с кнопкой «Подтвердить» —
    нажимаем её; итог всегда проверяем по падению баланса. Весь ответ бота пишется в историю."""
    core = acc.core
    gbtn = find_button_by_data(w, exact=(gift["self_data"],))
    if not gbtn:
        return _fail(job, f"на экране вывода нет подарка {gift['self_data']}")
    await human_delay(2, 4)
    since = utcnow()
    alert, _a = await core.click_button(gbtn)
    res = None if alert else await core.wait_fresh(since, timeout=15)
    text = alert or ((res.text or "") if res else "")
    job["bot_response"] = " ".join(text.split())[:300]
    _step(job, "выбрал подарок", True, job["bot_response"][:120])
    if alert and any(x in alert.lower() for x in ("❌", "не ", "недостат", "минимум", "подпиш")):
        return _fail(job, f"бот отказал: {alert}", "выбор подарка")

    # возможный экран подтверждения
    if res is not None:
        cbtn = None
        for row in (getattr(res, "buttons", None) or []):
            for b in row:
                if getattr(b, "data", None) and "подтверд" in (b.text or "").lower():
                    cbtn = b
        if cbtn is not None:
            await human_delay(3, 6)
            since = utcnow()
            alert2, _b = await core.click_button(cbtn)
            res2 = None if alert2 else await core.wait_fresh(since, timeout=15)
            job["bot_response"] = " ".join((alert2 or ((res2.text or "") if res2 else "") or "").split())[:300]
            _step(job, "нажал «Подтвердить»", True, job["bot_response"][:120])
        elif "❌" in (res.text or ""):
            return _fail(job, f"бот отказал: {job['bot_response']}", "выбор подарка")

    job["status"] = "success"
    await save()
    await _verify_balance(acc, job, price)


async def _verify_balance(acc, job, price):
    core = acc.core
    await human_delay(3, 6)
    try:
        msg = await core._open_unlocked()
        pbtn = find_button_by_data(msg, exact=("profile:show",)) if msg else None
        if pbtn:
            since = utcnow()
            await core.click_button(pbtn)
            prof = await core.wait_fresh(since, timeout=12)
            if prof:
                await acc.profile_stats.update_from_profile_text(prof.text or "")
                job["balance_after"] = _balance(prof.text)
                if job["balance_after"] is None:
                    job["balance_after"] = float(acc.profile_stats.balance)
    except Exception as e:
        _step(job, "проверка баланса", False, str(e)[:100])
    b0, b1 = job["balance_before"], job["balance_after"]
    if b0 is not None and b1 is not None:
        if b0 - b1 >= price - 0.5:
            _step(job, "баланс уменьшился", True, f"{b0} → {b1}")
        else:
            job["status"] = "unverified"
            job["error"] = f"баланс не уменьшился на {price}: {b0} → {b1}"
            _step(job, "баланс не уменьшился", False, job["error"])


def core_target():
    from config import TARGET_BOT
    return TARGET_BOT
