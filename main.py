
import re
import os
import sys
import random
import asyncio
import logging
import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, StartBotRequest
from telethon.errors import UserAlreadyParticipantError, InviteHashInvalidError

# --- Конфигурация ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

API_ID = 31206776[cite: 1]
API_HASH = '7ca69453a014d6a87d67afe4d9df8284'[cite: 1]
TARGET_BOT = 'patrickstarsrobot'[cite: 1]
CHANNEL_SOURCE = 'patrickstarsfarm'[cite: 1]
TARGET_USER = 'sanzezzz'[cite: 1]
TIMEZONE_OFFSET = 3  # UTC+3[cite: 1]

ALL_DONE_PHRASES = [
    "выполнил все задания",
    "все задания выполнены",
    "приходи через",
    "новых заданий нет",
    "заданий больше нет",
    "респект",
    "молодец",
]

# Английские слова-исключения, которые бот не должен принять за промокод
IGNORED_WORDS = {
    "html", "http", "https", "main", "patrickstarsrobot", "patrickstarsfarm", 
    "profile", "claim", "check", "start", "webapp", "join", "bot", "stars",
    "tgwebapp", "startapp", "go", "app", "link", "channel", "group", "send"
}

SESSION_STRING = os.getenv("TELEGRAM_SESSION")[cite: 1]
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) if SESSION_STRING else None[cite: 1]


# --- Утилиты ---
def solve_math(text: str):[cite: 1]
    match = re.search(r'(\d+)\s*([\+\-\*\/])\s*(\d+)\s*=\s*\?', text)[cite: 1]
    if match:[cite: 1]
        a, op, b = int(match.group(1)), match.group(2), int(match.group(3))[cite: 1]
        if op == '+': return a + b[cite: 1]
        if op == '-': return a - b[cite: 1]
        if op == '*': return a * b[cite: 1]
        if op == '/' and b != 0: return a // b[cite: 1]
    expression = re.sub(r'[^\d\+\-\*\/\(\)\s]', '', text)[cite: 1]
    try:[cite: 1]
        return int(eval(expression))[cite: 1]
    except Exception:[cite: 1]
        return None[cite: 1]


async def human_delay(min_sec: float = 2.0, max_sec: float = 4.0):[cite: 1]
    await asyncio.sleep(random.uniform(min_sec, max_sec))[cite: 1]


async def send_report(text: str):[cite: 1]
    try:[cite: 1]
        await client.send_message(TARGET_USER, text)[cite: 1]
        logger.info(f"📤 Отчёт: {text[:80]}")[cite: 1]
    except Exception as e:[cite: 1]
        logger.error(f"Ошибка отправки отчёта: {e}")[cite: 1]


def random_task_interval() -> int:[cite: 1]
    base = random.randint(2 * 3600 + 15 * 60, 3 * 3600 + 45 * 60)[cite: 1]
    extra = random.randint(0, 300)[cite: 1]
    return base + extra[cite: 1]


# --- Ждём свежий ответ от бота ---
async def open_bot():[cite: 1]
    await client.send_message(TARGET_BOT, '/start')[cite: 1]
    await asyncio.sleep(5)[cite: 1]

    msg = await get_bot_response()[cite: 1]
    if not msg:[cite: 1]
        return None[cite: 1]

    if "проверка" in (msg.text or "").lower():[cite: 1]
        solved = await solve_captcha_if_needed(msg)[cite: 1]
        if solved:[cite: 1]
            await asyncio.sleep(4)[cite: 1]
            msg = await get_bot_response()[cite: 1]
        else:[cite: 1]
            return None[cite: 1]

    return msg[cite: 1]


async def get_bot_response(max_age: int = 120, attempts: int = 8):[cite: 1]
    """Ждёт свежий ответ от бота"""
    for i in range(attempts):[cite: 1]
        await asyncio.sleep(2)[cite: 1]
        now = datetime.datetime.now(datetime.timezone.utc)[cite: 1]
        async for msg in client.iter_messages(TARGET_BOT, limit=5):[cite: 1]
            age = (now - msg.date).total_seconds()[cite: 1]
            logger.info(f"📨 Сообщение age={int(age)}с | text={str(msg.text or '')[:50]} | buttons={bool(msg.buttons)}")[cite: 1]
            if age < max_age and (msg.text or msg.buttons):[cite: 1]
                return msg[cite: 1]
        logger.info(f"⏳ Жду ответ бота... попытка {i + 1}/{attempts}")[cite: 1]
    async for msg in client.iter_messages(TARGET_BOT, limit=1):[cite: 1]
        return msg[cite: 1]
    return None[cite: 1]


# --- Определить WebApp кнопку ---
def is_webapp_button(btn) -> bool:[cite: 1]
    btn_text = btn.text.lower()[cite: 1]
    if any(w in btn_text for w in [[cite: 1]
        "запустить приложение", "открыть приложение",[cite: 1]
        "webapp", "open app", "launch app"[cite: 1]
    ]):[cite: 1]
        return True[cite: 1]
    if type(btn).__name__ in [[cite: 1]
        'KeyboardButtonWebView',[cite: 1]
        'InlineKeyboardButtonWebApp',[cite: 1]
        'KeyboardButtonSimpleWebView'[cite: 1]
    ]:[cite: 1]
        return True[cite: 1]
    url = getattr(btn, 'url', None)[cite: 1]
    if url and any(w in url.lower() for w in ['startapp', 'tgwebapp']):[cite: 1]
        return True[cite: 1]
    return False[cite: 1]


# --- Запуск startapp (мини-апп через StartBotRequest) ---
async def handle_startapp(url: str) -> bool:[cite: 1]
    try:[cite: 1]
        match = re.search(r't\.me/([a-zA-Z0-9_]+)(?:/\w+)?\?startapp=([a-zA-Z0-9_]+)', url)[cite: 1]
        if not match:[cite: 1]
            logger.warning(f"⚠️ startapp: не распознал URL: {url}")[cite: 1]
            return False[cite: 1]

        bot_username = match.group(1)[cite: 1]
        start_param = match.group(2)[cite: 1]
        logger.info(f"🚀 StartApp: @{bot_username} param={start_param}")[cite: 1]

        bot = await client.get_entity(bot_username)[cite: 1]
        await client(StartBotRequest(bot=bot, peer=bot, start_param=start_param))[cite: 1]
        logger.info(f"✅ StartApp запущен: @{bot_username}")[cite: 1]
        await human_delay(25, 35)[cite: 1]
        return True[cite: 1]

    except Exception as e:[cite: 1]
        logger.error(f"⚠️ StartApp ошибка: {e}")[cite: 1]
        return False[cite: 1]


# --- Вступление в канал/группу ---
async def safe_join(target: str):[cite: 1]
    try:[cite: 1]
        if target.startswith('+'):[cite: 1]
            await client(ImportChatInviteRequest(target[1:]))[cite: 1]
            logger.info(f"✅ Вступил по инвайту: {target}")[cite: 1]
        else:[cite: 1]
            await client(JoinChannelRequest(target))[cite: 1]
            logger.info(f"✅ Подписался: @{target}")[cite: 1]
        await human_delay(4, 8)[cite: 1]
    except UserAlreadyParticipantError:[cite: 1]
        logger.info(f"ℹ️ Уже подписан: {target}")[cite: 1]
    except InviteHashInvalidError:[cite: 1]
        logger.warning(f"⚠️ Невалидный инвайт: {target}")[cite: 1]
    except Exception as e:[cite: 1]
        logger.error(f"Ошибка вступления {target}: {e}")[cite: 1]


FRUIT_MAP = {[cite: 1]
    "🍎": ["яблок", "apple"],[cite: 1]
    "🍊": ["апельсин", "orange"],[cite: 1]
    "🍋": ["лимон", "lemon"],[cite: 1]
    "🍇": ["виноград", "grape"],[cite: 1]
    "🍓": ["клубник", "strawberry"],[cite: 1]
    "🍒": ["вишн", "черешн", "cherry"],[cite: 1]
    "🍑": ["персик", "peach"],[cite: 1]
    "🍍": ["ананас", "pineapple"],[cite: 1]
    "🍌": ["банан", "banana"],[cite: 1]
    "🍅": ["помидор", "томат", "tomato"],[cite: 1]
    "🥝": ["киви", "kiwi"],[cite: 1]
    "🍐": ["груш", "pear"],[cite: 1]
    "🫐": ["черник", "голубик", "blueberry"],[cite: 1]
    "🥭": ["манго", "mango"],[cite: 1]
    "🍉": ["арбуз", "watermelon"],[cite: 1]
    "🍈": ["дын", "melon"],[cite: 1]
    "🌽": ["кукуруз", "corn"],[cite: 1]
    "🥑": ["авокадо", "avocado"],[cite: 1]
    "🍆": ["баклажан", "eggplant"],[cite: 1]
    "🥕": ["морков", "carrot"],[cite: 1]
}[cite: 1]


async def solve_captcha_if_needed(msg) -> bool:[cite: 1]
    """Решает капчу. Поддерживает математику и выбор фрукта по эмодзи."""
    if not msg:[cite: 1]
        return False[cite: 1]

    text = msg.text or ""[cite: 1]

    if "проверка на робота" not in text.lower():[cite: 1]
        return True[cite: 1]

    logger.info(f"🤖 Капча обнаружена: {text[:150]}")[cite: 1]

    if not msg.buttons:[cite: 1]
        await send_report("⚠️ Капча без кнопок — не могу решить")[cite: 1]
        return False[cite: 1]

    all_btns = [b.text for row in msg.buttons for b in row][cite: 1]
    logger.info(f"🔘 Кнопки капчи: {all_btns}")[cite: 1]

    res = solve_math(text)[cite: 1]
    if res is not None:[cite: 1]
        for row in msg.buttons:[cite: 1]
            for btn in row:[cite: 1]
                if btn.text.strip() == str(res):[cite: 1]
                    await btn.click()[cite: 1]
                    logger.info(f"✅ Математическая капча: {res}")[cite: 1]
                    await human_delay(2, 3)[cite: 1]
                    await send_report(f"🤖 Капча (математика) решена! Ответ: {res}")[cite: 1]
                    return True[cite: 1]
        await send_report(f"⚠️ Матем. капча: ответ {res} не найден среди {all_btns}")[cite: 1]
        return False[cite: 1]

    clean_text = re.sub(r"\*+", "", text).lower()[cite: 1]
    word_match = re.search(r"[«\"'](\S+)[»\"\']", clean_text)[cite: 1]
    if word_match:[cite: 1]
        target_word = word_match.group(1).lower().strip()[cite: 1]
        logger.info(f"🔍 Ищу фрукт: '{target_word}'")[cite: 1]

        target_emoji = None[cite: 1]
        for emoji, keywords in FRUIT_MAP.items():[cite: 1]
            for kw in keywords:[cite: 1]
                if kw in target_word or target_word in kw:[cite: 1]
                    target_emoji = emoji[cite: 1]
                    logger.info(f"🍎 Нашёл эмодзи для '{target_word}': {emoji}")[cite: 1]
                    break[cite: 1]
            if target_emoji:[cite: 1]
                break[cite: 1]

        for row in msg.buttons:[cite: 1]
            for btn in row:[cite: 1]
                btn_text = btn.text.strip()[cite: 1]
                if target_emoji and target_emoji in btn_text:[cite: 1]
                    await btn.click()[cite: 1]
                    logger.info(f"✅ Фруктовая капча: нажал '{btn_text}'")[cite: 1]
                    await human_delay(2, 3)[cite: 1]
                    await send_report(f"🤖 Капча решена! '{target_word}' → {btn_text}")[cite: 1]
                    return True[cite: 1]
                if target_word in btn_text.lower():[cite: 1]
                    await btn.click()[cite: 1]
                    logger.info(f"✅ Фруктовая капча (текст): нажал '{btn_text}'")[cite: 1]
                    await human_delay(2, 3)[cite: 1]
                    await send_report(f"🤖 Капча решена! Нажал: {btn_text}")[cite: 1]
                    return True[cite: 1]

        await send_report([cite: 1]
            f"⚠️ Капча: не нашёл '{target_word}' (эмодзи: {target_emoji})"[cite: 1]
            f" среди {all_btns}"[cite: 1]
        )[cite: 1]
        return False[cite: 1]

    await send_report(f"⚠️ Капча: неизвестный тип\n📄 {text[:150]}\n🔘 {all_btns}")[cite: 1]
    return False[cite: 1]


async def start_other_bot(bot_username: str):[cite: 1]
    try:[cite: 1]
        bot = await client.get_entity(bot_username)[cite: 1]
        await client(StartBotRequest(bot=bot, peer=bot, start_param=''))[cite: 1]
        logger.info(f"✅ Запустил бота: @{bot_username}")[cite: 1]
        await human_delay(3, 5)[cite: 1]
    except Exception as e:[cite: 1]
        logger.error(f"Ошибка запуска бота {bot_username}: {e}")[cite: 1]


# --- Сбор ежедневки ---
async def collect_daily():[cite: 1]
    try:[cite: 1]
        logger.info("📅 Собираю ежедневку...")[cite: 1]
        await send_report("📅 Начинаю сбор ежедневки...")[cite: 1]

        msg = await open_bot()[cite: 1]
        if not msg or not msg.buttons:[cite: 1]
            await send_report("⚠️ Ежедневка: бот не ответил на /start")[cite: 1]
            return False[cite: 1]

        profile_clicked = False[cite: 1]
        for row in msg.buttons:[cite: 1]
            for btn in row:[cite: 1]
                if any(w in btn.text.lower() for w in ["профил", "profile"]):[cite: 1]
                    await btn.click()[cite: 1]
                    logger.info(f"👤 Нажал: {btn.text}")[cite: 1]
                    profile_clicked = True[cite: 1]
                    await human_delay(3, 5)[cite: 1]
                    break[cite: 1]
            if profile_clicked:[cite: 1]
                break[cite: 1]

        if not profile_clicked:[cite: 1]
            await send_report("⚠️ Ежедневка: кнопка Профиль не найдена")[cite: 1]
            return False[cite: 1]

        msg = await get_bot_response()[cite: 1]
        if not msg or not msg.buttons:[cite: 1]
            await send_report("⚠️ Ежедневка: нет ответа после Профиля")[cite: 1]
            return False[cite: 1]

        daily_clicked = False[cite: 1]
        for row in msg.buttons:[cite: 1]
            for btn in row:[cite: 1]
                if any(w in btn.text.lower() for w in ["еже", "daily", "бонус"]):[cite: 1]
                    await btn.click()[cite: 1]
                    logger.info(f"📅 Нажал: {btn.text}")[cite: 1]
                    daily_clicked = True[cite: 1]
                    await human_delay(4, 6)[cite: 1]
                    break[cite: 1]
            if daily_clicked:[cite: 1]
                break[cite: 1]

        if not daily_clicked:[cite: 1]
            await send_report("⚠️ Ежедневка: кнопка не найдена в профиле")[cite: 1]
            return False[cite: 1]

        msg = await get_bot_response()[cite: 1]
        if not msg:[cite: 1]
            await send_report("⚠️ Ежедневка: нет ответа с капчей")[cite: 1]
            return False[cite: 1]

        text = msg.text or ""[cite: 1]

        if any(w in text.lower() for w in ["уже", "завтра", "получил"]):[cite: 1]
            logger.info("ℹ️ Ежедневка уже забрана")[cite: 1]
            await send_report("ℹ️ Ежедневка уже забрана сегодня")[cite: 1]
            return True[cite: 1]

        if not msg.buttons:[cite: 1]
            await send_report(f"⚠️ Ежедневка: нет кнопок капчи\n📄 {text[:150]}")[cite: 1]
            return False[cite: 1]

        res = solve_math(text)[cite: 1]
        if res is None:[cite: 1]
            await send_report(f"⚠️ Капча: не нашёл математику\n📄 {text[:100]}")[cite: 1]
            return False[cite: 1]

        logger.info(f"🔢 Капча: {text[:60]} | Ответ: {res}")[cite: 1]

        found_answer = False[cite: 1]
        for row in msg.buttons:[cite: 1]
            for btn in row:[cite: 1]
                if btn.text.strip() == str(res):[cite: 1]
                    await btn.click()[cite: 1]
                    logger.info(f"✅ Нажал ответ капчи: {res}")[cite: 1]
                    found_answer = True[cite: 1]
                    await human_delay(2, 3)[cite: 1]
                    resp = await get_bot_response()[cite: 1]
                    resp_text = (resp.text or "") if resp else ""[cite: 1]
                    await send_report([cite: 1]
                        f"📅 Ежедневка собрана!\n"[cite: 1]
                        f"🔢 Капча: {res}\n"[cite: 1]
                        f"📩 Бот: {resp_text[:150]}"[cite: 1]
                    )[cite: 1]
                    return True[cite: 1]

        if not found_answer:[cite: 1]
            all_btns = [b.text for row in msg.buttons for b in row][cite: 1]
            await send_report(f"⚠️ Капча: ответ {res} не найден среди {all_btns}")[cite: 1]
            return False[cite: 1]

    except Exception as e:[cite: 1]
        logger.error(f"Ошибка ежедневки: {e}")[cite: 1]
        await send_report(f"❌ Ошибка ежедневки: {e}")[cite: 1]
        return False[cite: 1]


async def wait_until_midnight():[cite: 1]
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)[cite: 1]
    tomorrow = (now + datetime.timedelta(days=1)).replace([cite: 1]
        hour=0, minute=0, second=5, microsecond=0[cite: 1]
    )[cite: 1]
    wait_sec = (tomorrow - now).total_seconds()[cite: 1]
    h, m = int(wait_sec // 3600), int((wait_sec % 3600) // 60)[cite: 1]
    logger.info(f"⏳ Ежедневка через {h}ч {m}м (в 00:00 UTC+{TIMEZONE_OFFSET})")[cite: 1]
    await asyncio.sleep(wait_sec)[cite: 1]


async def daily_cycle():[cite: 1]
    await collect_daily()[cite: 1]
    while True:[cite: 1]
        await wait_until_midnight()[cite: 1]
        await collect_daily()[cite: 1]


# --- Обработка одного задания ---
async def process_one_task(task_msg) -> tuple[str, str]:[cite: 1]
    text = task_msg.text or ""[cite: 1]

    if any(phrase in text.lower() for phrase in ALL_DONE_PHRASES):[cite: 1]
        return 'all_done', text[cite: 1]

    if task_msg.buttons:[cite: 1]
        for row in task_msg.buttons:[cite: 1]
            for btn in row:[cite: 1]
                url = getattr(btn, 'url', None)[cite: 1]
                btn_type = type(btn).__name__[cite: 1]
                logger.info(f"🔘 '{btn.text}' | url: {url} | type: {btn_type}")[cite: 1]

    has_webapp = False[cite: 1]
    if task_msg.buttons:[cite: 1]
        for row in task_msg.buttons:[cite: 1]
            for btn in row:[cite: 1]
                if is_webapp_button(btn):[cite: 1]
                    has_webapp = True[cite: 1]
                    break[cite: 1]
            if has_webapp:[cite: 1]
                break[cite: 1]

    if has_webapp:[cite: 1]
        logger.info("🎮 Мини-апп обнаружен — пробуем startapp")[cite: 1]

        startapp_url = None[cite: 1]
        if task_msg.buttons:[cite: 1]
            for row in task_msg.buttons:[cite: 1]
                for btn in row:[cite: 1]
                    url = getattr(btn, 'url', None)[cite: 1]
                    if url and 'startapp' in url.lower():[cite: 1]
                        startapp_url = url[cite: 1]
                        break[cite: 1]
                if startapp_url:[cite: 1]
                    break[cite: 1]

        if startapp_url:[cite: 1]
            startapp_done = await handle_startapp(startapp_url)[cite: 1]
            if startapp_done:[cite: 1]
                fresh_msg = await get_bot_response()[cite: 1]
                confirmed = False[cite: 1]
                if fresh_msg and fresh_msg.buttons:[cite: 1]
                    for row in fresh_msg.buttons:[cite: 1]
                        for btn in row:[cite: 1]
                            if any(w in btn.text.lower() for w in ["подтвер", "подтвердить запуск"]):[cite: 1]
                                await btn.click()[cite: 1]
                                logger.info(f"✅ Подтвердил запуск приложения: {btn.text}")[cite: 1]
                                confirmed = True[cite: 1]
                                await human_delay(2, 3)[cite: 1]
                                break[cite: 1]
                        if confirmed:[cite: 1]
                            break[cite: 1]
                if confirmed:[cite: 1]
                    await send_report(f"🚀 Приложение запущено и подтверждено!\n📄 {text[:100]}")[cite: 1]
                    return 'done', f'startapp ({startapp_url[:50]})'[cite: 1]
                else:[cite: 1]
                    await send_report(f"🚀 StartApp выполнен, но кнопка подтверждения не найдена\n📄 {text[:100]}")[cite: 1]
                    return 'done', f'startapp без подтверждения'[cite: 1]

        skipped = False[cite: 1]
        if task_msg.buttons:[cite: 1]
            for row in task_msg.buttons:[cite: 1]
                for btn in row:[cite: 1]
                    if "пропустить" in btn.text.lower():[cite: 1]
                        await btn.click()[cite: 1]
                        logger.info("⏭️ Нажал Пропустить (мини-апп)")[cite: 1]
                        skipped = True[cite: 1]
                        await human_delay(2, 3)[cite: 1]
                        break[cite: 1]
                if skipped:[cite: 1]
                    break[cite: 1]

        if skipped:[cite: 1]
            await send_report(f"⏭️ Мини-апп пропущен\n📄 {text[:100]}")[cite: 1]
            return 'skip', 'мини-апп (пропущен)'[cite: 1]
        else:[cite: 1]
            await send_report([cite: 1]
                f"⚠️ Мини-апп без кнопки Пропустить\n"[cite: 1]
                f"📄 {text[:150]}\n"[cite: 1]
                f"👆 Выполни вручную, жду следующей проверки"[cite: 1]
            )[cite: 1]
            return 'all_done', text[cite: 1]

    action_done = False[cite: 1]
    action_desc = ""[cite: 1]

    if task_msg.buttons:[cite: 1]
        for row in task_msg.buttons:[cite: 1]
            for btn in row:[cite: 1]
                url = getattr(btn, 'url', None)[cite: 1]
                btn_text = btn.text.lower()[cite: 1]
                if any(w in btn_text for w in ["подтвер", "пропустить", "меню", "назад", "главное"]):[cite: 1]
                    continue[cite: 1]
                if url:[cite: 1]
                    tme_match = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)[cite: 1]
                    if tme_match:[cite: 1]
                        target = tme_match.group(1)[cite: 1]
                        if target.lower().endswith('bot'):[cite: 1]
                            await start_other_bot(target)[cite: 1]
                            action_desc = f"запустил бота @{target}"[cite: 1]
                        else:[cite: 1]
                            await safe_join(target)[cite: 1]
                            action_desc = f"подписался на {target}"[cite: 1]
                        action_done = True[cite: 1]

    if not action_done:[cite: 1]
        for target in re.findall(r't\.me/(\+?[a-zA-Z0-9_]+)', text):[cite: 1]
            if target.lower().endswith('bot'):[cite: 1]
                await start_other_bot(target)[cite: 1]
                action_desc = f"запустил бота @{target}"[cite: 1]
            else:[cite: 1]
                await safe_join(target)[cite: 1]
                action_desc = f"подписался на {target}"[cite: 1]
            action_done = True[cite: 1]

        for username in re.findall(r'@([a-zA-Z0-9_]{5,})', text):[cite: 1]
            if username.lower().endswith('bot'):[cite: 1]
                await start_other_bot(username)[cite: 1]
                action_desc = f"запустил бота @{username}"[cite: 1]
            else:[cite: 1]
                await safe_join(username)[cite: 1]
                action_desc = f"подписался на @{username}"[cite: 1]
            action_done = True[cite: 1]

    if not action_done:[cite: 1]
        logger.warning(f"⚠️ Тип не определён: {text[:150]}")[cite: 1]
        await send_report(f"⚠️ Не смог определить задание\n📄 {text[:150]}")[cite: 1]
        if task_msg.buttons:[cite: 1]
            for row in task_msg.buttons:[cite: 1]
                for btn in row:[cite: 1]
                    if "пропустить" in btn.text.lower():[cite: 1]
                        await btn.click()[cite: 1]
                        await human_delay(2, 3)[cite: 1]
                        break[cite: 1]
        return 'skip', text[:100][cite: 1]

    await human_delay(6, 12)[cite: 1]

    fresh_msg = await get_bot_response()[cite: 1]
    confirmed = False[cite: 1]
    if fresh_msg and fresh_msg.buttons:[cite: 1]
        for row in fresh_msg.buttons:[cite: 1]
            for btn in row:[cite: 1]
                if any(w in btn.text.lower() for w in [[cite: 1]
                    "подтвер", "выдать", "проверить", "claim", "check"[cite: 1]
                ]):[cite: 1]
                    await btn.click()[cite: 1]
                    logger.info(f"✅ Подтвердил: {btn.text}")[cite: 1]
                    confirmed = True[cite: 1]
                    await human_delay(3, 5)[cite: 1]
                    break[cite: 1]
            if confirmed:[cite: 1]
                break[cite: 1]

    suffix = "" if confirmed else " (без подтверждения)"[cite: 1]
    return 'done', action_desc + suffix[cite: 1]


# --- Цикл заданий ---
async def tasks_cycle():[cite: 1]
    while True:[cite: 1]
        try:[cite: 1]
            logger.info("📋 Запуск цикла заданий...")[cite: 1]
            await send_report("📋 Начинаю выполнение заданий...")[cite: 1]

            msg = await open_bot()[cite: 1]
            if not msg or not msg.buttons:[cite: 1]
                await send_report("⚠️ Бот не ответил на /start")[cite: 1]
                await asyncio.sleep(10800)[cite: 1]
                continue[cite: 1]

            task_opened = False[cite: 1]
            for row in msg.buttons:[cite: 1]
                for btn in row:[cite: 1]
                    if "задани" in btn.text.lower():[cite: 1]
                        await btn.click()[cite: 1]
                        logger.info(f"📋 Нажал: {btn.text}")[cite: 1]
                        task_opened = True[cite: 1]
                        break[cite: 1]
                if task_opened:[cite: 1]
                    break[cite: 1]

            if not task_opened:[cite: 1]
                logger.warning("⚠️ Кнопка заданий не найдена")[cite: 1]
                await send_report("⚠️ Кнопка заданий не найдена")[cite: 1]
                await asyncio.sleep(10800)[cite: 1]
                continue[cite: 1]

            completed = 0[cite: 1]
            skipped = 0[cite: 1]
            reports = [][cite: 1]
            
            last_task_msg_id = None[cite: 1]

            for i in range(30):[cite: 1]
                logger.info(f"🔄 Задание #{i + 1}")[cite: 1]

                task_msg = await get_bot_response()[cite: 1]
                if not task_msg:[cite: 1]
                    logger.warning("⚠️ Сообщение не получено")[cite: 1]
                    await send_report("⚠️ Не получил сообщение от бота")[cite: 1]
                    break[cite: 1]

                if last_task_msg_id and task_msg.id == last_task_msg_id:[cite: 1]
                    logger.warning(f"⚠️ Обнаружен повтор сообщения ID {task_msg.id}. Пытаюсь пропустить...")[cite: 1]
                    
                    skipped_by_button = False[cite: 1]
                    if task_msg.buttons:[cite: 1]
                        for row in task_msg.buttons:[cite: 1]
                            for btn in row:[cite: 1]
                                if "пропустить" in btn.text.lower():[cite: 1]
                                    await btn.click()[cite: 1]
                                    logger.info("⏭️ Нажал Пропустить для выхода из цикла повтора")[cite: 1]
                                    skipped_by_button = True[cite: 1]
                                    await human_delay(3, 5)[cite: 1]
                                    break[cite: 1]
                            if skipped_by_button:[cite: 1]
                                break[cite: 1]
                    
                    if skipped_by_button:[cite: 1]
                        skipped += 1[cite: 1]
                        continue[cite: 1]
                    else:[cite: 1]
                        await send_report(f"⚠️ Задание зациклилось, а кнопки 'Пропустить' нет. Сплю до следующего цикла.")[cite: 1]
                        break[cite: 1]

                last_task_msg_id = task_msg.id[cite: 1]

                result, desc = await process_one_task(task_msg)[cite: 1]

                if result == 'all_done':[cite: 1]
                    interval = random_task_interval()[cite: 1]
                    h, m = interval // 3600, (interval % 3600) // 60[cite: 1]
                    report = ([cite: 1]
                        f"🎉 Все задания выполнены!\n"[cite: 1]
                        f"✅ Выполнено: {completed}\n"[cite: 1]
                        f"⏭️ Пропущено: {skipped}\n"[cite: 1]
                    )[cite: 1]
                    if reports:[cite: 1]
                        report += "\n📋 Что сделал:\n" + "\n".join(reports)[cite: 1]
                    report += f"\n\n⏳ Следующая проверка через {h}ч {m}м"[cite: 1]
                    await send_report(report)[cite: 1]
                    await asyncio.sleep(interval)[cite: 1]
                    break[cite: 1]

                elif result == 'done':[cite: 1]
                    completed += 1[cite: 1]
                    reports.append(f"✅ {completed}. {desc}")[cite: 1]
                    await send_report(f"✅ Задание #{completed} выполнено!\n📌 {desc}")[cite: 1]
                    await human_delay(4, 9)[cite: 1]

                elif result == 'skip':[cite: 1]
                    skipped += 1[cite: 1]
                    reports.append(f"⏭️ {desc[:50]}")[cite: 1]
                    await human_delay(2, 4)[cite: 1]

            else:[cite: 1]
                interval = random_task_interval()[cite: 1]
                h, m = interval // 3600, (interval % 3600) // 60[cite: 1]
                await send_report([cite: 1]
                    f"⚠️ Достигнут лимит заданий (30)\n"[cite: 1]
                    f"✅ {completed} | ⏭️ {skipped}\n"[cite: 1]
                    f"⏳ Следующая через {h}ч {m}м"[cite: 1]
                )[cite: 1]
                await asyncio.sleep(interval)[cite: 1]

        except Exception as e:[cite: 1]
            logger.error(f"Ошибка заданий: {e}")[cite: 1]
            await send_report(f"❌ Ошибка заданий: {e}")[cite: 1]
            await asyncio.sleep(1800)[cite: 1]
            

# --- Промокоды из канала ---
_activated_promos = set()[cite: 1]


async def activate_promo(code: str):[cite: 1]
    """Активирует промокод в боте"""
    global _activated_promos

    if code.lower() in _activated_promos:[cite: 1]
        logger.info(f"ℹ️ Промокод уже активировали: {code}")[cite: 1]
        return[cite: 1]

    _activated_promos.add(code.lower())[cite: 1]
    if len(_activated_promos) > 50:[cite: 1]
        _activated_promos = set(list(_activated_promos)[-20:])[cite: 1]

    logger.info(f"🔔 Активирую промокод: {code}")[cite: 1]
    await send_report(f"🚀 Попытка активации кода: `{code}`...")

    msg = await open_bot()[cite: 1]
    if not msg or not msg.buttons:[cite: 1]
        await send_report("⚠️ Промокод: бот не ответил")[cite: 1]
        return[cite: 1]

    profile_clicked = False[cite: 1]
    for row in msg.buttons:[cite: 1]
        for btn in row:[cite: 1]
            if any(w in btn.text.lower() for w in ["профил", "profile"]):[cite: 1]
                await btn.click()[cite: 1]
                profile_clicked = True[cite: 1]
                await human_delay(2, 3)[cite: 1]
                break[cite: 1]
        if profile_clicked:[cite: 1]
            break[cite: 1]

    if not profile_clicked:[cite: 1]
        await send_report("⚠️ Промокод: кнопка Профиль не найдена")[cite: 1]
        return[cite: 1]

    msg = await get_bot_response()[cite: 1]
    if not msg or not msg.buttons:[cite: 1]
        await send_report("⚠️ Промокод: нет ответа после Профиля")[cite: 1]
        return[cite: 1]

    promo_clicked = False[cite: 1]
    for row in msg.buttons:[cite: 1]
        for btn in row:[cite: 1]
            if "промо" in btn.text.lower():[cite: 1]
                await btn.click()[cite: 1]
                promo_clicked = True[cite: 1]
                await human_delay(2, 3)[cite: 1]
                break[cite: 1]
        if promo_clicked:[cite: 1]
            break[cite: 1]

    if not promo_clicked:[cite: 1]
        await send_report("⚠️ Не нашёл кнопку промокода")[cite: 1]
        return[cite: 1]

    await client.send_message(TARGET_BOT, code)[cite: 1]
    await human_delay(3, 5)[cite: 1]

    resp = await get_bot_response()[cite: 1]
    if not resp:[cite: 1]
        await send_report(f"⚠️ Промокод `{code}`: нет ответа от бота")[cite: 1]
        return[cite: 1]

    resp_text = resp.text or ""[cite: 1]

    if resp.buttons:[cite: 1]
        for r in resp.buttons:[cite: 1]
            for b in r:[cite: 1]
                url = getattr(b, 'url', None)[cite: 1]
                b_text = b.text.lower()[cite: 1]
                if url:[cite: 1]
                    tme = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)[cite: 1]
                    if tme and not tme.group(1).lower().endswith('bot'):[cite: 1]
                        await safe_join(tme.group(1))[cite: 1]
                if any(w in b_text for w in [[cite: 1]
                    "подписал", "проверить", "готово", "я подписал", "check"[cite: 1]
                ]):[cite: 1]
                    await human_delay(4, 7)[cite: 1]
                    await b.click()[cite: 1]
                    logger.info(f"✅ Нажал: {b.text}")[cite: 1]

    await send_report([cite: 1]
        f"✅ Промокод `{code}` отправлен на обработку!\n"[cite: 1]
        f"📩 Ответ бота: {resp_text[:120]}"[cite: 1]
    )[cite: 1]


def find_promo_in_text(text: str):
    """
    Ультимативный неубиваемый поиск промокода.
    Берёт чистую латиницу, так как промокоды всегда английские.
    """
    if not text:
        return None

    lower_text = text.lower()
    
    # Проверяем, есть ли триггеры промо-активности в посте
    if not any(w in lower_text for w in ["промо", "активац", "promo", "код"]):
        return None

    # Очищаем от Markdown и превращаем пунктуацию в пробелы
    clean_text = text.replace("**", " ").replace("__", " ").replace("`", " ").replace("||", " ")
    for char in [".", ",", "!", "?", ":", ";", "(", ")", "[", "]", "-", "+", "/", "»", "«", "'", '"']:
        clean_text = clean_text.replace(char, " ")

    # Проверяем каждое отдельное слово
    words = clean_text.split()
    for word in words:
        word_clean = word.strip()
        
        # Код состоит строго из латинских букв/цифр и имеет длину 4-15 символов
        if 4 <= len(word_clean) <= 15:
            if re.match(r'^[a-zA-Z0-9]+$', word_clean):
                if word_clean.lower() not in IGNORED_WORDS:
                    # Возвращаем в оригинальном регистре (сохраняя заглавные)
                    return word_clean
    return None


if client:
    @client.on(events.NewMessage(chats=CHANNEL_SOURCE))
    @client.on(events.MessageEdited(chats=CHANNEL_SOURCE))
    async def handle_promo(event):
        raw = event.raw_text or ""
        logger.info(f"📨 Новый пост в канале: {raw[:100]}...")
        
        # Если в посте речь идет о промо, МГНОВЕННО шлём лог в ЛС
        if any(w in raw.lower() for w in ["промо", "активац", "promo", "конкурс"]):
            try:
                alert_text = f"🔔 **Обнаружен пост с промокодом в канале!**\n\n📝 **Текст поста:**\n{raw}"
                await client.send_message(TARGET_USER, alert_text)
                logger.info("🚀 Уведомление о посте отправлено админу в ЛС")
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление в ЛС: {e}")

        # Вытаскиваем английский промокод
        code = find_promo_in_text(raw)
        if code:
            await activate_promo(code)


# --- Кликер (10 раз в день, 8:00-00:00, рандомно) ---
async def clicker_cycle():[cite: 1]
    CLICKER_TIMES_PER_DAY = 10[cite: 1]
    CLICKER_START_HOUR = 8   # 8:00
    CLICKER_END_HOUR = 24    # 00:00[cite: 1]

    while True:[cite: 1]
        try:[cite: 1]
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)[cite: 1]

            if now.hour < CLICKER_START_HOUR:[cite: 1]
                wake_at = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)[cite: 1]
                wait_sec = (wake_at - now).total_seconds()[cite: 1]
                logger.info(f"🕐 Кликер спит до 08:00 ({int(wait_sec//3600)}ч {int((wait_sec%3600)//60)}м)")[cite: 1]
                await asyncio.sleep(wait_sec)[cite: 1]
                continue[cite: 1]

            window_sec = (CLICKER_END_HOUR - CLICKER_START_HOUR) * 3600[cite: 1]
            part_sec = window_sec // CLICKER_TIMES_PER_DAY[cite: 1]
            day_start = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)[cite: 1]

            click_times = [][cite: 1]
            for i in range(CLICKER_TIMES_PER_DAY):[cite: 1]
                part_start = day_start + datetime.timedelta(seconds=i * part_sec)[cite: 1]
                rand_sec = random.randint(0, int(part_sec * 0.8))[cite: 1]
                rand_extra = random.randint(0, 300)[cite: 1]
                click_time = part_start + datetime.timedelta(seconds=rand_sec + rand_extra)[cite: 1]
                midnight = day_start + datetime.timedelta(hours=16)[cite: 1]
                if click_time > midnight:[cite: 1]
                    click_time = midnight - datetime.timedelta(seconds=random.randint(60, 600))[cite: 1]
                click_times.append(click_time)[cite: 1]

            click_times.sort()[cite: 1]
            logger.info(f"🎮 Кликер: запланировано {len(click_times)} кликов на сегодня")[cite: 1]

            for i, click_time in enumerate(click_times):[cite: 1]
                now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)[cite: 1]

                if click_time <= now:[cite: 1]
                    logger.info(f"🎮 Клик #{i+1} пропущен (время прошло)")[cite: 1]
                    continue[cite: 1]

                wait_sec = (click_time - now).total_seconds()[cite: 1]
                h, m = int(wait_sec // 3600), int((wait_sec % 3600) // 60)[cite: 1]
                logger.info(f"🎮 Клик #{i+1}/10 через {h}ч {m}м")[cite: 1]
                await asyncio.sleep(wait_sec)[cite: 1]

                try:[cite: 1]
                    await client.send_message(TARGET_BOT, '/start')[cite: 1]
                    await human_delay(3, 5)[cite: 1]

                    msg = await get_bot_response()[cite: 1]
                    if not msg or not msg.buttons:[cite: 1]
                        logger.warning("🎮 Кликер: нет ответа от бота")[cite: 1]
                        continue[cite: 1]

                    if "проверка" in (msg.text or "").lower():[cite: 1]
                        solved = await solve_captcha_if_needed(msg)[cite: 1]
                        if solved:[cite: 1]
                            await asyncio.sleep(3)[cite: 1]
                            msg = await get_bot_response()[cite: 1]
                        else:[cite: 1]
                            continue[cite: 1]

                    clicker_clicked = False[cite: 1]
                    if msg and msg.buttons:[cite: 1]
                        for row in msg.buttons:[cite: 1]
                            for btn in row:[cite: 1]
                                if any(w in btn.text.lower() for w in ["кликер", "clicker", "farm", "✨"]):[cite: 1]
                                    await btn.click()[cite: 1]
                                    logger.info(f"🎮 Нажал кликер: {btn.text}")[cite: 1]
                                    clicker_clicked = True[cite: 1]
                                    await human_delay(2, 4)[cite: 1]
                                    break[cite: 1]
                            if clicker_clicked:[cite: 1]
                                break[cite: 1]

                    if clicker_clicked:[cite: 1]
                        resp = await get_bot_response()[cite: 1]
                        resp_text = (resp.text or "") if resp else ""[cite: 1]
                        logger.info(f"🎮 Кликер #{i+1} собран! {resp_text[:80]}")[cite: 1]
                        
                        # Расчет времени до следующего клика
                        if i + 1 < len(click_times):
                            next_click_time = click_times[i + 1]
                            now_after = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)[cite: 1]
                            next_wait = (next_click_time - now_after).total_seconds()
                            if next_wait > 0:
                                next_h, next_m = int(next_wait // 3600), int((next_wait % 3600) // 60)
                                next_info = f"{next_h}ч {next_m}м"
                            else:
                                next_info = "совсем скоро"
                        else:
                            next_info = "завтра после 08:00"

                        # Отправка красивого отчета в ЛС
                        await send_report(
                            f"🎮 **Кликер: Успешный сбор!**\n"
                            f"✅ Выполнен клик #{i+1}/10\n"
                            f"⏳ Следующий клик через: `{next_info}`\n\n"
                            f"📩 **Ответ Патрика:**\n{resp_text[:120]}"
                        )
                    else:[cite: 1]
                        logger.warning(f"🎮 Кнопка кликера не найдена")[cite: 1]

                except Exception as e:[cite: 1]
                    logger.error(f"🎮 Ошибка клика #{i+1}: {e}")[cite: 1]

            now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)[cite: 1]
            tomorrow_8 = (now + datetime.timedelta(days=1)).replace([cite: 1]
                hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0[cite: 1]
            )[cite: 1]
            wait_sec = (tomorrow_8 - now).total_seconds()[cite: 1]
            logger.info(f"🎮 Кликер завершил день. Следующий цикл через {int(wait_sec//3600)}ч")[cite: 1]
            await asyncio.sleep(wait_sec)[cite: 1]

        except Exception as e:[cite: 1]
            logger.error(f"Ошибка кликера: {e}")[cite: 1]
            await asyncio.sleep(3600)[cite: 1]

# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):[cite: 1]
    if client:[cite: 1]
        await client.start()[cite: 1]
        logger.info("✅ Telegram client запущен")[cite: 1]
        await send_report([cite: 1]
            "🤖 Бот запущен!\n"[cite: 1]
            "📅 Ежедневка: каждый день в 00:00\n"[cite: 1]
            "📋 Задания: каждые ~3 часа\n"[cite: 1]
            "🔔 Промокоды: слежу за каналом 24/7 (перехват 10000000000%)\n"
            "🎮 Кликер: 10 раз в день (8:00-00:00)"[cite: 1]
        )[cite: 1]

        async def delayed_tasks():[cite: 1]
            await asyncio.sleep(30)[cite: 1]
            await tasks_cycle()[cite: 1]

        t1 = asyncio.create_task(daily_cycle())[cite: 1]
        t2 = asyncio.create_task(delayed_tasks())[cite: 1]
        t3 = asyncio.create_task(clicker_cycle())[cite: 1]
        yield[cite: 1]
        t1.cancel()[cite: 1]
        t2.cancel()[cite: 1]
        t3.cancel()[cite: 1]
        await client.disconnect()[cite: 1]
    else:[cite: 1]
        logger.warning("⚠️ SESSION_STRING не задан")[cite: 1]
        yield[cite: 1]


app = FastAPI(lifespan=lifespan)[cite: 1]


@app.get("/")[cite: 1]
@app.get("/health")[cite: 1]
async def health():[cite: 1]
    return {"status": "ok"}[cite: 1]
