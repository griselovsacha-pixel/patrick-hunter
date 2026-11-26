"""
Утилиты без состояния — чистые функции, никакого Telegram-клиента тут нет.
Если фиксишь баг в парсинге текста/времени — тебе сюда, а не в account.py.
"""
import re
import random
import asyncio
from collections import OrderedDict

from config import PROMO_KEYWORDS, IGNORED_WORDS, CONFIRM_WORDS, SPONSOR_GATE_HINTS, BIO_LINK_HINTS
from config import TASK_INTERVAL_MIN_SEC, TASK_INTERVAL_MAX_SEC
from config import (
    TASK_MARKERS, TASK_FAIL_WORDS, TASK_OK_WORDS,
    CLICK_OK_WORDS, CLICK_COOLDOWN_WORDS, CLICK_LIMIT_WORDS, CLICK_GATE_WORDS,
)
import hashlib
from urllib.parse import parse_qsl


async def human_delay(min_sec: float = 2.0, max_sec: float = 4.0):
    await asyncio.sleep(random.uniform(min_sec, max_sec))


URL_RE = re.compile(r'(?:https?://|tg://|(?:www\.)?t\.me/|telegram\.me/)\S+', re.IGNORECASE)

_START_LINK_RE = re.compile(
    r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{3,})/?\?start=([A-Za-z0-9_\-]+)',
    re.IGNORECASE,
)
_TG_RESOLVE_RE = re.compile(
    r'tg://resolve\?domain=([A-Za-z0-9_]{3,})&start=([A-Za-z0-9_\-]+)', re.IGNORECASE,
)


class BoundedSet:
    """Множество с ограничением размера (вытесняет самые старые).
    Нужно, чтобы кэши «уже видели/активировали» не росли бесконечно."""

    def __init__(self, maxlen: int = 200):
        self.maxlen = maxlen
        self._d: "OrderedDict[object, None]" = OrderedDict()

    def add(self, item) -> None:
        self._d.pop(item, None)
        self._d[item] = None
        while len(self._d) > self.maxlen:
            self._d.popitem(last=False)

    def __contains__(self, item) -> bool:
        return item in self._d

    def __len__(self) -> int:
        return len(self._d)


def collect_urls(message) -> list:
    """Все ссылки поста: из текста, из entity (текст со скрытой ссылкой) и
    из URL-кнопок под постом."""
    urls = []
    raw = getattr(message, "raw_text", None) or getattr(message, "text", None) or ""
    urls += re.findall(r'(?:https?://|tg://)\S+|(?:t\.me|telegram\.me)/\S+', raw)
    for ent in getattr(message, "entities", None) or []:
        u = getattr(ent, "url", None)
        if u:
            urls.append(u)
    try:
        rows = message.buttons or []
    except Exception:
        rows = []
    for row in rows:
        for btn in row:
            u = getattr(btn, "url", None)
            if u:
                urls.append(u)
    return urls


def button_texts(message) -> str:
    """Подписи всех кнопок под постом (нижний регистр) — «Получить 111 ⭐» и т.п."""
    try:
        rows = message.buttons or []
    except Exception:
        rows = []
    return " ".join((getattr(b, "text", "") or "").lower() for row in rows for b in row)


def extract_start_links(message) -> list:
    """[(bot_username_lower, payload), ...] из ссылок вида
    t.me/<bot>?start=<payload> / tg://resolve?domain=<bot>&start=<payload>.
    Дубликаты убираются, порядок сохраняется."""
    found, seen = [], set()
    for url in collect_urls(message):
        for rx in (_START_LINK_RE, _TG_RESOLVE_RE):
            for bot, payload in rx.findall(url):
                key = (bot.lower(), payload)
                if key not in seen:
                    seen.add(key)
                    found.append(key)
    return found


def find_button(msg, words):
    """Первая кнопка сообщения, в тексте которой есть любое из слов."""
    if not msg or not msg.buttons:
        return None
    for row in msg.buttons:
        for btn in row:
            low = btn.text.lower()
            if any(w in low for w in words):
                return btn
    return None


def find_button_by_data(msg, prefixes=(), exact=()):
    """Callback-кнопка по её data (реальные значения из «Обучения»: task:ID:1,
    skip_task:ID, tasks, main_menu…). Надёжнее подписи: тексты кнопок у бота меняются
    («Подтвердить запуск», «Подтвердить подписку», «Я сделал(а)»)."""
    if not msg or not msg.buttons:
        return None
    for row in msg.buttons:
        for btn in row:
            data = getattr(btn, "data", None)
            if not data:
                continue
            d = bytes(data).decode("utf-8", "ignore")
            if d in exact or any(d.startswith(p) for p in prefixes):
                return btn
    return None


def solve_math(text: str):
    match = re.search(r'(\d+)\s*([\+\-\*\/])\s*(\d+)\s*=\s*\?', text)
    if match:
        a, op, b = int(match.group(1)), match.group(2), int(match.group(3))
        if op == '+': return a + b
        if op == '-': return a - b
        if op == '*': return a * b
        if op == '/' and b != 0: return a // b
    expression = re.sub(r'[^\d\+\-\*\/\(\)\s]', '', text)
    return _safe_arith(expression)


def _safe_arith(expression: str):
    """Арифметика из текста бота БЕЗ eval: только + - * / и скобки над целыми. Раньше здесь был eval():
    строка вида «9**9**9**9» (оператор * разрешён дважды подряд) вешала весь event loop —
    то есть ВСЕ аккаунты процесса разом, а не один."""
    import ast
    expression = (expression or "").strip()
    if not expression or len(expression) > 60:
        return None
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError):
        return None

    def ev(n, depth=0):
        if depth > 12:
            raise ValueError("слишком глубоко")
        if isinstance(n, ast.Expression):
            return ev(n.body, depth + 1)
        if isinstance(n, ast.Constant) and isinstance(n.value, int) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = ev(n.operand, depth + 1)
            return v if isinstance(n.op, ast.UAdd) else -v
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv)):
            a, b = ev(n.left, depth + 1), ev(n.right, depth + 1)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if b == 0:
                raise ValueError("деление на ноль")
            return a // b
        raise ValueError("недопустимое выражение")   # в т.ч. ** и вызовы

    try:
        return int(ev(tree))
    except (ValueError, OverflowError, RecursionError):
        return None


def random_task_interval() -> int:
    base = random.randint(TASK_INTERVAL_MIN_SEC, TASK_INTERVAL_MAX_SEC)
    return base + random.randint(0, 300)


def is_webapp_button(btn) -> bool:
    btn_text = btn.text.lower()
    if any(w in btn_text for w in ["запустить приложение", "открыть приложение", "webapp", "open app"]):
        return True
    raw = getattr(btn, 'button', btn)  # Telethon-обёртка MessageButton хранит настоящую кнопку в .button
    if type(raw).__name__ in ['KeyboardButtonWebView', 'InlineKeyboardButtonWebApp', 'KeyboardButtonSimpleWebView']:
        return True
    url = getattr(btn, 'url', None)
    if url and any(w in url.lower() for w in ['startapp', 'tgwebapp']):
        return True
    return False


def parse_next_daily_seconds(text: str) -> int:
    """Парсит время до следующей попытки.

    ВАЖНО (нашли на реальном алерте): текст вида "Бонус можно получить раз
    в 24 часа! Осталось: 6 ч 51 мин" содержит ДВА числа с "час" — "24 часа"
    (общее правило) и "6 ч 51 мин" (реально оставшееся время). Простой
    re.search берёт первое попавшееся совпадение по всему тексту и раньше
    возвращал 24ч вместо настоящих 6ч51м. Поэтому сначала ищем именно
    после слова "Осталось" / "останется" / "через", и только если такого
    маркера нет — фолбэк на первое найденное число с "час"/"ч"."""
    text_lower = text.lower()

    remaining_match = re.search(
        r'(?:осталось|останется|через)\s*:?\s*(\d+)\s*ч(?:ас(?:ов|а)?)?\s*(?:(\d+)\s*мин)?',
        text_lower,
    )
    if remaining_match:
        hours = int(remaining_match.group(1))
        minutes = int(remaining_match.group(2)) if remaining_match.group(2) else 0
        return hours * 3600 + minutes * 60 + 300

    # Фолбэк на минуты после "осталось", если часов нет вообще ("осталось 40 мин")
    remaining_min_only = re.search(r'(?:осталось|останется|через)\s*:?\s*(\d+)\s*мин', text_lower)
    if remaining_min_only:
        return int(remaining_min_only.group(1)) * 60 + 300

    # Общий фолбэк (старое поведение) — на случай текста без явного маркера
    hours, minutes = 0, 0
    h_match = re.search(r'(\d+)\s*ч(?:ас(?:ов|а)?)?\b', text_lower)
    if h_match:
        hours = int(h_match.group(1))
    m_match = re.search(r'(\d+)\s*мин', text_lower)
    if m_match:
        minutes = int(m_match.group(1))
    if hours == 0 and minutes == 0:
        return 24 * 3600
    return hours * 3600 + minutes * 60 + 300


def find_promo_in_text(text: str):
    """Ищет промокод в тексте поста канала. См. историю правок в комментариях
    ниже — раньше ловился только формат 'Активаций: CODE'."""
    if not text:
        return None
    lower_text = text.lower()
    if not any(w in lower_text for w in PROMO_KEYWORDS):
        return None

    # Ссылки вырезаем ДО чистки пунктуации: раньше из t.me/bot?start=880628963
    # в фолбэк попадал токен "880628963" и принимался за промокод (есть
    # цифры). Коды внутри deep-link ловит отдельный путь (extract_start_links).
    text = URL_RE.sub(" ", text)
    clean_text = text.replace("**", " ").replace("__", " ").replace("`", " ").replace("||", " ")
    for char in [".", ",", "!", "?", ":", ";", "()", "[]", "»", "«", "—", "–"]:
        clean_text = clean_text.replace(char, " ")

    exact = re.search(r'активаций\s+([a-zA-Z0-9]{3,20})', clean_text, re.IGNORECASE)
    if exact and exact.group(1).lower() not in IGNORED_WORDS:
        return exact.group(1)

    promo_match = re.search(r'промо[\s\-]?код\w*\s+([a-zA-Z0-9]{3,20})', clean_text, re.IGNORECASE)
    if promo_match and promo_match.group(1).lower() not in IGNORED_WORDS:
        return promo_match.group(1)

    promo_en_match = re.search(r'promo(?:\s*code)?\s+([a-zA-Z0-9]{3,20})', clean_text, re.IGNORECASE)
    if promo_en_match and promo_en_match.group(1).lower() not in IGNORED_WORDS:
        return promo_en_match.group(1)

    # Фолбэк: пост явно про промо, формат незнакомый — берём токен, похожий на код
    for word in re.findall(r'\b([A-Za-z0-9]{4,20})\b', clean_text):
        wl = word.lower()
        if wl in IGNORED_WORDS:
            continue
        if word.isupper() or bool(re.search(r'\d', word)):
            return word

    return None


def looks_like_sponsor_gate(msg) -> bool:
    if not msg or not msg.buttons:
        return False
    text = (msg.text or "").lower()
    if not any(h in text for h in SPONSOR_GATE_HINTS):
        return False

    has_confirm = False
    has_link = False
    for row in msg.buttons:
        for btn in row:
            btn_text = btn.text.lower()
            if any(w in btn_text for w in CONFIRM_WORDS):
                has_confirm = True
            if getattr(btn, 'url', None) or is_webapp_button(btn):
                has_link = True  # ссылка на спонсора ИЛИ мини-апп (Flyer и т.п.)

    return has_confirm and has_link


def looks_like_bio_link_popup(text: str) -> bool:
    """Всплывашка ежедневки вида 'поставь свою личную ссылку в описание,
    чтобы получать бонус' — см. config.BIO_LINK_HINTS."""
    lower = (text or "").lower()
    return any(h in lower for h in BIO_LINK_HINTS)


def parse_profile_text(text: str) -> dict:
    """Парсит экран '✨ Профиль' в словарь с реальными цифрами аккаунта.
    Формат (см. пример от пользователя):
        👥 Всего друзей: 87
        ✅ Активировали бота: 39
        🔄 Повторные активации: 9
        💰 Баланс: 9.65 ⭐️
    Любое поле, которое не нашли, просто отсутствует в результате —
    вызывающий код обязан на это рассчитывать (см. profile_stats.py).
    """
    result = {}
    if not text:
        return result

    balance_m = re.search(r'Баланс:\s*([\d]+[.,]?\d*)\s*⭐', text)
    if balance_m:
        result['balance'] = float(balance_m.group(1).replace(',', '.'))

    friends_m = re.search(r'Всего друзей:\s*(\d+)', text)
    if friends_m:
        result['friends'] = int(friends_m.group(1))

    activated_m = re.search(r'Активировали бота:\s*(\d+)', text)
    if activated_m:
        result['activated'] = int(activated_m.group(1))

    reactivations_m = re.search(r'Повторные активации:\s*(\d+)', text)
    if reactivations_m:
        result['reactivations'] = int(reactivations_m.group(1))

    id_m = re.search(r'ID:\s*(\d+)', text)
    if id_m:
        result['tg_id'] = int(id_m.group(1))

    username_m = re.search(r'Username:\s*@?(\w+)', text)
    if username_m:
        result['username'] = username_m.group(1)

    return result


# ---------------------------------------------------------------------------
# Разбор ссылок t.me, сообщений-заданий и ответов бота
# ---------------------------------------------------------------------------
_TME_RE = re.compile(r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([^\s?#]+)(?:\?([^\s#]*))?', re.IGNORECASE)
_TME_SERVICE = {
    "c", "addstickers", "addemoji", "share", "proxy", "socks", "login", "boost",
    "setlanguage", "addtheme", "iv", "invoice", "giftcode", "m", "contact",
    "addlist", "confirmphone", "bg",
}


def parse_tme_target(url: str):
    """Разбор ссылки t.me -> {'kind', 'target', 'start'} или None.

    kind: 'invite' (target='+HASH'), 'channel' (публичный username),
          'bot' (start — payload из ?start=), 'app' (мини-апп, start — startapp).

    ВАЖНО: инвайт-хэш может содержать '-' и '_'. Раньше регэксп [a-zA-Z0-9_]
    обрезал его по первому '-' (получалось «+RfN») — вступление падало с
    «chat has expired», а в логе всё равно писалось «подписался»."""
    m = _TME_RE.search(url or "")
    if not m:
        return None
    parts = [p for p in m.group(1).split("/") if p]
    if not parts:
        return None
    params = dict(parse_qsl(m.group(2) or "", keep_blank_values=True))
    first = parts[0]

    if first.lower() == "joinchat" and len(parts) > 1:
        return {"kind": "invite", "target": "+" + parts[1], "start": None}
    if first.startswith("+"):
        if re.fullmatch(r"\+[A-Za-z0-9_\-]{6,}", first) and not first[1:].isdigit():  # +7999... - телефон, не инвайт
            return {"kind": "invite", "target": first, "start": None}
        return None
    if first.lower() in _TME_SERVICE:
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", first):
        return None
    if first.lower().endswith("bot"):
        if "startapp" in params or (len(parts) > 1 and not parts[1].isdigit()):
            return {"kind": "app", "target": first, "start": params.get("startapp")}
        return {"kind": "bot", "target": first, "start": params.get("start")}
    return {"kind": "channel", "target": first, "start": None}


def is_task_message(msg) -> bool:
    """Сообщение бота — именно ЗАДАНИЕ (а не меню/профиль/поздравление)."""
    low = (getattr(msg, "text", None) or "").lower()
    return any(m in low for m in TASK_MARKERS)


def parse_reward(text: str):
    m = re.search(r'награда:?\s*\**\s*(\d+(?:[.,]\d+)?)', (text or "").lower())
    return float(m.group(1).replace(",", ".")) if m else None


def classify_task_alert(text) -> str:
    """Ответ бота на «Проверить»: 'ok' | 'fail' | 'unknown'. Провал проверяем
    первым: «❌ Задание не выполнено» содержит слово «выполнено»."""
    low = (text or "").lower()
    if not low:
        return "unknown"
    if any(w in low for w in TASK_FAIL_WORDS):
        return "fail"
    if any(w in low for w in TASK_OK_WORDS):
        return "ok"
    return "unknown"


def classify_click_alert(text) -> str:
    """Всплывающее окно кликера: 'limit' | 'cooldown' | 'gate' | 'ok' | 'unknown'."""
    low = (text or "").lower()
    if any(w in low for w in CLICK_LIMIT_WORDS):
        return "limit"
    if any(w in low for w in CLICK_COOLDOWN_WORDS):
        return "cooldown"
    if "задани" in low and any(w in low for w in CLICK_GATE_WORDS):
        return "gate"
    if any(w in low for w in CLICK_OK_WORDS):
        return "ok"
    return "unknown"


def parse_cooldown_seconds(text: str) -> int:
    """'Подожди 05:10 перед следующим кликом' -> 310. Нет времени -> 0."""
    m = re.search(r'(\d{1,2}):(\d{2})', text or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


def message_signature(msg) -> tuple:
    """Отпечаток сообщения-задания: id + текст + кнопки. Бот часто РЕДАКТИРУЕТ
    одно сообщение в следующее задание, поэтому сравнивать по одному id нельзя
    (раньше новое задание принималось за «зациклилось» и пропускалось вслепую)."""
    btns = []
    for row in (getattr(msg, "buttons", None) or []):
        for b in row:
            btns.append((b.text, getattr(b, "url", None), getattr(b, "data", None)))
    h = hashlib.md5(((getattr(msg, "text", None) or "") + repr(btns)).encode("utf-8", "ignore")).hexdigest()[:12]
    return (getattr(msg, "id", None), h)
