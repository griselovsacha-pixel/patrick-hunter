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

ALL_DONE_PHRASES = [
    "выполнил все задания", "все задания выполнены",
    "приходи через", "новых заданий нет",
    "заданий больше нет", "респект", "молодец",
]

# Слова-маркеры кнопки подтверждения выполнения задания/подписки.
# Расширенный список — раньше сюда не попадали "выполнил", "готово", "изучил" и т.п.,
# из-за чего бот не нажимал финальную кнопку и терял награду.
CONFIRM_WORDS = [
    "подтвер", "выдать", "проверить", "claim", "check", "выполнил",
    "готов", "получить", "забрать", "collect", "confirm", "done",
    "изучил", "ознакомил", "принять", "я подписал", "продолжить",
]

# Кнопки, которые точно НЕ являются действием (подписка/запуск), даже если у них есть url
NON_ACTION_BUTTON_WORDS = ["пропустить", "меню", "назад", "главное"] + CONFIRM_WORDS

# Маркеры сообщения-"гейта": бот просит подписаться на N спонсоров, чтобы продолжить фарм
SPONSOR_GATE_HINTS = [
    "для продолжения", "выполни всего", "выполни задани", "чтобы продолжить",
]

IGNORED_WORDS = {
    "html", "http", "https", "main", "patrickstarsrobot", "patrickstarsfarm",
    "profile", "claim", "check", "start", "webapp", "join", "bot", "stars",
    "tgwebapp", "startapp", "go", "app", "link", "channel", "group", "send",
    "ловите", "вводим", "боте", "профиль", "промокод", "дейли", "друзья",
    "патрик", "всем", "привет", "получить", "звезд", "депозит", "игры",
    "первым", "днем", "лета", "поздравляет",
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

SESSION_STRING = os.getenv("TELEGRAM_SESSION")
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) if SESSION_STRING else None

# Лок на переписку с TARGET_BOT: не даёт разным циклам (задания/ежедневка/кликер/промо)
# одновременно слать и читать сообщения бота и путать ответы друг друга.
# Захватывается короткими участками, чтобы промокод/проверка не ждали долго.
BOT_LOCK = asyncio.Lock()

# --- Глобальная статистика за день ---
_stats = {
    "daily_collected": False,
    "daily_errors": 0,
    "tasks_completed": 0,
    "tasks_skipped": 0,
    "promos_activated": [],
    "clicker_clicks": 0,
    "errors": [],
    "stars_earned": 0.0,
}

def reset_stats():
    global _stats
    _stats = {
        "daily_collected": False,
        "daily_errors": 0,
        "tasks_completed": 0,
        "tasks_skipped": 0,
        "promos_activated": [],
        "clicker_clicks": 0,
        "errors": [],
        "stars_earned": 0.0,
    }

def add_error(msg: str):
    _stats["errors"].append(f"{datetime.datetime.now().strftime('%H:%M')} {msg[:60]}")
    if len(_stats["errors"]) > 20:
        _stats["errors"] = _stats["errors"][-20:]


# --- Утилиты ---
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


async def send_report(text: str):
    try:
        await client.send_message(TARGET_USER, text)
        logger.info(f"📤 Отчёт: {text[:80]}")
    except Exception as e:
        logger.error(f"Ошибка отправки отчёта: {e}")


def random_task_interval() -> int:
    base = random.randint(2 * 3600 + 15 * 60, 3 * 3600 + 45 * 60)
    return base + random.randint(0, 300)


# --- Архивировать чат (папка 1 = архив) ---
async def archive_chat(entity):
    try:
        peer = await client.get_input_entity(entity)
        await client(EditPeerFoldersRequest(
            folder_peers=[InputFolderPeer(peer=peer, folder_id=1)]
        ))
        logger.info(f"📁 Архивировал: {entity}")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось архивировать {entity}: {e}")


# --- Ждём свежий ответ от бота ---
async def get_bot_response(max_age: int = 120, attempts: int = 8):
    for i in range(attempts):
        now = datetime.datetime.now(datetime.timezone.utc)
        async for msg in client.iter_messages(TARGET_BOT, limit=5):
            age = (now - msg.date).total_seconds()
            if age < max_age and (msg.text or msg.buttons):
                return msg
        logger.info(f"⏳ Жду ответ бота... попытка {i + 1}/{attempts}")
        await asyncio.sleep(1.5)
    async for msg in client.iter_messages(TARGET_BOT, limit=1):
        return msg
    return None


async def _open_bot_unlocked():
    """Тело открытия бота БЕЗ захвата BOT_LOCK. Вызывать только если лок уже взят снаружи."""
    await client.send_message(TARGET_BOT, '/start')
    await asyncio.sleep(5)
    msg = await get_bot_response()
    if not msg:
        return None

    # Разруливаем цепочку экранов-блокеров: капча и/или гейт "подпишись на спонсоров".
    # Ограничено 4 итерациями на случай если бот шлёт их одно за другим.
    for _ in range(4):
        if not msg:
            return None
        text_lower = (msg.text or "").lower()

        if "проверка" in text_lower:
            solved = await solve_captcha_if_needed(msg)
            if not solved:
                return None
            await asyncio.sleep(4)
            msg = await get_bot_response()
            continue

        if looks_like_sponsor_gate(msg):
            msg = await handle_sponsor_gate(msg)
            continue

        break

    return msg


async def open_bot():
    """Открыть бота как отдельную атомарную операцию (сама берёт лок)."""
    async with BOT_LOCK:
        return await _open_bot_unlocked()


# --- WebApp ---
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


async def handle_startapp(url: str) -> bool:
    try:
        match = re.search(r't\.me/([a-zA-Z0-9_]+)(?:/\w+)?\?startapp=([a-zA-Z0-9_]+)', url)
        if not match:
            return False
        bot_username = match.group(1)
        start_param = match.group(2)
        logger.info(f"🚀 StartApp: @{bot_username} param={start_param}")
        bot = await client.get_entity(bot_username)
        await client(StartBotRequest(bot=bot, peer=bot, start_param=start_param))
        logger.info(f"✅ StartApp запущен: @{bot_username}")
        # Архивируем бота
        await archive_chat(bot_username)
        await human_delay(25, 35)
        return True
    except Exception as e:
        logger.error(f"⚠️ StartApp ошибка: {e}")
        return False


# --- Вступление в канал/группу + архивирование ---
async def safe_join(target: str):
    try:
        if target.startswith('+'):
            await client(ImportChatInviteRequest(target[1:]))
            logger.info(f"✅ Вступил по инвайту: {target}")
        else:
            await client(JoinChannelRequest(target))
            logger.info(f"✅ Подписался: @{target}")
        await human_delay(2, 4)
        # Архивируем канал сразу после подписки
        await archive_chat(target)
        await human_delay(2, 4)
    except UserAlreadyParticipantError:
        logger.info(f"ℹ️ Уже подписан: {target}")
        await archive_chat(target)
    except InviteHashInvalidError:
        logger.warning(f"⚠️ Невалидный инвайт: {target}")
    except Exception as e:
        logger.error(f"Ошибка вступления {target}: {e}")


# --- Капча ---
async def solve_captcha_if_needed(msg) -> bool:
    if not msg:
        return False
    text = msg.text or ""
    if "проверка на робота" not in text.lower():
        return True

    logger.info(f"🤖 Капча: {text[:100]}")
    if not msg.buttons:
        await send_report("⚠️ Капча без кнопок")
        return False

    all_btns = [b.text for row in msg.buttons for b in row]
    logger.info(f"🔘 Кнопки: {all_btns}")

    # Математика
    res = solve_math(text)
    if res is not None:
        for row in msg.buttons:
            for btn in row:
                if btn.text.strip() == str(res):
                    await btn.click()
                    logger.info(f"✅ Матем. капча: {res}")
                    await human_delay(2, 3)
                    await send_report(f"🤖 Капча решена! Ответ: {res}")
                    return True
        await send_report(f"⚠️ Капча: {res} не найден среди {all_btns}")
        return False

    # Фрукты
    clean_text = re.sub(r"\*+", "", text).lower()
    word_match = re.search(r"[«\"'“”‘’](.+?)[»\"'“”‘’]", clean_text)
    if word_match:
        target_word = word_match.group(1).lower().strip()
        logger.info(f"🔍 Ищу фрукт: '{target_word}'")
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
                    await btn.click()
                    logger.info(f"✅ Фрукт капча: {btn_text}")
                    await human_delay(2, 3)
                    await send_report(f"🤖 Капча решена! '{target_word}' → {btn_text}")
                    return True

        await send_report(f"⚠️ Капча: не нашёл '{target_word}' среди {all_btns}")
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
        logger.info(f"🔍 Фолбэк: нашёл '{matched_kw}' в тексте капчи")
        for row in msg.buttons:
            for btn in row:
                if target_emoji in btn.text.strip():
                    await btn.click()
                    logger.info(f"✅ Фрукт капча (фолбэк): {btn.text.strip()}")
                    await human_delay(2, 3)
                    await send_report(f"🤖 Капча решена (фолбэк)! '{matched_kw}' → {btn.text.strip()}")
                    return True

    await send_report(f"⚠️ Капча неизвестного типа\n{text[:150]}\n{all_btns}")
    return False


# --- Гейт "подпишись на спонсоров чтобы продолжить фарм" ---
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


async def handle_sponsor_gate(msg):
    """Подписывается на все каналы-спонсоры в гейт-сообщении и жмёт кнопку подтверждения.
    Возвращает следующее сообщение бота (или исходное, если подтвердить не удалось)."""
    logger.info(f"🚧 Спонсорский гейт обнаружен: {(msg.text or '')[:100]}")
    await send_report(f"🚧 Гейт спонсоров — подписываюсь на каналы:\n{(msg.text or '')[:150]}")

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
                        await safe_join(target)
                        joined += 1

    if not confirm_btn:
        logger.warning("⚠️ Гейт спонсоров: кнопка подтверждения не найдена")
        await send_report("⚠️ Гейт спонсоров: не нашёл кнопку подтверждения, продолжаю без неё")
        return msg

    await human_delay(4, 8)
    try:
        await confirm_btn.click()
        logger.info(f"✅ Гейт спонсоров пройден: подписался на {joined} каналов, нажал '{confirm_btn.text}'")
        await send_report(f"✅ Гейт спонсоров пройден! Подписался на {joined} каналов")
    except Exception as e:
        logger.error(f"⚠️ Ошибка подтверждения гейта: {e}")
        await send_report(f"⚠️ Гейт спонсоров: ошибка подтверждения: {e}")
        return msg

    await human_delay(2, 4)
    return await get_bot_response()



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
    logger.info(f"⏰ Следующая ежедневка через {hours}ч {minutes}м (+5мин)")
    return total


# --- Сбор ежедневки ---
async def collect_daily() -> int:
    global _stats
    try:
        logger.info("📅 Собираю ежедневку...")
        await send_report("📅 Начинаю сбор ежедневки...")

        # Пробуем до 3 раз если что-то пошло не так
        for attempt in range(3):
            if attempt > 0:
                logger.info(f"🔄 Повторная попытка ежедневки #{attempt+1}")
                await asyncio.sleep(10)

            async with BOT_LOCK:
                msg = await _open_bot_unlocked()
                if not msg or not msg.buttons:
                    add_error("Ежедневка: бот не ответил")
                    continue

                # Профиль
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
                    add_error("Ежедневка: профиль не найден")
                    continue

                msg = await get_bot_response()
                if not msg or not msg.buttons:
                    add_error("Ежедневка: нет ответа после профиля")
                    continue

                # Ежедневка
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
                    add_error("Ежедневка: кнопка не найдена")
                    continue

                msg = await get_bot_response()
                if not msg:
                    add_error("Ежедневка: нет ответа после нажатия")
                    continue

                text = msg.text or ""
                logger.info(f"📅 Ответ: {text[:150]}")

                # Уже забрана
                if any(w in text.lower() for w in ["уже", "завтра", "получил", "следующ", "приходи", "через"]):
                    wait_sec = parse_next_daily_seconds(text)
                    h, m = wait_sec // 3600, (wait_sec % 3600) // 60
                    await send_report(f"ℹ️ Ежедневка уже забрана\n⏳ Следующая через {h}ч {m}м")
                    return wait_sec

                if not msg.buttons:
                    add_error(f"Ежедневка: нет кнопок: {text[:80]}")
                    continue

                # Капча
                if "проверка" in text.lower() or any(op in text for op in ["+", "-", "*", "/"]):
                    solved = await solve_captcha_if_needed(msg)
                    if solved:
                        resp = await get_bot_response()
                        resp_text = (resp.text or "") if resp else ""
                        wait_sec = parse_next_daily_seconds(resp_text) if resp_text else 24 * 3600
                        h, m = wait_sec // 3600, (wait_sec % 3600) // 60
                        _stats["daily_collected"] = True
                        _stats["stars_earned"] += 2.0
                        await send_report(
                            f"📅 Ежедневка собрана! +2⭐\n"
                            f"📩 {resp_text[:150]}\n"
                            f"⏳ Следующая через {h}ч {m}м"
                        )
                        return wait_sec
                    else:
                        add_error("Ежедневка: капча не решена")
                        continue

                # Математика
                res = solve_math(text)
                if res is not None:
                    for row in msg.buttons:
                        for btn in row:
                            if btn.text.strip() == str(res):
                                await btn.click()
                                await human_delay(2, 3)
                                resp = await get_bot_response()
                                resp_text = (resp.text or "") if resp else ""
                                wait_sec = parse_next_daily_seconds(resp_text) if resp_text else 24 * 3600
                                h, m = wait_sec // 3600, (wait_sec % 3600) // 60
                                _stats["daily_collected"] = True
                                _stats["stars_earned"] += 2.0
                                await send_report(
                                    f"📅 Ежедневка собрана! +2⭐\n"
                                    f"🔢 Капча: {res}\n"
                                    f"📩 {resp_text[:150]}\n"
                                    f"⏳ Следующая через {h}ч {m}м"
                                )
                                return wait_sec
                    add_error(f"Ежедневка: ответ {res} не найден")
                    continue

                add_error(f"Ежедневка: непонятный ответ: {text[:60]}")
                continue

        await send_report("❌ Ежедневка: все 3 попытки провалились")
        return 3600

    except Exception as e:
        logger.error(f"Ошибка ежедневки: {e}")
        add_error(f"Ежедневка exception: {str(e)[:50]}")
        await send_report(f"❌ Ошибка ежедневки: {e}")
        return 3600


async def daily_cycle():
    while True:
        wait_sec = await collect_daily()
        h, m = wait_sec // 3600, (wait_sec % 3600) // 60
        logger.info(f"⏳ Следующая ежедневка через {h}ч {m}м")
        await asyncio.sleep(wait_sec)


# --- Обработка одного задания ---
async def process_one_task(task_msg) -> tuple[str, str]:
    global _stats
    text = task_msg.text or ""

    if any(phrase in text.lower() for phrase in ALL_DONE_PHRASES):
        return 'all_done', text

    if task_msg.buttons:
        for row in task_msg.buttons:
            for btn in row:
                logger.info(f"🔘 '{btn.text}' | url={getattr(btn,'url',None)} | type={type(btn).__name__}")

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
            startapp_done = await handle_startapp(startapp_url)
            if startapp_done:
                fresh_msg = await get_bot_response()
                if fresh_msg and "проверка" in (fresh_msg.text or "").lower():
                    if await solve_captcha_if_needed(fresh_msg):
                        await asyncio.sleep(3)
                        fresh_msg = await get_bot_response()
                confirmed = False
                if fresh_msg and fresh_msg.buttons:
                    for row in fresh_msg.buttons:
                        for btn in row:
                            if any(w in btn.text.lower() for w in CONFIRM_WORDS):
                                await btn.click()
                                confirmed = True
                                await human_delay(2, 3)
                                break
                        if confirmed:
                            break
                _stats["stars_earned"] += 0.4
                await send_report(f"🚀 Приложение запущено{'и подтверждено' if confirmed else ''}!\n📄 {text[:80]}")
                return 'done', f'startapp ({startapp_url[:50]})'

        # Пропустить
        skipped = False
        if task_msg.buttons:
            for row in task_msg.buttons:
                for btn in row:
                    if "пропустить" in btn.text.lower():
                        await btn.click()
                        skipped = True
                        await human_delay(2, 3)
                        break
                if skipped:
                    break

        if skipped:
            return 'skip', 'мини-апп (пропущен)'
        else:
            await send_report(f"⚠️ Мини-апп без Пропустить\n📄 {text[:150]}\n👆 Выполни вручную")
            return 'all_done', text

    action_done = False
    action_desc = ""
    action_parts = []
    same_msg_confirm_btn = None

    if task_msg.buttons:
        for row in task_msg.buttons:
            for btn in row:
                url = getattr(btn, 'url', None)
                btn_text = btn.text.lower()

                # Кнопка подтверждения — запоминаем её, но не трогаем сейчас
                # (частый паттерн: подписки + одна кнопка "Я выполнил(а)" в ТОМ ЖЕ сообщении)
                if any(w in btn_text for w in CONFIRM_WORDS):
                    same_msg_confirm_btn = btn
                    continue

                if any(w in btn_text for w in ["пропустить", "меню", "назад", "главное"]):
                    continue

                if url:
                    tme_match = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                    if tme_match:
                        target = tme_match.group(1)
                        if target.lower().endswith('bot'):
                            await client.send_message(target, '/start')
                            await archive_chat(target)
                            action_parts.append(f"запустил бота @{target}")
                        else:
                            await safe_join(target)
                            action_parts.append(f"подписался на {target}")
                    else:
                        # Внешняя (не t.me) ссылка — "переход по ссылке" задание.
                        # Telethon не открывает браузер, но большинству таких ботов
                        # для зачёта важен только последующий клик по кнопке подтверждения.
                        logger.info(f"🔗 Внешняя ссылка в задании: {url}")
                        action_parts.append("перешёл по ссылке")
                    action_done = True

    if not action_done:
        for target in re.findall(r't\.me/(\+?[a-zA-Z0-9_]+)', text):
            if target.lower().endswith('bot'):
                await client.send_message(target, '/start')
                await archive_chat(target)
                action_parts.append(f"запустил бота @{target}")
            else:
                await safe_join(target)
                action_parts.append(f"подписался на {target}")
            action_done = True

        for username in re.findall(r'@([a-zA-Z0-9_]{5,})', text):
            if username.lower().endswith('bot'):
                await client.send_message(username, '/start')
                await archive_chat(username)
                action_parts.append(f"запустил бота @{username}")
            else:
                await safe_join(username)
                action_parts.append(f"подписался на @{username}")
            action_done = True

    # Задание без ссылок, но с явной кнопкой подтверждения ("изучи список", "прочти" и т.п.) —
    # тоже считаем действием: жать будем ту же кнопку ниже.
    if not action_done and same_msg_confirm_btn:
        action_done = True
        action_parts.append("ознакомился с заданием")

    action_desc = ", ".join(action_parts) if action_parts else "выполнил задание"

    if not action_done:
        logger.warning(f"⚠️ Тип не определён: {text[:150]}")
        await send_report(f"⚠️ Не смог определить задание\n📄 {text[:150]}")
        if task_msg.buttons:
            for row in task_msg.buttons:
                for btn in row:
                    if "пропустить" in btn.text.lower():
                        await btn.click()
                        await human_delay(2, 3)
                        break
        return 'skip', text[:100]

    await human_delay(6, 12)

    confirmed = False

    # Сначала пробуем нажать кнопку подтверждения, если она была в ЭТОМ ЖЕ сообщении
    if same_msg_confirm_btn:
        try:
            await same_msg_confirm_btn.click()
            confirmed = True
            await human_delay(3, 5)
        except Exception as e:
            logger.warning(f"⚠️ Не удалось нажать кнопку подтверждения в том же сообщении: {e}")

    # Иначе (или дополнительно) смотрим, не прислал ли бот новое сообщение с подтверждением
    fresh_msg = await get_bot_response()
    if fresh_msg and "проверка" in (fresh_msg.text or "").lower():
        if await solve_captcha_if_needed(fresh_msg):
            await asyncio.sleep(3)
            fresh_msg = await get_bot_response()

    if not confirmed and fresh_msg and fresh_msg.buttons:
        for row in fresh_msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in CONFIRM_WORDS):
                    await btn.click()
                    confirmed = True
                    await human_delay(3, 5)
                    break
            if confirmed:
                break

    if confirmed:
        _stats["stars_earned"] += 0.4

    suffix = "" if confirmed else " (без подтверждения)"
    return 'done', action_desc + suffix


# --- Цикл заданий ---
async def tasks_cycle():
    global _stats
    while True:
        try:
            logger.info("📋 Запуск цикла заданий...")
            await send_report("📋 Начинаю выполнение заданий...")

            async with BOT_LOCK:
                msg = await _open_bot_unlocked()
                if not msg or not msg.buttons:
                    await send_report("⚠️ Бот не ответил на /start")
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
                    await send_report("⚠️ Кнопка заданий не найдена")
                    await asyncio.sleep(10800)
                    continue

            completed = 0
            skipped = 0
            reports = []
            last_task_msg_id = None

            for i in range(30):
                async with BOT_LOCK:
                    task_msg = await get_bot_response()
                    if not task_msg:
                        await send_report("⚠️ Не получил сообщение от бота")
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
                            await send_report("⚠️ Задание зациклилось без Пропустить")
                            break

                    last_task_msg_id = task_msg.id
                    result, desc = await process_one_task(task_msg)

                if result == 'all_done':
                    interval = random_task_interval()
                    h, m = interval // 3600, (interval % 3600) // 60
                    _stats["tasks_completed"] += completed
                    _stats["tasks_skipped"] += skipped
                    report = (
                        f"🎉 Все задания выполнены!\n"
                        f"✅ Выполнено: {completed}\n"
                        f"⏭️ Пропущено: {skipped}\n"
                    )
                    if reports:
                        report += "\n📋 Что сделал:\n" + "\n".join(reports)
                    report += f"\n\n⏳ Следующая проверка через {h}ч {m}м"
                    await send_report(report)
                    await asyncio.sleep(interval)
                    break

                elif result == 'done':
                    completed += 1
                    reports.append(f"✅ {completed}. {desc}")
                    await send_report(f"✅ Задание #{completed} выполнено!\n📌 {desc}")
                    await human_delay(4, 9)

                elif result == 'skip':
                    skipped += 1
                    reports.append(f"⏭️ {desc[:50]}")
                    await human_delay(2, 4)

            else:
                interval = random_task_interval()
                h, m = interval // 3600, (interval % 3600) // 60
                _stats["tasks_completed"] += completed
                await send_report(
                    f"⚠️ Лимит заданий (30)\n"
                    f"✅ {completed} | ⏭️ {skipped}\n"
                    f"⏳ Через {h}ч {m}м"
                )
                await asyncio.sleep(interval)

        except Exception as e:
            logger.error(f"Ошибка заданий: {e}")
            add_error(f"tasks: {str(e)[:50]}")
            await send_report(f"❌ Ошибка заданий: {e}")
            await asyncio.sleep(1800)


# --- Промокоды ---
_activated_promos = set()


_promo_in_progress = set()


async def _activate_promo_attempt(code: str) -> bool:
    """Одна попытка активации промокода. Возвращает True при подтверждённом успехе."""
    async with BOT_LOCK:
        logger.info(f"🔔 Активирую промокод: {code}")

        msg = await _open_bot_unlocked()
        if not msg or not msg.buttons:
            await send_report("⚠️ Промокод: бот не ответил")
            return False

        profile_clicked = False
        for row in msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in ["профил", "profile"]):
                    await btn.click()
                    profile_clicked = True
                    await human_delay(2, 3)
                    break
            if profile_clicked:
                break

        if not profile_clicked:
            await send_report("⚠️ Промокод: кнопка Профиль не найдена")
            return False

        msg = await get_bot_response()
        if not msg or not msg.buttons:
            await send_report("⚠️ Промокод: нет ответа после Профиля")
            return False

        promo_clicked = False
        for row in msg.buttons:
            for btn in row:
                if "промо" in btn.text.lower():
                    await btn.click()
                    promo_clicked = True
                    await human_delay(2, 3)
                    break
            if promo_clicked:
                break

        if not promo_clicked:
            await send_report("⚠️ Не нашёл кнопку промокода")
            return False

        await client.send_message(TARGET_BOT, code)
        await human_delay(3, 5)

        resp = await get_bot_response()
        if not resp:
            await send_report(f"⚠️ Промокод `{code}`: нет ответа")
            return False

        resp_text = resp.text or ""

        # Если бот явно говорит что код неверный/уже использован — retry не поможет
        if any(w in resp_text.lower() for w in ["неверн", "не найден", "истёк", "истек", "уже использован", "invalid", "expired"]):
            await send_report(f"❌ Промокод `{code}` не подошёл:\n📩 {resp_text[:150]}")
            add_error(f"промо {code}: отклонён ботом")
            return True  # не ошибка соединения — повторять бессмысленно, считаем обработанным

        if resp.buttons:
            for r in resp.buttons:
                for b in r:
                    url = getattr(b, 'url', None)
                    b_text = b.text.lower()
                    if url:
                        tme = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                        if tme and not tme.group(1).lower().endswith('bot'):
                            await safe_join(tme.group(1))
                    if any(w in b_text for w in ["подписал", "проверить", "готово", "я подписал", "check"]):
                        await human_delay(4, 7)
                        await b.click()

        _stats["promos_activated"].append(code)
        _stats["stars_earned"] += 2.0
        await send_report(f"✅ Промокод `{code}` активирован! +2⭐\n📩 {resp_text[:100]}")
        return True


async def activate_promo(code: str):
    global _activated_promos, _stats
    if code.lower() in _activated_promos:
        logger.info(f"ℹ️ Промокод уже активировали: {code}")
        return
    if code.lower() in _promo_in_progress:
        logger.info(f"ℹ️ Промокод уже в процессе активации: {code}")
        return

    _promo_in_progress.add(code.lower())
    await send_report(f"🚀 Активирую промокод: `{code}`")
    try:
        success = await _activate_promo_attempt(code)
        if not success:
            logger.warning(f"⚠️ Промокод {code}: попытка #1 неудачна, повторяю сразу")
            await asyncio.sleep(2)
            success = await _activate_promo_attempt(code)

        if success:
            _activated_promos.add(code.lower())
            if len(_activated_promos) > 50:
                _activated_promos = set(list(_activated_promos)[-20:])
        else:
            await send_report(f"❌ Промокод `{code}`: обе попытки неудачны, код будет повторно активирован если увидим его снова")
    finally:
        _promo_in_progress.discard(code.lower())


def find_promo_in_text(text: str):
    if not text:
        return None
    lower_text = text.lower()
    if not any(w in lower_text for w in ["промо", "активац", "promo"]):
        return None

    # Сначала ищем точный паттерн "активаций: КОД"
    exact = re.search(r'активаций\s*:\s*([a-zA-Z0-9]{3,20})', text, re.IGNORECASE)
    if exact:
        return exact.group(1)

    # Потом общий поиск
    clean_text = text.replace("**", " ").replace("__", " ").replace("`", " ").replace("||", " ")
    for char in [".", ",", "!", "?", ":", ";", "()", "[]", "»", "«"]:
        clean_text = clean_text.replace(char, " ")

    # Берём слово ПОСЛЕ "активаций"
    after_match = re.search(r'активаций\s+(\S+)', clean_text, re.IGNORECASE)
    if after_match:
        word = after_match.group(1).strip()
        if re.match(r'^[a-zA-Z0-9]{3,20}$', word) and word.lower() not in IGNORED_WORDS:
            return word

    return None


# --- Ловля чека (кнопка "Получить") ---
_claimed_checks = set()


async def try_claim_check(event):
    """Моментально нажимает кнопку Получить при появлении чека в канале"""
    msg = event.message
    if not msg.buttons:
        return

    text = (msg.text or "").lower()
    # Определяем что это чек
    if not any(w in text for w in ["чек", "check", "получить", "claim", "⭐", "звезд"]):
        return

    msg_id = msg.id
    if msg_id in _claimed_checks:
        return

    # Ищем кнопку "Получить"
    for row in msg.buttons:
        for btn in row:
            if any(w in btn.text.lower() for w in ["получить", "claim", "забрать", "взять", "активировать", "открыть", "🎁"]):
                logger.info(f"🎁 ЧЕК НАЙДЕН! Нажимаю '{btn.text}' МОМЕНТАЛЬНО!")
                _claimed_checks.add(msg_id)
                try:
                    await btn.click()
                    await send_report(
                        f"🎁 ЧЕК ПОЙМАН И НАЖАТ!\n"
                        f"📝 {msg.text[:100] if msg.text else 'без текста'}"
                    )
                    logger.info("✅ Чек забран!")
                except Exception as e:
                    logger.warning(f"⚠️ Первая попытка клика по чеку не удалась, пробую ещё раз: {e}")
                    try:
                        await btn.click()
                        await send_report(
                            f"🎁 ЧЕК ПОЙМАН СО ВТОРОЙ ПОПЫТКИ!\n"
                            f"📝 {msg.text[:100] if msg.text else 'без текста'}"
                        )
                        logger.info("✅ Чек забран (2-я попытка)!")
                    except Exception as e2:
                        logger.error(f"⚠️ Ошибка клика по чеку: {e2}")
                        await send_report(f"⚠️ Чек найден но не нажат: {e2}")
                return


# --- Self-ping (анти-засыпание на бесплатном тарифе Render) ---
# Render.com усыпляет бесплатный веб-сервис после ~15 минут без входящих HTTP-запросов.
# Именно поэтому промокоды/чеки раньше "ловились" только через ~15 минут — сервис
# засыпал и просыпался лишь при следующем внешнем запросе. Этот цикл сам стучится
# в /health каждые ~4 минуты, не давая сервису уснуть, поэтому канал слушается 24/7
# и реакция на промокод/чек остаётся мгновенной (события ловятся сразу, без задержек).
async def keep_alive_cycle():
    external_url = os.getenv("RENDER_EXTERNAL_URL")
    if not external_url:
        logger.info("ℹ️ RENDER_EXTERNAL_URL не задан — self-ping пропущен (не Render/локальный запуск)")
        return

    ping_url = external_url.rstrip('/') + '/health'
    logger.info(f"💓 Self-ping включён: {ping_url} каждые ~4 мин")

    while True:
        await asyncio.sleep(240 + random.randint(-15, 15))  # ~4 минуты, с небольшим разбросом
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(ping_url, timeout=15).read())
            logger.info("💓 Self-ping OK")
        except Exception as e:
            logger.warning(f"⚠️ Self-ping ошибка: {e}")


# --- Статистика в 20:00 ---
async def stats_cycle():
    while True:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
        # Ждём до 20:00
        target = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        logger.info(f"📊 Статистика через {int(wait_sec//3600)}ч {int((wait_sec%3600)//60)}м")
        await asyncio.sleep(wait_sec)

        # Собираем и отправляем статистику
        s = _stats
        promos_str = ", ".join(s["promos_activated"]) if s["promos_activated"] else "нет"
        errors_str = "\n".join(s["errors"][-5:]) if s["errors"] else "нет"

        report = (
            f"📊 СТАТИСТИКА ЗА ДЕНЬ\n"
            f"{'━' * 25}\n"
            f"📅 Ежедневка: {'✅ собрана' if s['daily_collected'] else '❌ не собрана'}\n"
            f"📋 Задания: ✅{s['tasks_completed']} | ⏭️{s['tasks_skipped']}\n"
            f"🎮 Кликер: {s['clicker_clicks']}/10 кликов\n"
            f"🔔 Промокоды: {promos_str}\n"
            f"⭐ Примерно заработано: ~{s['stars_earned']:.1f}⭐\n"
            f"{'━' * 25}\n"
            f"❌ Ошибки:\n{errors_str}"
        )
        await send_report(report)
        reset_stats()


# --- Кликер ---
async def clicker_cycle():
    global _stats
    CLICKER_TIMES_PER_DAY = 10
    CLICKER_START_HOUR = 8
    CLICKER_END_HOUR = 24

    while True:
        try:
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)

            if now.hour < CLICKER_START_HOUR:
                wake_at = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
                wait_sec = (wake_at - now).total_seconds()
                logger.info(f"🕐 Кликер спит до 08:00")
                await asyncio.sleep(wait_sec)
                continue

            window_sec = (CLICKER_END_HOUR - CLICKER_START_HOUR) * 3600
            part_sec = window_sec // CLICKER_TIMES_PER_DAY
            day_start = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)

            click_times = []
            for i in range(CLICKER_TIMES_PER_DAY):
                part_start = day_start + datetime.timedelta(seconds=i * part_sec)
                rand_sec = random.randint(0, int(part_sec * 0.8))
                click_time = part_start + datetime.timedelta(seconds=rand_sec + random.randint(0, 300))
                midnight = day_start + datetime.timedelta(hours=16)
                if click_time > midnight:
                    click_time = midnight - datetime.timedelta(seconds=random.randint(60, 600))
                click_times.append(click_time)

            click_times.sort()
            logger.info(f"🎮 Кликер: {len(click_times)} кликов запланировано")

            for i, click_time in enumerate(click_times):
                now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
                if click_time <= now:
                    continue

                wait_sec = (click_time - now).total_seconds()
                h, m = int(wait_sec // 3600), int((wait_sec % 3600) // 60)
                logger.info(f"🎮 Клик #{i+1}/10 через {h}ч {m}м")
                await asyncio.sleep(wait_sec)

                try:
                    async with BOT_LOCK:
                        await client.send_message(TARGET_BOT, '/start')
                        await human_delay(3, 5)
                        msg = await get_bot_response()
                        if not msg or not msg.buttons:
                            continue

                        if "проверка" in (msg.text or "").lower():
                            solved = await solve_captcha_if_needed(msg)
                            if solved:
                                await asyncio.sleep(3)
                                msg = await get_bot_response()
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
                            resp = await get_bot_response()
                            if resp and "проверка" in (resp.text or "").lower():
                                solved = await solve_captcha_if_needed(resp)
                                if solved:
                                    await asyncio.sleep(4)
                                    resp = await get_bot_response()
                            resp_text = (resp.text or "") if resp else ""
                            _stats["clicker_clicks"] += 1
                            _stats["stars_earned"] += 0.1
                            logger.info(f"🎮 Клик #{i+1} собран!")

                except Exception as e:
                    logger.error(f"🎮 Ошибка клика #{i+1}: {e}")
                    add_error(f"кликер#{i+1}: {str(e)[:40]}")

            now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
            tomorrow_8 = (now + datetime.timedelta(days=1)).replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
            wait_sec = (tomorrow_8 - now).total_seconds()
            await asyncio.sleep(wait_sec)

        except Exception as e:
            logger.error(f"Ошибка кликера: {e}")
            await asyncio.sleep(3600)


# --- Обработчики канала ---
if client:
    @client.on(events.NewMessage(chats=CHANNEL_SOURCE))
    @client.on(events.MessageEdited(chats=CHANNEL_SOURCE))
    async def handle_channel(event):
        raw = event.raw_text or ""
        logger.info(f"📨 Канал: {raw[:120]}")

        # 1. МОМЕНТАЛЬНАЯ ловля чека
        asyncio.create_task(try_claim_check(event))

        # 2. Промокод
        code = find_promo_in_text(raw)
        if code:
            logger.info(f"🔔 Промокод найден: {code}")
            asyncio.create_task(activate_promo(code))
        elif any(w in raw.lower() for w in ["промо", "активац"]):
            await send_report(f"🔔 Пост с промо но код не найден:\n{raw[:200]}")


# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if client:
        await client.start()
        logger.info("✅ Telegram client запущен")
        await send_report(
            "🤖 Бот запущен (обновлённая версия)!\n"
            "📅 Ежедневка: по времени бота\n"
            "📋 Задания: каждые ~3 часа\n"
            "🔔 Промокоды: мониторинг 24/7, реакция мгновенная\n"
            "🎁 Чеки: моментальный перехват\n"
            "🎮 Кликер: 10 раз в день (8:00-00:00)\n"
            "📊 Статистика: каждый день в 20:00\n"
            "💓 Анти-сон: self-ping каждые ~4 мин (Render не усыпит сервис)"
        )

        async def delayed_tasks():
            await asyncio.sleep(30)
            await tasks_cycle()

        t1 = asyncio.create_task(daily_cycle())
        t2 = asyncio.create_task(delayed_tasks())
        t3 = asyncio.create_task(clicker_cycle())
        t4 = asyncio.create_task(stats_cycle())
        t5 = asyncio.create_task(keep_alive_cycle())
        yield
        t1.cancel()
        t2.cancel()
        t3.cancel()
        t4.cancel()
        t5.cancel()
        await client.disconnect()
    else:
        logger.warning("⚠️ SESSION_STRING не задан")
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok"}
