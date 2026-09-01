import re
import os
import sys
import random
import asyncio
import logging
import datetime
import urllib.request
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, StartBotRequest
from telethon.tl.functions.folders import EditPeerFoldersRequest
from telethon.tl.types import InputFolderPeer, InputPeerChannel, InputPeerUser
from telethon.errors import UserAlreadyParticipantError, InviteHashInvalidError

# --- Конфигурация ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

API_ID = 31206776
API_HASH = '7ca69453a014d6a87d67afe4d9df8284'
TARGET_BOT = 'patrickstarsrobot'
CHANNEL_SOURCE = 'patrickstarsfarm'
TARGET_USER = 'sanzezzz'
TIMEZONE_OFFSET = 3  # UTC+3

# Кликер: рандомим количество кликов в день в этом диапазоне (вкл.)
CLICKER_MIN_PER_DAY = 20
CLICKER_MAX_PER_DAY = 25

ALL_DONE_PHRASES = [
    "выполнил все задания", "все задания выполнены",
    "приходи через", "новых заданий нет",
    "заданий больше нет", "респект", "молодец",
]

# Слова-маркеры кнопки подтверждения выполнения задания/подписки.
CONFIRM_WORDS = [
    "подтвер", "выдать", "проверить", "claim", "check", "выполнил",
    "готов", "получить", "забрать", "collect", "confirm", "done",
    "изучил", "ознакомил", "принять", "я подписал", "продолжить",
]

# Маркеры сообщения-"гейта": бот просит подписаться на N спонсоров, чтобы продолжить фарм
SPONSOR_GATE_HINTS = [
    "для продолжения", "выполни всего", "выполни задани", "чтобы продолжить",
]

# Ключевые слова, по которым понимаем, что пост в канале вообще про промокод
PROMO_KEYWORDS = [
    "промокод", "промо-код", "промо код", "активац", "promo code", "promo",
]

IGNORED_WORDS = {
    "html", "http", "https", "main", "patrickstarsrobot", "patrickstarsfarm",
    "profile", "claim", "check", "start", "webapp", "join", "bot", "stars",
    "tgwebapp", "startapp", "go", "app", "link", "channel", "group", "send",
    "ловите", "вводим", "боте", "профиль", "промокод", "дейли", "друзья",
    "патрик", "всем", "привет", "получить", "звезд", "депозит", "игры",
    "первым", "днем", "лета", "поздравляет", "патрика", "хантер", "farm",
    "новый", "новая", "новое", "актив", "код", "активируй", "активируйте",
    "успей", "успейте", "лимит", "штук", "шт", "первых",
}

FRUIT_MAP = {
    "🍎": ["яблок", "apple", "red apple"], "🍏": ["зелен яблок", "green apple"],
    "🍊": ["апельсин", "orange", "мандарин", "mandarin", "tangerine"],
    "🍋": ["лимон", "lemon"], "🍋‍🟩": ["лайм", "lime"],
    "🍇": ["виноград", "grape"], "🍓": ["клубник", "strawberry"],
    "🫐": ["черник", "голубик", "blueberry"], "🍒": ["вишн", "черешн", "cherry"],
    "🍑": ["персик", "peach"], "🍍": ["ананас", "pineapple"],
    "🥭": ["манго", "mango"], "🍌": ["банан", "banana"],
    "🍉": ["арбуз", "watermelon"], "🍈": ["дын", "melon"],
    "🥝": ["киви", "kiwi"], "🍐": ["груш", "pear"],
    "🍅": ["помидор", "томат", "tomato"], "🫒": ["маслин", "олив", "olive"],
    "🥥": ["кокос", "coconut"], "🥑": ["авокадо", "avocado"],
    "🌽": ["кукуруз", "corn", "corncob"], "🥕": ["морков", "carrot"],
    "🫑": ["болгарск перец", "bell pepper", "sweet pepper"],
    "🌶️": ["остр перец", "чили", "chili", "chilli", "hot pepper"],
    "🥒": ["огурец", "огурц", "cucumber"], "🥦": ["брокколи", "broccoli"],
    "🥬": ["капуст", "салат лист", "lettuce", "leafy green"],
    "🧄": ["чеснок", "garlic"], "🧅": ["лук", "onion"],
    "🥔": ["картош", "картофел", "potato"],
    "🍆": ["баклажан", "eggplant", "aubergine"], "🍄": ["гриб", "mushroom"],
    "🥜": ["арахис", "орех", "peanut"], "🌰": ["каштан", "chestnut"],
    "🫘": ["фасол", "боб", "bean", "beans"], "🫛": ["горох", "pea", "peas"],
    "🍞": ["хлеб", "bread"], "🥐": ["круасан", "croissant"],
}


# --- Загрузка аккаунта из env ---
# Раньше здесь читалось несколько сессий (TELEGRAM_SESSION, _2, _3, ...) для
# мультиаккаунт-режима. Оставляем только один — первый/основной TELEGRAM_SESSION.
def load_account_sessions():
    sessions = []
    primary = os.getenv("TELEGRAM_SESSION")
    if primary:
        sessions.append(("Аккаунт 1", primary))
    return sessions


ACCOUNT_SESSIONS = load_account_sessions()
MULTI_ACCOUNT = False  # мультиаккаунт-режим отключён, оставлен один аккаунт


# --- Утилиты без привязки к конкретному аккаунту ---
def solve_math(text: str):
    match = re.search(r'(\d+)\s*([\+\-\*\/])\s*(\d+)\s*=\s*\?', text)
    if match:
        a, op, b = int(match.group(1)), match.group(2), int(match.group(3))
        if op == '+': return a + b
        if op == '-': return a - b
        if op == '*': return a * b
        if op == '/' and b != 0: return a // b
    expression = re.sub(r'[^\d\+\-\*\/\(\)\s]', '', text)
    try:
        return int(eval(expression))
    except Exception:
        return None


async def human_delay(min_sec: float = 2.0, max_sec: float = 4.0):
    await asyncio.sleep(random.uniform(min_sec, max_sec))


def random_task_interval() -> int:
    base = random.randint(2 * 3600 + 15 * 60, 3 * 3600 + 45 * 60)
    return base + random.randint(0, 300)


def is_webapp_button(btn) -> bool:
    btn_text = btn.text.lower()
    if any(w in btn_text for w in ["запустить приложение", "открыть приложение", "webapp", "open app"]):
        return True
    if type(btn).__name__ in ['KeyboardButtonWebView', 'InlineKeyboardButtonWebApp', 'KeyboardButtonSimpleWebView']:
        return True
    url = getattr(btn, 'url', None)
    if url and any(w in url.lower() for w in ['startapp', 'tgwebapp']):
        return True
    return False


def parse_next_daily_seconds(text: str) -> int:
    text_lower = text.lower()
    hours, minutes = 0, 0
    h_match = re.search(r'(\d+)\s*час', text_lower)
    if h_match:
        hours = int(h_match.group(1))
    m_match = re.search(r'(\d+)\s*мин', text_lower)
    if m_match:
        minutes = int(m_match.group(1))
    if hours == 0 and minutes == 0:
        return 24 * 3600
    total = hours * 3600 + minutes * 60 + 300
    return total


def find_promo_in_text(text: str):
    """Ищет промокод в тексте поста канала.

    Расширено по сравнению с прошлой версией: раньше ловился только формат
    'Активаций: CODE' / 'активаций CODE'. Теперь дополнительно распознаём:
      - 'Промокод: CODE' / 'промо-код CODE' / 'промо код: CODE'
      - произвольный формат: если в тексте есть промо-ключевые слова, но
        ни один из явных шаблонов не сработал — берём первый "похожий на
        код" токен (капс и/или содержит цифры), которого нет в стоп-листе.
    """
    if not text:
        return None
    lower_text = text.lower()
    if not any(w in lower_text for w in PROMO_KEYWORDS):
        return None

    clean_text = text.replace("**", " ").replace("__", " ").replace("`", " ").replace("||", " ")
    for char in [".", ",", "!", "?", ":", ";", "()", "[]", "»", "«", "—", "–"]:
        clean_text = clean_text.replace(char, " ")

    # 1) "Активаций: CODE" / "активаций CODE" (старый формат)
    exact = re.search(r'активаций\s+([a-zA-Z0-9]{3,20})', clean_text, re.IGNORECASE)
    if exact and exact.group(1).lower() not in IGNORED_WORDS:
        return exact.group(1)

    # 2) "Промокод CODE" / "промо код CODE" / "промо-код CODE"
    promo_match = re.search(r'промо[\s\-]?код\w*\s+([a-zA-Z0-9]{3,20})', clean_text, re.IGNORECASE)
    if promo_match and promo_match.group(1).lower() not in IGNORED_WORDS:
        return promo_match.group(1)

    # 3) "promo code CODE" / "promo CODE"
    promo_en_match = re.search(r'promo(?:\s*code)?\s+([a-zA-Z0-9]{3,20})', clean_text, re.IGNORECASE)
    if promo_en_match and promo_en_match.group(1).lower() not in IGNORED_WORDS:
        return promo_en_match.group(1)

    # 4) Фолбэк: пост явно про промо, но формат незнакомый — берём первый
    # токен, похожий на код (ВЕРХНИЙ РЕГИСТР и/или содержит цифру), которого
    # нет в стоп-листе. Может иногда ошибаться — если словит не то слово,
    # пришли пример поста, донастроим точнее.
    for word in re.findall(r'\b([A-Za-z0-9]{4,20})\b', clean_text):
        wl = word.lower()
        if wl in IGNORED_WORDS:
            continue
        looks_like_code = word.isupper() or bool(re.search(r'\d', word))
        if looks_like_code:
            return word

    return None


def looks_like_sponsor_gate(msg) -> bool:
    """Определяет сообщение вида:
    '💫 Для продолжения фарма звёзд, выполни всего 3 задания...' —
    несколько кнопок-каналов + одна кнопка подтверждения ("Я выполнил(а)" и т.п.)."""
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
            if getattr(btn, 'url', None):
                has_link = True

    return has_confirm and has_link


def safe_click_exception_text(e: Exception) -> str:
    return str(e)[:80]


# ======================================================================
#  PriorityLock — лок с приоритетом.
#  Промокоды и чеки должны обрабатываться максимально быстро, даже если
#  прямо сейчас идёт долгий цикл заданий (30 заданий подряд, каждое с
#  человеческими задержками). Обычный asyncio.Lock отдаёт лок строго в
#  порядке очереди (FIFO), поэтому промокод мог реально ждать очень долго.
#  Здесь high-priority ожидающие (промокоды) встают в очередь ПЕРЕД любыми
#  low-priority ожидающими (задания/ежедневка/кликер), как только лок
#  освобождается — то есть максимум ждём завершения ТЕКУЩЕГО шага, а не
#  всей очереди обычных задач.
# ======================================================================
class PriorityLock:
    def __init__(self):
        self._locked = False
        self._high_waiting = 0
        self._cv = asyncio.Condition()

    @asynccontextmanager
    async def acquire(self, high_priority: bool = False):
        async with self._cv:
            if high_priority:
                self._high_waiting += 1
            try:
                while self._locked or (not high_priority and self._high_waiting > 0):
                    await self._cv.wait()
                self._locked = True
            finally:
                if high_priority:
                    self._high_waiting -= 1
        try:
            yield
        finally:
            async with self._cv:
                self._locked = False
                self._cv.notify_all()


# ======================================================================
#  PatrickAccount — вся логика фарма для аккаунта.
# ======================================================================
class PatrickAccount:
    def __init__(self, label: str, session_string: str):
        self.label = label
        self.client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

        # Приоритетный лок на переписку с TARGET_BOT (см. PriorityLock выше).
        self.bot_lock = PriorityLock()

        # Очередь входящих сообщений от TARGET_BOT — для событийного (мгновенного)
        # ожидания ответа вместо опроса истории раз в 1.5с. См. register_bot_dm_handler().
        self.bot_msg_queue: asyncio.Queue = asyncio.Queue(maxsize=50)

        self.activated_promos = set()
        self.promo_in_progress = set()
        self.claimed_checks = set()

        self.stats = self._fresh_stats()

    @staticmethod
    def _fresh_stats():
        return {
            "daily_collected": False,
            "daily_errors": 0,
            "tasks_completed": 0,
            "tasks_skipped": 0,
            "promos_activated": [],
            "clicker_clicks": 0,
            "clicker_target": 0,
            "errors": [],
            "stars_earned": 0.0,
        }

    def reset_stats(self):
        self.stats = self._fresh_stats()

    def add_error(self, msg: str):
        self.stats["errors"].append(f"{datetime.datetime.now().strftime('%H:%M')} {msg[:60]}")
        if len(self.stats["errors"]) > 20:
            self.stats["errors"] = self.stats["errors"][-20:]

    # --- Отчёты владельцу ---
    async def send_report(self, text: str):
        prefixed = f"[{self.label}]\n{text}" if MULTI_ACCOUNT else text
        try:
            await self.client.send_message(TARGET_USER, prefixed)
            logger.info(f"📤 [{self.label}] Отчёт: {text[:80]}")
        except Exception as e:
            logger.error(f"[{self.label}] Ошибка отправки отчёта: {e}")

    # --- Архивировать чат (папка 1 = архив) ---
    async def archive_chat(self, entity):
        try:
            peer = await self.client.get_input_entity(entity)
            await self.client(EditPeerFoldersRequest(
                folder_peers=[InputFolderPeer(peer=peer, folder_id=1)]
            ))
            logger.info(f"📁 [{self.label}] Архивировал: {entity}")
        except Exception as e:
            logger.warning(f"⚠️ [{self.label}] Не удалось архивировать {entity}: {e}")

    # --- Вступление в канал/группу + архивирование ---
    async def safe_join(self, target: str):
        try:
            if target.startswith('+'):
                await self.client(ImportChatInviteRequest(target[1:]))
                logger.info(f"✅ [{self.label}] Вступил по инвайту: {target}")
            else:
                await self.client(JoinChannelRequest(target))
                logger.info(f"✅ [{self.label}] Подписался: @{target}")
            await human_delay(2, 4)
            await self.archive_chat(target)
            await human_delay(2, 4)
        except UserAlreadyParticipantError:
            logger.info(f"ℹ️ [{self.label}] Уже подписан: {target}")
            await self.archive_chat(target)
        except InviteHashInvalidError:
            logger.warning(f"⚠️ [{self.label}] Невалидный инвайт: {target}")
        except Exception as e:
            logger.error(f"[{self.label}] Ошибка вступления {target}: {e}")

    # --- Ждём свежий ответ от бота (медленный, опросом истории чата) ---
    async def get_bot_response(self, max_age: int = 120, attempts: int = 8):
        for i in range(attempts):
            now = datetime.datetime.now(datetime.timezone.utc)
            async for msg in self.client.iter_messages(TARGET_BOT, limit=5):
                age = (now - msg.date).total_seconds()
                if age < max_age and (msg.text or msg.buttons):
                    return msg
            await asyncio.sleep(1.5)
        async for msg in self.client.iter_messages(TARGET_BOT, limit=1):
            return msg
        return None

    # --- Ждём свежий ответ от бота (быстрый, событийный) ---
    # В отличие от get_bot_response() (опрос истории раз в 1.5с — теряли
    # секунды на каждом шаге промо-активации), здесь мы подписаны на новые
    # сообщения от бота через register_bot_dm_handler() и просыпаемся сразу,
    # как только Telegram доставил ответ. `since` отсекает случайные старые/
    # застрявшие в очереди сообщения — принимаем только то, что реально
    # пришло ПОСЛЕ нашего действия, так что порядок шагов не может сбиться.
    async def wait_fresh_bot_response(self, since: datetime.datetime, timeout: float = 10.0):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(self.bot_msg_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if msg.date > since and (msg.text or msg.buttons):
                return msg
            # старое/повторное сообщение — игнорируем, ждём дальше
        # Событие не пришло вовремя (пропущено/лаг) — подстраховка опросом истории
        return await self.get_bot_response()

    def _drain_bot_queue(self):
        while not self.bot_msg_queue.empty():
            try:
                self.bot_msg_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _open_bot_unlocked(self):
        """Тело открытия бота БЕЗ захвата bot_lock. Вызывать только если лок уже взят снаружи."""
        since = datetime.datetime.now(datetime.timezone.utc)
        await self.client.send_message(TARGET_BOT, '/start')
        msg = await self.wait_fresh_bot_response(since, timeout=10)
        if not msg:
            return None

        # Разруливаем цепочку экранов-блокеров: капча и/или гейт "подпишись на спонсоров".
        for _ in range(4):
            if not msg:
                return None
            text_lower = (msg.text or "").lower()

            if "проверка" in text_lower:
                since = datetime.datetime.now(datetime.timezone.utc)
                solved = await self.solve_captcha_if_needed(msg)
                if not solved:
                    return None
                msg = await self.wait_fresh_bot_response(since, timeout=8)
                continue

            if looks_like_sponsor_gate(msg):
                msg = await self.handle_sponsor_gate(msg)
                continue

            break

        return msg

    async def open_bot(self):
        """Открыть бота как отдельную атомарную операцию (сама берёт лок)."""
        async with self.bot_lock.acquire():
            return await self._open_bot_unlocked()

    # --- WebApp ---
    async def handle_startapp(self, url: str) -> bool:
        try:
            match = re.search(r't\.me/([a-zA-Z0-9_]+)(?:/\w+)?\?startapp=([a-zA-Z0-9_]+)', url)
            if not match:
                return False
            bot_username = match.group(1)
            start_param = match.group(2)
            logger.info(f"🚀 [{self.label}] StartApp: @{bot_username} param={start_param}")
            bot = await self.client.get_entity(bot_username)
            await self.client(StartBotRequest(bot=bot, peer=bot, start_param=start_param))
            logger.info(f"✅ [{self.label}] StartApp запущен: @{bot_username}")
            await self.archive_chat(bot_username)
            await human_delay(25, 35)
            return True
        except Exception as e:
            logger.error(f"⚠️ [{self.label}] StartApp ошибка: {e}")
            return False

    # --- Капча ---
    async def solve_captcha_if_needed(self, msg) -> bool:
        if not msg:
            return False
        text = msg.text or ""
        if "проверка на робота" not in text.lower():
            return True

        logger.info(f"🤖 [{self.label}] Капча: {text[:100]}")
        if not msg.buttons:
            await self.send_report("⚠️ Капча без кнопок")
            return False

        all_btns = [b.text for row in msg.buttons for b in row]

        # Математика
        res = solve_math(text)
        if res is not None:
            for row in msg.buttons:
                for btn in row:
                    if btn.text.strip() == str(res):
                        try:
                            await btn.click()
                        except Exception as e:
                            logger.warning(f"⚠️ [{self.label}] Клик капчи не удался: {e}")
                            return False
                        logger.info(f"✅ [{self.label}] Матем. капча: {res}")
                        await human_delay(2, 3)
                        await self.send_report(f"🤖 Капча решена! Ответ: {res}")
                        return True
            await self.send_report(f"⚠️ Капча: {res} не найден среди {all_btns}")
            return False

        # Фрукты/овощи
        clean_text = re.sub(r"\*+", "", text).lower()
        word_match = re.search(r"[«\"'“”‘’](.+?)[»\"'“”‘’]", clean_text)
        if word_match:
            target_word = word_match.group(1).lower().strip()
            target_emoji = None
            for emoji, keywords in FRUIT_MAP.items():
                for kw in keywords:
                    if kw in target_word or target_word in kw:
                        target_emoji = emoji
                        break
                if target_emoji:
                    break

            for row in msg.buttons:
                for btn in row:
                    btn_text = btn.text.strip()
                    if (target_emoji and target_emoji in btn_text) or target_word in btn_text.lower():
                        try:
                            await btn.click()
                        except Exception as e:
                            logger.warning(f"⚠️ [{self.label}] Клик капчи не удался: {e}")
                            return False
                        logger.info(f"✅ [{self.label}] Фрукт капча: {btn_text}")
                        await human_delay(2, 3)
                        await self.send_report(f"🤖 Капча решена! '{target_word}' → {btn_text}")
                        return True

            await self.send_report(f"⚠️ Капча: не нашёл '{target_word}' среди {all_btns}")
            return False

        # Фолбэк: слово не в кавычках — ищем любое известное слово прямо в тексте капчи
        target_emoji = None
        matched_kw = None
        for emoji, keywords in FRUIT_MAP.items():
            for kw in keywords:
                if kw in clean_text:
                    target_emoji = emoji
                    matched_kw = kw
                    break
            if target_emoji:
                break

        if target_emoji:
            for row in msg.buttons:
                for btn in row:
                    if target_emoji in btn.text.strip():
                        try:
                            await btn.click()
                        except Exception as e:
                            logger.warning(f"⚠️ [{self.label}] Клик капчи не удался: {e}")
                            return False
                        logger.info(f"✅ [{self.label}] Фрукт капча (фолбэк): {btn.text.strip()}")
                        await human_delay(2, 3)
                        await self.send_report(f"🤖 Капча решена (фолбэк)! '{matched_kw}' → {btn.text.strip()}")
                        return True

        await self.send_report(f"⚠️ Капча неизвестного типа\n{text[:150]}\n{all_btns}")
        return False

    # --- Гейт "подпишись на спонсоров чтобы продолжить фарм" ---
    async def handle_sponsor_gate(self, msg):
        logger.info(f"🚧 [{self.label}] Спонсорский гейт: {(msg.text or '')[:100]}")
        await self.send_report(f"🚧 Гейт спонсоров — подписываюсь на каналы:\n{(msg.text or '')[:150]}")

        confirm_btn = None
        joined = 0
        for row in msg.buttons:
            for btn in row:
                btn_text = btn.text.lower()
                if any(w in btn_text for w in CONFIRM_WORDS):
                    confirm_btn = btn
                    continue
                url = getattr(btn, 'url', None)
                if url:
                    tme_match = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                    if tme_match:
                        target = tme_match.group(1)
                        if not target.lower().endswith('bot'):
                            await self.safe_join(target)
                            joined += 1

        if not confirm_btn:
            await self.send_report("⚠️ Гейт спонсоров: не нашёл кнопку подтверждения, продолжаю без неё")
            return msg

        await human_delay(4, 8)
        since = datetime.datetime.now(datetime.timezone.utc)
        try:
            await confirm_btn.click()
            logger.info(f"✅ [{self.label}] Гейт пройден: подписался на {joined} каналов")
            await self.send_report(f"✅ Гейт спонсоров пройден! Подписался на {joined} каналов")
        except Exception as e:
            logger.error(f"⚠️ [{self.label}] Ошибка подтверждения гейта: {e}")
            await self.send_report(f"⚠️ Гейт спонсоров: ошибка подтверждения: {e}")
            return msg

        return await self.wait_fresh_bot_response(since, timeout=8)

    # --- Сбор ежедневки ---
    async def collect_daily(self) -> int:
        try:
            logger.info(f"📅 [{self.label}] Собираю ежедневку...")
            await self.send_report("📅 Начинаю сбор ежедневки...")

            for attempt in range(3):
                if attempt > 0:
                    logger.info(f"🔄 [{self.label}] Повторная попытка ежедневки #{attempt+1}")
                    await asyncio.sleep(10)

                async with self.bot_lock.acquire():
                    msg = await self._open_bot_unlocked()
                    if not msg or not msg.buttons:
                        self.add_error("Ежедневка: бот не ответил")
                        continue

                    profile_clicked = False
                    for row in msg.buttons:
                        for btn in row:
                            if any(w in btn.text.lower() for w in ["профил", "profile"]):
                                await btn.click()
                                profile_clicked = True
                                await human_delay(3, 5)
                                break
                        if profile_clicked:
                            break

                    if not profile_clicked:
                        self.add_error("Ежедневка: профиль не найден")
                        continue

                    msg = await self.get_bot_response()
                    if not msg or not msg.buttons:
                        self.add_error("Ежедневка: нет ответа после профиля")
                        continue

                    daily_clicked = False
                    for row in msg.buttons:
                        for btn in row:
                            if any(w in btn.text.lower() for w in ["еже", "daily", "бонус"]):
                                await btn.click()
                                daily_clicked = True
                                await human_delay(4, 6)
                                break
                        if daily_clicked:
                            break

                    if not daily_clicked:
                        self.add_error("Ежедневка: кнопка не найдена")
                        continue

                    msg = await self.get_bot_response()
                    if not msg:
                        self.add_error("Ежедневка: нет ответа после нажатия")
                        continue

                    text = msg.text or ""

                    if any(w in text.lower() for w in ["уже", "завтра", "получил", "следующ", "приходи", "через"]):
                        wait_sec = parse_next_daily_seconds(text)
                        h, m = wait_sec // 3600, (wait_sec % 3600) // 60
                        await self.send_report(f"ℹ️ Ежедневка уже забрана\n⏳ Следующая через {h}ч {m}м")
                        return wait_sec

                    if not msg.buttons:
                        self.add_error(f"Ежедневка: нет кнопок: {text[:80]}")
                        continue

                    if "проверка" in text.lower() or any(op in text for op in ["+", "-", "*", "/"]):
                        solved = await self.solve_captcha_if_needed(msg)
                        if solved:
                            resp = await self.get_bot_response()
                            resp_text = (resp.text or "") if resp else ""
                            wait_sec = parse_next_daily_seconds(resp_text) if resp_text else 24 * 3600
                            h, m = wait_sec // 3600, (wait_sec % 3600) // 60
                            self.stats["daily_collected"] = True
                            self.stats["stars_earned"] += 2.0
                            await self.send_report(
                                f"📅 Ежедневка собрана! +2⭐\n"
                                f"📩 {resp_text[:150]}\n"
                                f"⏳ Следующая через {h}ч {m}м"
                            )
                            return wait_sec
                        else:
                            self.add_error("Ежедневка: капча не решена")
                            continue

                    res = solve_math(text)
                    if res is not None:
                        clicked_math = False
                        for row in msg.buttons:
                            for btn in row:
                                if btn.text.strip() == str(res):
                                    await btn.click()
                                    clicked_math = True
                                    await human_delay(2, 3)
                                    break
                            if clicked_math:
                                break
                        if clicked_math:
                            resp = await self.get_bot_response()
                            resp_text = (resp.text or "") if resp else ""
                            wait_sec = parse_next_daily_seconds(resp_text) if resp_text else 24 * 3600
                            h, m = wait_sec // 3600, (wait_sec % 3600) // 60
                            self.stats["daily_collected"] = True
                            self.stats["stars_earned"] += 2.0
                            await self.send_report(
                                f"📅 Ежедневка собрана! +2⭐\n"
                                f"🔢 Капча: {res}\n"
                                f"📩 {resp_text[:150]}\n"
                                f"⏳ Следующая через {h}ч {m}м"
                            )
                            return wait_sec
                        self.add_error(f"Ежедневка: ответ {res} не найден")
                        continue

                    self.add_error(f"Ежедневка: непонятный ответ: {text[:60]}")
                    continue

            await self.send_report("❌ Ежедневка: все 3 попытки провалились")
            return 3600

        except Exception as e:
            logger.error(f"[{self.label}] Ошибка ежедневки: {e}")
            self.add_error(f"Ежедневка exception: {str(e)[:50]}")
            await self.send_report(f"❌ Ошибка ежедневки: {e}")
            return 3600

    async def daily_cycle(self):
        while True:
            wait_sec = await self.collect_daily()
            h, m = wait_sec // 3600, (wait_sec % 3600) // 60
            logger.info(f"⏳ [{self.label}] Следующая ежедневка через {h}ч {m}м")
            await asyncio.sleep(wait_sec)

    # --- Обработка одного задания ---
    async def process_one_task(self, task_msg) -> tuple:
        text = task_msg.text or ""

        if any(phrase in text.lower() for phrase in ALL_DONE_PHRASES):
            return 'all_done', text

        # WebApp
        has_webapp = False
        if task_msg.buttons:
            for row in task_msg.buttons:
                for btn in row:
                    if is_webapp_button(btn):
                        has_webapp = True
                        break
                if has_webapp:
                    break

        if has_webapp:
            startapp_url = None
            if task_msg.buttons:
                for row in task_msg.buttons:
                    for btn in row:
                        url = getattr(btn, 'url', None)
                        if url and 'startapp' in url.lower():
                            startapp_url = url
                            break
                    if startapp_url:
                        break

            if startapp_url:
                startapp_done = await self.handle_startapp(startapp_url)
                if startapp_done:
                    fresh_msg = await self.get_bot_response()
                    if fresh_msg and "проверка" in (fresh_msg.text or "").lower():
                        if await self.solve_captcha_if_needed(fresh_msg):
                            await asyncio.sleep(3)
                            fresh_msg = await self.get_bot_response()
                    confirmed = False
                    if fresh_msg and fresh_msg.buttons:
                        for row in fresh_msg.buttons:
                            for btn in row:
                                if any(w in btn.text.lower() for w in CONFIRM_WORDS):
                                    try:
                                        await btn.click()
                                        confirmed = True
                                    except Exception as e:
                                        logger.warning(f"⚠️ [{self.label}] Клик подтверждения startapp: {e}")
                                    await human_delay(2, 3)
                                    break
                            if confirmed:
                                break
                    self.stats["stars_earned"] += 0.4
                    confirm_note = " и подтверждено" if confirmed else ""
                    await self.send_report(f"🚀 Приложение запущено{confirm_note}!\n📄 {text[:80]}")
                    return 'done', f'startapp ({startapp_url[:50]})'

            skipped = False
            if task_msg.buttons:
                for row in task_msg.buttons:
                    for btn in row:
                        if "пропустить" in btn.text.lower():
                            try:
                                await btn.click()
                                skipped = True
                            except Exception as e:
                                logger.warning(f"⚠️ [{self.label}] Клик Пропустить: {e}")
                            await human_delay(2, 3)
                            break
                    if skipped:
                        break

            if skipped:
                return 'skip', 'мини-апп (пропущен)'
            else:
                await self.send_report(f"⚠️ Мини-апп без Пропустить\n📄 {text[:150]}\n👆 Выполни вручную")
                return 'all_done', text

        action_done = False
        action_parts = []
        same_msg_confirm_btn = None

        if task_msg.buttons:
            for row in task_msg.buttons:
                for btn in row:
                    url = getattr(btn, 'url', None)
                    btn_text = btn.text.lower()

                    if any(w in btn_text for w in CONFIRM_WORDS):
                        same_msg_confirm_btn = btn
                        continue

                    if any(w in btn_text for w in ["пропустить", "меню", "назад", "главное"]):
                        continue

                    if url:
                        tme_match = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                        try:
                            if tme_match:
                                target = tme_match.group(1)
                                if target.lower().endswith('bot'):
                                    await self.client.send_message(target, '/start')
                                    await self.archive_chat(target)
                                    action_parts.append(f"запустил бота @{target}")
                                else:
                                    await self.safe_join(target)
                                    action_parts.append(f"подписался на {target}")
                            else:
                                logger.info(f"🔗 [{self.label}] Внешняя ссылка в задании: {url}")
                                action_parts.append("перешёл по ссылке")
                            action_done = True
                        except Exception as e:
                            logger.warning(f"⚠️ [{self.label}] Ошибка действия по кнопке: {e}")

        if not action_done:
            for target in re.findall(r't\.me/(\+?[a-zA-Z0-9_]+)', text):
                if target.lower().endswith('bot'):
                    await self.client.send_message(target, '/start')
                    await self.archive_chat(target)
                    action_parts.append(f"запустил бота @{target}")
                else:
                    await self.safe_join(target)
                    action_parts.append(f"подписался на {target}")
                action_done = True

            for username in re.findall(r'@([a-zA-Z0-9_]{5,})', text):
                if username.lower().endswith('bot'):
                    await self.client.send_message(username, '/start')
                    await self.archive_chat(username)
                    action_parts.append(f"запустил бота @{username}")
                else:
                    await self.safe_join(username)
                    action_parts.append(f"подписался на @{username}")
                action_done = True

        if not action_done and same_msg_confirm_btn:
            action_done = True
            action_parts.append("ознакомился с заданием")

        action_desc = ", ".join(action_parts) if action_parts else "выполнил задание"

        if not action_done:
            logger.warning(f"⚠️ [{self.label}] Тип не определён: {text[:150]}")
            await self.send_report(f"⚠️ Не смог определить задание\n📄 {text[:150]}")
            if task_msg.buttons:
                for row in task_msg.buttons:
                    for btn in row:
                        if "пропустить" in btn.text.lower():
                            try:
                                await btn.click()
                            except Exception:
                                pass
                            await human_delay(2, 3)
                            break
            return 'skip', text[:100]

        await human_delay(6, 12)

        confirmed = False

        if same_msg_confirm_btn:
            try:
                await same_msg_confirm_btn.click()
                confirmed = True
                await human_delay(3, 5)
            except Exception as e:
                logger.warning(f"⚠️ [{self.label}] Не удалось нажать подтверждение в том же сообщении: {e}")

        fresh_msg = await self.get_bot_response()
        if fresh_msg and "проверка" in (fresh_msg.text or "").lower():
            if await self.solve_captcha_if_needed(fresh_msg):
                await asyncio.sleep(3)
                fresh_msg = await self.get_bot_response()

        if not confirmed and fresh_msg and fresh_msg.buttons:
            for row in fresh_msg.buttons:
                for btn in row:
                    if any(w in btn.text.lower() for w in CONFIRM_WORDS):
                        try:
                            await btn.click()
                            confirmed = True
                        except Exception as e:
                            logger.warning(f"⚠️ [{self.label}] Не удалось нажать подтверждение: {e}")
                        await human_delay(3, 5)
                        break
                if confirmed:
                    break

        if confirmed:
            self.stats["stars_earned"] += 0.4

        suffix = "" if confirmed else " (без подтверждения)"
        return 'done', action_desc + suffix

    # --- Цикл заданий ---
    async def tasks_cycle(self):
        while True:
            try:
                logger.info(f"📋 [{self.label}] Запуск цикла заданий...")
                await self.send_report("📋 Начинаю выполнение заданий...")

                async with self.bot_lock.acquire():
                    msg = await self._open_bot_unlocked()
                    if not msg or not msg.buttons:
                        await self.send_report("⚠️ Бот не ответил на /start")
                        await asyncio.sleep(10800)
                        continue

                    task_opened = False
                    for row in msg.buttons:
                        for btn in row:
                            if "задани" in btn.text.lower():
                                await btn.click()
                                task_opened = True
                                break
                        if task_opened:
                            break

                    if not task_opened:
                        await self.send_report("⚠️ Кнопка заданий не найдена")
                        await asyncio.sleep(10800)
                        continue

                completed = 0
                skipped = 0
                reports = []
                last_task_msg_id = None
                cycle_broken = False

                for i in range(30):
                    try:
                        async with self.bot_lock.acquire():
                            task_msg = await self.get_bot_response()
                            if not task_msg:
                                await self.send_report("⚠️ Не получил сообщение от бота")
                                cycle_broken = True
                                break

                            if last_task_msg_id and task_msg.id == last_task_msg_id:
                                skipped_by_btn = False
                                if task_msg.buttons:
                                    for row in task_msg.buttons:
                                        for btn in row:
                                            if "пропустить" in btn.text.lower():
                                                await btn.click()
                                                skipped_by_btn = True
                                                await human_delay(3, 5)
                                                break
                                        if skipped_by_btn:
                                            break
                                if skipped_by_btn:
                                    skipped += 1
                                    continue
                                else:
                                    await self.send_report("⚠️ Задание зациклилось без Пропустить")
                                    cycle_broken = True
                                    break

                            last_task_msg_id = task_msg.id
                            result, desc = await self.process_one_task(task_msg)
                    except Exception as e:
                        # Не роняем весь цикл заданий из-за одной сбойной кнопки/сообщения
                        # (раньше такая ошибка стоила 30 минут простоя farm-а).
                        logger.error(f"[{self.label}] Ошибка на задании #{i+1}: {e}")
                        self.add_error(f"tasks#{i+1}: {str(e)[:40]}")
                        await human_delay(3, 6)
                        continue

                    if result == 'all_done':
                        interval = random_task_interval()
                        h, m = interval // 3600, (interval % 3600) // 60
                        self.stats["tasks_completed"] += completed
                        self.stats["tasks_skipped"] += skipped
                        report = (
                            f"🎉 Все задания выполнены!\n"
                            f"✅ Выполнено: {completed}\n"
                            f"⏭️ Пропущено: {skipped}\n"
                        )
                        if reports:
                            report += "\n📋 Что сделал:\n" + "\n".join(reports)
                        report += f"\n\n⏳ Следующая проверка через {h}ч {m}м"
                        await self.send_report(report)
                        await asyncio.sleep(interval)
                        cycle_broken = True
                        break

                    elif result == 'done':
                        completed += 1
                        reports.append(f"✅ {completed}. {desc}")
                        await self.send_report(f"✅ Задание #{completed} выполнено!\n📌 {desc}")
                        await human_delay(4, 9)

                    elif result == 'skip':
                        skipped += 1
                        reports.append(f"⏭️ {desc[:50]}")
                        await human_delay(2, 4)

                if not cycle_broken:
                    interval = random_task_interval()
                    h, m = interval // 3600, (interval % 3600) // 60
                    self.stats["tasks_completed"] += completed
                    await self.send_report(
                        f"⚠️ Лимит заданий (30)\n"
                        f"✅ {completed} | ⏭️ {skipped}\n"
                        f"⏳ Через {h}ч {m}м"
                    )
                    await asyncio.sleep(interval)

            except Exception as e:
                logger.error(f"[{self.label}] Ошибка заданий: {e}")
                self.add_error(f"tasks: {str(e)[:50]}")
                await self.send_report(f"❌ Ошибка заданий: {e}")
                await asyncio.sleep(600)  # было 1800 — снижено, чтобы не терять полчаса на разовый глюк

    # --- Промокоды ---
    async def _activate_promo_attempt(self, code: str) -> bool:
        """Одна попытка активации промокода. Возвращает True при подтверждённом успехе.
        Использует high_priority=True на локе, чтобы не стоять в очереди за
        циклом заданий/ежедневки/кликера."""
        async with self.bot_lock.acquire(high_priority=True):
            logger.info(f"🔔 [{self.label}] Активирую промокод: {code}")

            msg = await self._open_bot_unlocked()
            if not msg or not msg.buttons:
                await self.send_report("⚠️ Промокод: бот не ответил")
                return False

            profile_clicked = False
            since = datetime.datetime.now(datetime.timezone.utc)
            for row in msg.buttons:
                for btn in row:
                    if any(w in btn.text.lower() for w in ["профил", "profile"]):
                        await btn.click()
                        profile_clicked = True
                        break
                if profile_clicked:
                    break

            if not profile_clicked:
                await self.send_report("⚠️ Промокод: кнопка Профиль не найдена")
                return False

            # Событийное ожидание вместо human_delay(1,2)+опрос — экономим секунды
            # на каждом шаге, это и есть основная причина медленной активации.
            msg = await self.wait_fresh_bot_response(since, timeout=8)
            if not msg or not msg.buttons:
                await self.send_report("⚠️ Промокод: нет ответа после Профиля")
                return False

            promo_clicked = False
            since = datetime.datetime.now(datetime.timezone.utc)
            for row in msg.buttons:
                for btn in row:
                    if "промо" in btn.text.lower():
                        await btn.click()
                        promo_clicked = True
                        break
                if promo_clicked:
                    break

            if not promo_clicked:
                await self.send_report("⚠️ Не нашёл кнопку промокода")
                return False

            msg2 = await self.wait_fresh_bot_response(since, timeout=8)
            if not msg2:
                await self.send_report("⚠️ Промокод: нет ответа после кнопки Промокод")
                return False

            since = datetime.datetime.now(datetime.timezone.utc)
            await self.client.send_message(TARGET_BOT, code)

            resp = await self.wait_fresh_bot_response(since, timeout=8)
            if not resp:
                await self.send_report(f"⚠️ Промокод `{code}`: нет ответа")
                return False

            resp_text = resp.text or ""

            if any(w in resp_text.lower() for w in ["неверн", "не найден", "истёк", "истек", "уже использован", "invalid", "expired"]):
                await self.send_report(f"❌ Промокод `{code}` не подошёл:\n📩 {resp_text[:150]}")
                self.add_error(f"промо {code}: отклонён ботом")
                return True  # не ошибка соединения — повторять бессмысленно

            if resp.buttons:
                for r in resp.buttons:
                    for b in r:
                        url = getattr(b, 'url', None)
                        b_text = b.text.lower()
                        if url:
                            tme = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                            if tme and not tme.group(1).lower().endswith('bot'):
                                await self.safe_join(tme.group(1))
                        if any(w in b_text for w in ["подписал", "проверить", "готово", "я подписал", "check"]):
                            await human_delay(2, 3)
                            await b.click()

            self.stats["promos_activated"].append(code)
            self.stats["stars_earned"] += 2.0
            await self.send_report(f"✅ Промокод `{code}` активирован! +2⭐\n📩 {resp_text[:100]}")
            return True

    async def activate_promo(self, code: str):
        if code.lower() in self.activated_promos:
            return
        if code.lower() in self.promo_in_progress:
            return

        self.promo_in_progress.add(code.lower())
        await self.send_report(f"🚀 Активирую промокод: `{code}`")
        try:
            success = await self._activate_promo_attempt(code)
            if not success:
                logger.warning(f"⚠️ [{self.label}] Промокод {code}: попытка #1 неудачна, повторяю сразу")
                success = await self._activate_promo_attempt(code)

            if success:
                self.activated_promos.add(code.lower())
                if len(self.activated_promos) > 50:
                    self.activated_promos = set(list(self.activated_promos)[-20:])
            else:
                await self.send_report(f"❌ Промокод `{code}`: обе попытки неудачны, попробую снова если увижу его ещё раз")
        finally:
            self.promo_in_progress.discard(code.lower())

    # --- Ловля чека (кнопка "Получить") ---
    async def try_claim_check(self, event):
        """Моментально нажимает кнопку Получить при появлении чека в канале.
        Не использует bot_lock — это отдельный чат (канал), не переписка с TARGET_BOT,
        поэтому не конкурирует с другими циклами и остаётся мгновенным."""
        msg = event.message
        if not msg.buttons:
            return

        text = (msg.text or "").lower()
        if not any(w in text for w in ["чек", "check", "получить", "claim", "⭐", "звезд"]):
            return

        msg_id = msg.id
        if msg_id in self.claimed_checks:
            return

        for row in msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in ["получить", "claim", "забрать", "взять", "активировать", "открыть", "🎁"]):
                    logger.info(f"🎁 [{self.label}] ЧЕК НАЙДЕН! Нажимаю '{btn.text}' МОМЕНТАЛЬНО!")
                    self.claimed_checks.add(msg_id)
                    try:
                        await btn.click()
                        await self.send_report(
                            f"🎁 ЧЕК ПОЙМАН И НАЖАТ!\n"
                            f"📝 {msg.text[:100] if msg.text else 'без текста'}"
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ [{self.label}] Первая попытка клика по чеку не удалась: {e}")
                        try:
                            await btn.click()
                            await self.send_report(
                                f"🎁 ЧЕК ПОЙМАН СО ВТОРОЙ ПОПЫТКИ!\n"
                                f"📝 {msg.text[:100] if msg.text else 'без текста'}"
                            )
                        except Exception as e2:
                            logger.error(f"⚠️ [{self.label}] Ошибка клика по чеку: {e2}")
                            await self.send_report(f"⚠️ Чек найден но не нажат: {e2}")
                    return

    # --- Кликер ---
    async def clicker_cycle(self):
        CLICKER_START_HOUR = 8
        CLICKER_END_HOUR = 24

        while True:
            try:
                now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)

                if now.hour < CLICKER_START_HOUR:
                    wake_at = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
                    wait_sec = (wake_at - now).total_seconds()
                    await asyncio.sleep(wait_sec)
                    continue

                # Каждый день заново рандомим количество кликов в диапазоне 20-25.
                times_today = random.randint(CLICKER_MIN_PER_DAY, CLICKER_MAX_PER_DAY)
                self.stats["clicker_target"] = times_today

                window_sec = (CLICKER_END_HOUR - CLICKER_START_HOUR) * 3600
                part_sec = window_sec // times_today
                day_start = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)

                click_times = []
                for i in range(times_today):
                    part_start = day_start + datetime.timedelta(seconds=i * part_sec)
                    rand_sec = random.randint(0, int(part_sec * 0.8))
                    click_time = part_start + datetime.timedelta(seconds=rand_sec + random.randint(0, 300))
                    midnight = day_start + datetime.timedelta(hours=16)
                    if click_time > midnight:
                        click_time = midnight - datetime.timedelta(seconds=random.randint(60, 600))
                    click_times.append(click_time)

                click_times.sort()

                for i, click_time in enumerate(click_times):
                    now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
                    if click_time <= now:
                        continue

                    wait_sec = (click_time - now).total_seconds()
                    await asyncio.sleep(wait_sec)

                    try:
                        async with self.bot_lock.acquire():
                            await self.client.send_message(TARGET_BOT, '/start')
                            await human_delay(3, 5)
                            msg = await self.get_bot_response()
                            if not msg or not msg.buttons:
                                continue

                            if "проверка" in (msg.text or "").lower():
                                solved = await self.solve_captcha_if_needed(msg)
                                if solved:
                                    await asyncio.sleep(3)
                                    msg = await self.get_bot_response()
                                else:
                                    continue

                            clicker_clicked = False
                            if msg and msg.buttons:
                                for row in msg.buttons:
                                    for btn in row:
                                        if any(w in btn.text.lower() for w in ["кликер", "clicker", "farm", "✨"]):
                                            await btn.click()
                                            clicker_clicked = True
                                            await human_delay(2, 4)
                                            break
                                    if clicker_clicked:
                                        break

                            if clicker_clicked:
                                resp = await self.get_bot_response()
                                if resp and "проверка" in (resp.text or "").lower():
                                    solved = await self.solve_captcha_if_needed(resp)
                                    if solved:
                                        await asyncio.sleep(4)
                                        resp = await self.get_bot_response()
                                self.stats["clicker_clicks"] += 1
                                self.stats["stars_earned"] += 0.1
                                logger.info(f"🎮 [{self.label}] Клик #{i+1}/{times_today} собран!")

                    except Exception as e:
                        logger.error(f"[{self.label}] Ошибка клика #{i+1}: {e}")
                        self.add_error(f"кликер#{i+1}: {str(e)[:40]}")

                now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
                tomorrow_8 = (now + datetime.timedelta(days=1)).replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
                wait_sec = (tomorrow_8 - now).total_seconds()
                await asyncio.sleep(wait_sec)

            except Exception as e:
                logger.error(f"[{self.label}] Ошибка кликера: {e}")
                await asyncio.sleep(3600)

    # --- Статистика в 20:00 ---
    async def stats_cycle(self):
        while True:
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
            target = now.replace(hour=20, minute=0, second=0, microsecond=0)
            if now >= target:
                target += datetime.timedelta(days=1)
            wait_sec = (target - now).total_seconds()
            await asyncio.sleep(wait_sec)

            s = self.stats
            promos_str = ", ".join(s["promos_activated"]) if s["promos_activated"] else "нет"
            errors_str = "\n".join(s["errors"][-5:]) if s["errors"] else "нет"
            clicker_target = s["clicker_target"] or f"{CLICKER_MIN_PER_DAY}-{CLICKER_MAX_PER_DAY}"

            report = (
                f"📊 СТАТИСТИКА ЗА ДЕНЬ\n"
                f"{'━' * 25}\n"
                f"📅 Ежедневка: {'✅ собрана' if s['daily_collected'] else '❌ не собрана'}\n"
                f"📋 Задания: ✅{s['tasks_completed']} | ⏭️{s['tasks_skipped']}\n"
                f"🎮 Кликер: {s['clicker_clicks']}/{clicker_target} кликов\n"
                f"🔔 Промокоды: {promos_str}\n"
                f"⭐ Примерно заработано: ~{s['stars_earned']:.1f}⭐\n"
                f"{'━' * 25}\n"
                f"❌ Ошибки:\n{errors_str}"
            )
            await self.send_report(report)
            self.reset_stats()

    # --- Обработчик личных сообщений от TARGET_BOT (для мгновенного ожидания ответа) ---
    def register_bot_dm_handler(self):
        @self.client.on(events.NewMessage(chats=TARGET_BOT))
        @self.client.on(events.MessageEdited(chats=TARGET_BOT))
        async def handle_bot_dm(event):
            try:
                self.bot_msg_queue.put_nowait(event.message)
            except asyncio.QueueFull:
                try:
                    self.bot_msg_queue.get_nowait()  # выкидываем самое старое
                except asyncio.QueueEmpty:
                    pass
                try:
                    self.bot_msg_queue.put_nowait(event.message)
                except asyncio.QueueFull:
                    pass

        self._bot_dm_handler = handle_bot_dm

    # --- Обработчик сообщений канала (промокоды/чеки) ---
    def register_channel_handler(self):
        @self.client.on(events.NewMessage(chats=CHANNEL_SOURCE))
        @self.client.on(events.MessageEdited(chats=CHANNEL_SOURCE))
        async def handle_channel(event):
            raw = event.raw_text or ""
            logger.info(f"📨 [{self.label}] Канал: {raw[:120]}")

            # 1. МОМЕНТАЛЬНАЯ ловля чека
            asyncio.create_task(self.try_claim_check(event))

            # 2. Промокод (high-priority — обрабатывается вне очереди циклов)
            code = find_promo_in_text(raw)
            if code:
                logger.info(f"🔔 [{self.label}] Промокод найден: {code}")
                asyncio.create_task(self.activate_promo(code))
            elif any(w in raw.lower() for w in PROMO_KEYWORDS):
                await self.send_report(f"🔔 Пост с промо но код не найден:\n{raw[:200]}")

        # Храним ссылку, чтобы обработчик не собрал сборщик мусора
        self._channel_handler = handle_channel

    async def _delayed_tasks_start(self):
        await asyncio.sleep(30)
        await self.tasks_cycle()

    def start_cycles(self) -> list:
        return [
            asyncio.create_task(self.daily_cycle()),
            asyncio.create_task(self._delayed_tasks_start()),
            asyncio.create_task(self.clicker_cycle()),
            asyncio.create_task(self.stats_cycle()),
        ]


# --- Self-ping (анти-засыпание на бесплатном тарифе Render) ---
async def keep_alive_cycle():
    external_url = os.getenv("RENDER_EXTERNAL_URL")
    if not external_url:
        logger.info("ℹ️ RENDER_EXTERNAL_URL не задан — self-ping пропущен (не Render/локальный запуск)")
        return

    ping_url = external_url.rstrip('/') + '/health'
    logger.info(f"💓 Self-ping включён: {ping_url} каждые ~4 мин")

    while True:
        await asyncio.sleep(240 + random.randint(-15, 15))
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(ping_url, timeout=15).read())
            logger.info("💓 Self-ping OK")
        except Exception as e:
            logger.warning(f"⚠️ Self-ping ошибка: {e}")


accounts: list = []
_background_tasks: list = []


# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global accounts, _background_tasks

    if not ACCOUNT_SESSIONS:
        logger.warning("⚠️ TELEGRAM_SESSION не задан")
        yield
        return

    accounts = [PatrickAccount(label, session) for label, session in ACCOUNT_SESSIONS]

    for acc in accounts:
        await acc.client.start()
        acc.register_channel_handler()
        acc.register_bot_dm_handler()
        logger.info(f"✅ [{acc.label}] Telegram client запущен")
        await acc.send_report(
            "🤖 Бот запущен!\n"
            "📅 Ежедневка: по времени бота\n"
            "📋 Задания: каждые ~3 часа\n"
            f"🔔 Промокоды: мониторинг 24/7, реакция мгновенная (приоритетный лок)\n"
            "🎁 Чеки: моментальный перехват\n"
            f"🎮 Кликер: {CLICKER_MIN_PER_DAY}-{CLICKER_MAX_PER_DAY} раз в день (рандом, 8:00-00:00)\n"
            "📊 Статистика: каждый день в 20:00\n"
            "💓 Анти-сон: self-ping каждые ~4 мин"
        )
        _background_tasks.extend(acc.start_cycles())

    _background_tasks.append(asyncio.create_task(keep_alive_cycle()))

    yield

    for t in _background_tasks:
        t.cancel()
    for acc in accounts:
        await acc.client.disconnect()


app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok", "accounts": len(accounts)}
