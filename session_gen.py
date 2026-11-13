"""
Генератор строки сессии Telethon из админки (вкладка «Сессия»).

Два способа:
  * по номеру: api_id + api_hash + телефон → код из Telegram → (облачный пароль 2FA) → строка;
  * по QR: api_id + api_hash → QR-код, сканируем в Telegram (Настройки → Устройства →
    Подключить устройство) → (пароль 2FA) → строка.

Данные (api_hash, пароль, строка сессии) живут только в памяти процесса, пока идёт вход
(максимум SESSION_GEN_TTL_SEC, по умолчанию 600 с), нигде не пишутся и не логируются.
Одновременно не больше 3 попыток. Новый клиент создаётся без обращения к рабочим
аккаунтам — их сессии не затрагиваются.
"""
import asyncio
import os
import secrets
import time

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

TTL = int(os.getenv("SESSION_GEN_TTL_SEC", "600"))
MAX_ACTIVE = 3
_items: dict = {}


def _err(e) -> str:
    n = type(e).__name__
    known = {
        "PhoneNumberInvalidError": "Неверный номер телефона",
        "ApiIdInvalidError": "Неверная пара api_id / api_hash",
        "PhoneCodeInvalidError": "Неверный код",
        "PhoneCodeExpiredError": "Код истёк — начни заново",
        "PasswordHashInvalidError": "Неверный облачный пароль",
        "PhoneNumberBannedError": "Номер заблокирован Telegram",
        "SessionPasswordNeededError": "Нужен облачный пароль",
    }
    if isinstance(e, errors.FloodWaitError):
        return f"Telegram просит подождать {e.seconds} с (FloodWait)"
    return known.get(n) or f"{n}: {str(e)[:150]}"


async def _drop(sid):
    it = _items.pop(sid, None)
    if not it:
        return
    t = it.get("task")
    if t:
        t.cancel()
    try:
        await it["client"].disconnect()
    except Exception:
        pass


async def _gc():
    now = time.time()
    for sid in [s for s, it in _items.items() if now - it["ts"] > TTL]:
        await _drop(sid)


async def _new(api_id, api_hash, mode):
    await _gc()
    if len(_items) >= MAX_ACTIVE:
        raise RuntimeError("Слишком много активных попыток, подожди пару минут или отмени текущие")
    try:
        api_id = int(str(api_id).strip())
    except ValueError:
        raise RuntimeError("api_id должен быть числом")
    api_hash = str(api_hash).strip()
    if len(api_hash) < 16:
        raise RuntimeError("Похоже, api_hash неполный")
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    sid = secrets.token_urlsafe(12)
    _items[sid] = {"client": client, "mode": mode, "state": "init", "ts": time.time(),
                   "phone": None, "hash": None, "session": None, "error": None, "qr_url": None, "task": None}
    return sid, _items[sid]


async def _finish(it):
    it["session"] = it["client"].session.save()
    it["state"] = "done"
    try:
        await it["client"].disconnect()   # сессия уже сохранена в строку; на сервере Telegram она остаётся активной
    except Exception:
        pass


# ---------- по номеру ----------
async def start_phone(api_id, api_hash, phone) -> dict:
    sid, it = await _new(api_id, api_hash, "phone")
    try:
        phone = str(phone).strip().replace(" ", "")
        sent = await it["client"].send_code_request(phone)
        it.update(phone=phone, hash=sent.phone_code_hash, state="need_code", ts=time.time())
        return {"sid": sid, "state": "need_code"}
    except Exception as e:
        await _drop(sid)
        raise RuntimeError(_err(e))


async def submit_code(sid, code) -> dict:
    it = _items.get(sid)
    if not it or it["mode"] != "phone":
        raise RuntimeError("Сессия входа не найдена (истекла?) — начни заново")
    it["ts"] = time.time()
    try:
        await it["client"].sign_in(it["phone"], str(code).strip().replace(" ", ""), phone_code_hash=it["hash"])
    except errors.SessionPasswordNeededError:
        it["state"] = "need_password"
        return {"sid": sid, "state": "need_password"}
    except Exception as e:
        if isinstance(e, (errors.PhoneCodeExpiredError,)):
            await _drop(sid)
        raise RuntimeError(_err(e))
    await _finish(it)
    return status(sid)


async def submit_password(sid, password) -> dict:
    it = _items.get(sid)
    if not it or it["state"] != "need_password":
        raise RuntimeError("Сейчас пароль не требуется или попытка истекла")
    it["ts"] = time.time()
    try:
        await it["client"].sign_in(password=str(password))
    except Exception as e:
        raise RuntimeError(_err(e))
    await _finish(it)
    return status(sid)


# ---------- по QR ----------
async def _qr_task(sid):
    it = _items.get(sid)
    try:
        qr = await it["client"].qr_login()
        while True:
            it["qr_url"], it["state"], it["ts"] = qr.url, "qr_wait", time.time()
            try:
                await qr.wait(timeout=25)
                break
            except asyncio.TimeoutError:
                await qr.recreate()   # токен QR живёт ~30 с — обновляем
        await _finish(it)
    except errors.SessionPasswordNeededError:
        it["state"] = "need_password"
    except asyncio.CancelledError:
        raise
    except Exception as e:
        it["state"], it["error"] = "error", _err(e)


async def start_qr(api_id, api_hash) -> dict:
    sid, it = await _new(api_id, api_hash, "qr")
    it["state"] = "qr_wait"
    it["task"] = asyncio.create_task(_qr_task(sid))
    return {"sid": sid, "state": "qr_wait"}


# ---------- общее ----------
def status(sid) -> dict:
    it = _items.get(sid)
    if not it:
        return {"state": "gone", "error": "Попытка не найдена или истекла"}
    out = {"sid": sid, "state": it["state"], "mode": it["mode"], "error": it["error"]}
    if it["state"] == "qr_wait":
        out["qr_url"] = it["qr_url"]
    if it["state"] == "done":
        out["session"] = it["session"]
    return out


async def cancel(sid):
    await _drop(sid)


async def forget_done(sid):
    """После того как строка показана — убираем её из памяти."""
    await _drop(sid)
