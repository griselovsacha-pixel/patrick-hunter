"""
«Обучение» — авто-сбор всего, что отдаёт @patrickstarsrobot, чтобы код можно было
обновлять по актуальным данным, а не по догадкам.

Что собирается (всё дедуплицируется по «отпечатку», цифры в тексте нормализуются,
поэтому «Подожди 05:50» и «Подожди 04:12» — одна запись):
  screen      — экран бота: текст + все кнопки (подпись, тип, callback-data, ссылка)
  task        — экран, похожий на задание (есть «новое задание»/«награда»)
  notice      — сообщение бота без кнопок (уведомления, плашки, ответы)
  alert       — всплывающее окно (ответ на нажатие inline-кнопки)
  transition  — «что делает кнопка»: экран → кнопка → результат (алерт или новый экран)

Пишется в таблицу bot_knowledge через RPC patrick_knowledge_upsert (см. db_schema.py).
Сбой записи никогда не мешает работе бота. Повторные наблюдения одного и того же
отпечатка отправляются не чаще раза в KNOWLEDGE_TOUCH_SEC (счётчик seen_count —
приблизительный, «сколько раз зафиксировано»).
"""
import asyncio
import hashlib
import logging
import os
import re
import time

from config import TASK_MARKERS

logger = logging.getLogger(__name__)

ENABLED = os.getenv("KNOWLEDGE_ENABLED", "1") != "0"
TOUCH_SEC = int(os.getenv("KNOWLEDGE_TOUCH_SEC", "600"))
TRANSITION_WINDOW_SEC = 20

# Общий для всех аккаунтов процесса: fingerprint -> monotonic последней отправки
_seen: dict = {}


def normalize(text) -> str:
    """Текст без изменчивых частей: числа -> #, время -> #:#, пробелы схлопнуты."""
    t = (text or "").lower()
    t = re.sub(r"\d+([:.,]\d+)*", "#", t)
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def _fp(*parts) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]


def _title(text, n=70) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:n]
    return ""


def button_info(btn) -> dict:
    raw = getattr(btn, "button", None)
    kind = type(raw).__name__.replace("KeyboardButton", "") or "Button"
    data = getattr(btn, "data", None)
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data)[:64].decode("utf-8", "replace")
    out = {"text": (getattr(btn, "text", "") or "")[:80], "type": kind}
    if data:
        out["data"] = data
    url = getattr(btn, "url", None)
    if url:
        out["url"] = str(url)[:300]
    return out


def _buttons(msg) -> list:
    rows = []
    try:
        for row in (msg.buttons or []):
            rows.extend(button_info(b) for b in row)
    except Exception:
        pass
    return rows


def screen_kind(msg) -> str:
    low = (getattr(msg, "text", "") or "").lower()
    if not _buttons(msg):
        return "notice"
    if any(m in low for m in TASK_MARKERS):
        return "task"
    return "screen"


def screen_fp(msg) -> str:
    labels = tuple(normalize(b["text"]) for b in _buttons(msg))
    return _fp("screen", normalize(getattr(msg, "text", ""))[:300], "|".join(labels))


class Knowledge:
    def __init__(self, db, label: str):
        self.db = db
        self.label = label
        self._pending = None  # последнее нажатие кнопки, ждём чем ответил бот

    # ---------- отправка ----------
    def _push(self, fp: str, kind: str, title: str, text: str, data: dict):
        if not ENABLED or not self.db:
            return
        now = time.monotonic()
        last = _seen.get(fp)
        if last is not None and now - last < TOUCH_SEC:
            return
        _seen[fp] = now
        try:
            asyncio.get_running_loop().create_task(self._send(fp, kind, title, text, data))
        except RuntimeError:
            pass

    async def _send(self, fp, kind, title, text, data):
        try:
            await self.db.knowledge(fp, kind, title, text, data, self.label)
        except Exception as e:
            logger.debug("[%s] knowledge: %s", self.label, e)

    # ---------- входящие сообщения бота ----------
    def on_message(self, msg):
        try:
            text = getattr(msg, "text", "") or ""
            if not text and not _buttons(msg):
                return
            kind = screen_kind(msg)
            fp = screen_fp(msg)
            data = {
                "buttons": _buttons(msg),
                "has_media": bool(getattr(msg, "media", None)),
            }
            self._push(fp, kind, _title(text), text[:3000], data)
            self._finish_transition(msg, fp, kind)
        except Exception as e:
            logger.debug("[%s] knowledge.on_message: %s", self.label, e)

    # ---------- нажатия кнопок ----------
    def on_click(self, btn, alert_text):
        """Вызывается из core.click_button. alert_text — всплывающее окно или None."""
        try:
            src = getattr(btn, "message", None)
            sfp = screen_fp(src) if src is not None else ""
            stitle = _title(getattr(src, "text", "") or "") if src is not None else ""
            info = button_info(btn)
            if alert_text:
                afp = _fp("alert", normalize(alert_text))
                self._push(afp, "alert", _title(alert_text), alert_text[:1000],
                           {"trigger_button": info["text"], "from_screen": stitle})
                tfp = _fp("transition", sfp, info["text"].lower(), afp)
                self._push(tfp, "transition", f"{info['text']} → алерт", alert_text[:1000], {
                    "screen_title": stitle, "screen_fp": sfp, "button": info,
                    "outcome": "alert", "outcome_title": _title(alert_text),
                })
                self._pending = None
            else:
                self._pending = {"ts": time.monotonic(), "sfp": sfp, "stitle": stitle, "button": info}
        except Exception as e:
            logger.debug("[%s] knowledge.on_click: %s", self.label, e)

    def _finish_transition(self, msg, fp, kind):
        p = self._pending
        if not p or time.monotonic() - p["ts"] > TRANSITION_WINDOW_SEC:
            return
        self._pending = None
        if fp == p["sfp"]:
            outcome_title = "тот же экран (без изменений)"
        else:
            outcome_title = _title(getattr(msg, "text", "") or "")
        tfp = _fp("transition", p["sfp"], p["button"]["text"].lower(), fp)
        self._push(tfp, "transition", f"{p['button']['text']} → {outcome_title}"[:120],
                   (getattr(msg, "text", "") or "")[:1000], {
                       "screen_title": p["stitle"], "screen_fp": p["sfp"], "button": p["button"],
                       "outcome": "screen", "outcome_title": outcome_title, "outcome_fp": fp,
                       "outcome_kind": kind,
                   })
