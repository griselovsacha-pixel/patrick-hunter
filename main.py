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

API_ID = 31206776
API_HASH = '7ca69453a014d6a87d67afe4d9df8284'
TARGET_BOT = 'patrickstarsrobot'
CHANNEL_SOURCE = 'patrickstarsfarm'
TARGET_USER = 'sanzezzz'
TIMEZONE_OFFSET = 3  # UTC+3

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

SESSION_STRING = os.getenv("TELEGRAM_SESSION")
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) if SESSION_STRING else None


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
    extra = random.randint(0, 300)
    return base + extra


# --- Ждём свежий ответ от бота ---
async def open_bot():
    await client.send_message(TARGET_BOT, '/start')
    await asyncio.sleep(5)

    msg = await get_bot_response()
    if not msg:
        return None

    if "проверка" in (msg.text or "").lower():
        solved = await solve_captcha_if_needed(msg)
        if solved:
            await asyncio.sleep(4)
            msg = await get_bot_response()
        else:
            return None

    return msg


async def get_bot_response(max_age: int = 120, attempts: int = 8):
    """Ждёт свежий ответ от бота"""
    for i in range(attempts):
        await asyncio.sleep(2)
        now = datetime.datetime.now(datetime.timezone.utc)
        async for msg in client.iter_messages(TARGET_BOT, limit=5):
            age = (now - msg.date).total_seconds()
            logger.info(f"📨 Сообщение age={int(age)}с | text={str(msg.text or '')[:50]} | buttons={bool(msg.buttons)}")
            if age < max_age and (msg.text or msg.buttons):
                return msg
        logger.info(f"⏳ Жду ответ бота... попытка {i + 1}/{attempts}")
    async for msg in client.iter_messages(TARGET_BOT, limit=1):
        return msg
    return None


# --- Определить WebApp кнопку ---
def is_webapp_button(btn) -> bool:
    btn_text = btn.text.lower()
    if any(w in btn_text for w in [
        "запустить приложение", "открыть приложение",
        "webapp", "open app", "launch app"
    ]):
        return True
    if type(btn).__name__ in [
        'KeyboardButtonWebView',
        'InlineKeyboardButtonWebApp',
        'KeyboardButtonSimpleWebView'
    ]:
        return True
    url = getattr(btn, 'url', None)
    if url and any(w in url.lower() for w in ['startapp', 'tgwebapp']):
        return True
    return False


# --- Запуск startapp ---
async def handle_startapp(url: str) -> bool:
    try:
        match = re.search(r't\.me/([a-zA-Z0-9_]+)(?:/\w+)?\?startapp=([a-zA-Z0-9_]+)', url)
        if not match:
            logger.warning(f"⚠️ startapp: не распознал URL: {url}")
            return False

        bot_username = match.group(1)
        start_param = match.group(2)
        logger.info(f"🚀 StartApp: @{bot_username} param={start_param}")

        bot = await client.get_entity(bot_username)
        await client(StartBotRequest(bot=bot, peer=bot, start_param=start_param))
        logger.info(f"✅ StartApp запущен: @{bot_username}")
        await human_delay(25, 35)
        return True

    except Exception as e:
        logger.error(f"⚠️ StartApp ошибка: {e}")
        return False


# --- Вступление в канал/группу ---
async def safe_join(target: str):
    try:
        if target.startswith('+'):
            await client(ImportChatInviteRequest(target[1:]))
            logger.info(f"✅ Вступил по инвайту: {target}")
        else:
            await client(JoinChannelRequest(target))
            logger.info(f"✅ Подписался: @{target}")
        await human_delay(4, 8)
    except UserAlreadyParticipantError:
        logger.info(f"ℹ️ Уже подписан: {target}")
    except InviteHashInvalidError:
        logger.warning(f"⚠️ Невалидный инвайт: {target}")
    except Exception as e:
        logger.error(f"Ошибка вступления {target}: {e}")


FRUIT_MAP = {
    "🍎": ["яблок", "apple"],
    "🍊": ["апельсин", "orange"],
    "🍋": ["лимон", "lemon"],
    "🍇": ["виноград", "grape"],
    "🍓": ["клубник", "strawberry"],
    "🍒": ["вишн", "черешн", "cherry"],
    "🍑": ["персик", "peach"],
    "🍍": ["ананас", "pineapple"],
    "🍌": ["банан", "banana"],
    "🍅": ["помидор", "томат", "tomato"],
    "🥝": ["киви", "kiwi"],
    "🍐": ["груш", "pear"],
    "🫐": ["черник", "голубик", "blueberry"],
    "🥭": ["манго", "mango"],
    "🍉": ["арбуз", "watermelon"],
    "🍈": ["дын", "melon"],
    "🌽": ["кукуруз", "corn"],
    "🥑": ["авокадо", "avocado"],
    "🍆": ["баклажан", "eggplant"],
    "🥕": ["морков", "carrot"],
}


async def solve_captcha_if_needed(msg) -> bool:
    """Решает капчу. Поддерживает математику и выбор фрукта по эмодзи."""
    if not msg:
        return False

    text = msg.text or ""

    if "проверка на робота" not in text.lower():
        return True

    logger.info(f"🤖 Капча обнаружена: {text[:150]}")

    if not msg.buttons:
        await send_report("⚠️ Капча без кнопок — не могу решить")
        return False

    all_btns = [b.text for row in msg.buttons for b in row]
    logger.info(f"🔘 Кнопки капчи: {all_btns}")

    res = solve_math(text)
    if res is not None:
        for row in msg.buttons:
            for btn in row:
                if btn.text.strip() == str(res):
                    await btn.click()
                    logger.info(f"✅ Математическая капча: {res}")
                    await human_delay(2, 3)
                    await send_report(f"🤖 Капча (математика) решена! Ответ: {res}")
                    return True
        await send_report(f"⚠️ Матем. капча: ответ {res} не найден среди {all_btns}")
        return False

    clean_text = re.sub(r"\*+", "", text).lower()
    word_match = re.search(r"[«\"'](\S+)[»\"\']", clean_text)
    if word_match:
        target_word = word_match.group(1).lower().strip()
        logger.info(f"🔍 Ищу фрукт: '{target_word}'")

        target_emoji = None
        for emoji, keywords in FRUIT_MAP.items():
            for kw in keywords:
                if kw in target_word or target_word in kw:
                    target_emoji = emoji
                    logger.info(f"🍎 Нашёл эмодзи для '{target_word}': {emoji}")
                    break
            if target_emoji:
                break

        for row in msg.buttons:
            for btn in row:
                btn_text = btn.text.strip()
                if target_emoji and target_emoji in btn_text:
                    await btn.click()
                    logger.info(f"✅ Фруктовая капча: нажал '{btn_text}'")
                    await human_delay(2, 3)
                    await send_report(f"🤖 Капча решена! '{target_word}' → {btn_text}")
                    return True
                if target_word in btn_text.lower():
                    await btn.click()
                    logger.info(f"✅ Фруктовая капча (текст): нажал '{btn_text}'")
                    await human_delay(2, 3)
                    await send_report(f"🤖 Капча решена! Нажал: {btn_text}")
                    return True

        await send_report(
            f"⚠️ Капча: не нашёл '{target_word}' (эмодзи: {target_emoji})"
            f" среди {all_btns}"
        )
        return False

    await send_report(f"⚠️ Капча: неизвестный тип\n📄 {text[:150]}\n🔘 {all_btns}")
    return False


# --- Сбор ежедневки ---
async def collect_daily():
    try:
        logger.info("📅 Собираю ежедневку...")
        await send_report("📅 Начинаю сбор ежедневки...")

        msg = await open_bot()
        if not msg or not msg.buttons:
            await send_report("⚠️ Ежедневка: бот не ответил на /start")
            return False

        profile_clicked = False
        for row in msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in ["профил", "profile"]):
                    await btn.click()
                    logger.info(f"👤 Нажал: {btn.text}")
                    profile_clicked = True
                    await human_delay(3, 5)
                    break
            if profile_clicked:
                break

        if not profile_clicked:
            await send_report("⚠️ Ежедневка: кнопка Профиль не найдена")
            return False

        msg = await get_bot_response()
        if not msg or not msg.buttons:
            await send_report("⚠️ Ежедневка: нет ответа после Профиля")
            return False

        daily_clicked = False
        for row in msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in ["еже", "daily", "бонус"]):
                    await btn.click()
                    logger.info(f"📅 Нажал: {btn.text}")
                    daily_clicked = True
                    await human_delay(4, 6)
                    break
            if daily_clicked:
                break

        if not daily_clicked:
            await send_report("⚠️ Ежедневка: кнопка не найдена в профиле")
            return False

        msg = await get_bot_response()
        if not msg:
            await send_report("⚠️ Ежедневка: нет ответа с капчей")
            return False

        text = msg.text or ""

        if any(w in text.lower() for w in ["уже", "завтра", "получил"]):
            logger.info("ℹ️ Ежедневка уже забрана")
            await send_report("ℹ️ Ежедневка уже забрана сегодня")
            return True

        if not msg.buttons:
            await send_report(f"⚠️ Ежедневка: нет кнопок капчи\n📄 {text[:150]}")
            return False

        res = solve_math(text)
        if res is None:
            await send_report(f"⚠️ Капча: не нашёл математику\n📄 {text[:100]}")
            return False

        logger.info(f"🔢 Капча: {text[:60]} | Ответ: {res}")

        found_answer = False
        for row in msg.buttons:
            for btn in row:
                if btn.text.strip() == str(res):
                    await btn.click()
                    logger.info(f"✅ Нажал ответ капчи: {res}")
                    found_answer = True
                    await human_delay(2, 3)
                    resp = await get_bot_response()
                    resp_text = (resp.text or "") if resp else ""
                    await send_report(
                        f"📅 Ежедневка собрана!\n"
                        f"🔢 Капча: {res}\n"
                        f"📩 Бот: {resp_text[:150]}"
                    )
                    return True

        if not found_answer:
            all_btns = [b.text for row in msg.buttons for b in row]
            await send_report(f"⚠️ Капча: ответ {res} не найден среди {all_btns}")
            return False

    except Exception as e:
        logger.error(f"Ошибка ежедневки: {e}")
        await send_report(f"❌ Ошибка ежедневки: {e}")
        return False


async def wait_until_midnight():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=5, microsecond=0
    )
    wait_sec = (tomorrow - now).total_seconds()
    h, m = int(wait_sec // 3600), int((wait_sec % 3600) // 60)
    logger.info(f"⏳ Ежедневка через {h}ч {m}м (в 00:00 UTC+{TIMEZONE_OFFSET})")
    await asyncio.sleep(wait_sec)


async def daily_cycle():
    await collect_daily()
    while True:
        await wait_until_midnight()
        await collect_daily()


# --- Обработка одного задания ---
async def process_one_task(task_msg) -> tuple[str, str]:
    text = task_msg.text or ""

    if any(phrase in text.lower() for phrase in ALL_DONE_PHRASES):
        return 'all_done', text

    if task_msg.buttons:
        for row in task_msg.buttons:
            for btn in row:
                url = getattr(btn, 'url', None)
                btn_type = type(btn).__name__
                logger.info(f"🔘 '{btn.text}' | url: {url} | type: {btn_type}")

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
        logger.info("🎮 Мини-апп обнаружен — пробуем startapp")

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
                confirmed = False
                if fresh_msg and fresh_msg.buttons:
                    for row in fresh_msg.buttons:
                        for btn in row:
                            if any(w in btn.text.lower() for w in ["подтвер", "подтвердить запуск"]):
                                await btn.click()
                                logger.info(f"✅ Подтвердил запуск приложения: {btn.text}")
                                confirmed = True
                                await human_delay(2, 3)
                                break
                        if confirmed:
                            break
                if confirmed:
                    await send_report(f"🚀 Приложение запущено и подтверждено!\n📄 {text[:100]}")
                    return 'done', f'startapp ({startapp_url[:50]})'
                else:
                    await send_report(f"🚀 StartApp выполнен, но кнопка подтверждения не найдена\n📄 {text[:100]}")
                    return 'done', f'startapp без подтверждения'

        skipped = False
        if task_msg.buttons:
            for row in task_msg.buttons:
                for btn in row:
                    if "пропустить" in btn.text.lower():
                        await btn.click()
                        logger.info("⏭️ Нажал Пропустить (мини-апп)")
                        skipped = True
                        await human_delay(2, 3)
                        break
                if skipped:
                    break

        if skipped:
            await send_report(f"⏭️ Мини-апп пропущен\n📄 {text[:100]}")
            return 'skip', 'мини-апп (пропущен)'
        else:
            await send_report(
                f"⚠️ Мини-апп без кнопки Пропустить\n"
                f"📄 {text[:150]}\n"
                f"👆 Выполни вручную, жду следующей проверки"
            )
            return 'all_done', text

    action_done = False
    action_desc = ""

    if task_msg.buttons:
        for row in task_msg.buttons:
            for btn in row:
                url = getattr(btn, 'url', None)
                btn_text = btn.text.lower()
                if any(w in btn_text for w in ["подтвер", "пропустить", "меню", "назад", "главное"]):
                    continue
                if url:
                    tme_match = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                    if tme_match:
                        target = tme_match.group(1)
                        if target.lower().endswith('bot'):
                            await client.send_message(target, '/start')
                            action_desc = f"запустил бота @{target}"
                        else:
                            await safe_join(target)
                            action_desc = f"подписался на {target}"
                        action_done = True

    if not action_done:
        for target in re.findall(r't\.me/(\+?[a-zA-Z0-9_]+)', text):
            if target.lower().endswith('bot'):
                await client.send_message(target, '/start')
                action_desc = f"запустил бота @{target}"
            else:
                await safe_join(target)
                action_desc = f"подписался на {target}"
            action_done = True

        for username in re.findall(r'@([a-zA-Z0-9_]{5,})', text):
            if username.lower().endswith('bot'):
                await client.send_message(username, '/start')
                action_desc = f"запустил бота @{username}"
            else:
                await safe_join(username)
                action_desc = f"подписался на @{username}"
            action_done = True

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

    fresh_msg = await get_bot_response()
    confirmed = False
    if fresh_msg and fresh_msg.buttons:
        for row in fresh_msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in [
                    "подтвер", "выдать", "проверить", "claim", "check"
                ]):
                    await btn.click()
                    logger.info(f"✅ Подтвердил: {btn.text}")
                    confirmed = True
                    await human_delay(3, 5)
                    break
            if confirmed:
                break

    suffix = "" if confirmed else " (без подтверждения)"
    return 'done', action_desc + suffix


# --- Цикл заданий ---
async def tasks_cycle():
    while True:
        try:
            logger.info("📋 Запуск цикла заданий...")
            await send_report("📋 Начинаю выполнение заданий...")

            msg = await open_bot()
            if not msg or not msg.buttons:
                await send_report("⚠️ Бот не ответил на /start")
                await asyncio.sleep(10800)
                continue

            task_opened = False
            for row in msg.buttons:
                for btn in row:
                    if "задани" in btn.text.lower():
                        await btn.click()
                        logger.info(f"📋 Нажал: {btn.text}")
                        task_opened = True
                        break
                if task_opened:
                    break

            if not task_opened:
                logger.warning("⚠️ Кнопка заданий не найдена")
                await send_report("⚠️ Кнопка заданий не найдена")
                await asyncio.sleep(10800)
                continue

            completed = 0
            skipped = 0
            reports = []
            
            last_task_msg_id = None

            for i in range(30):
                logger.info(f"🔄 Задание #{i + 1}")

                task_msg = await get_bot_response()
                if not task_msg:
                    logger.warning("⚠️ Сообщение не получено")
                    await send_report("⚠️ Не получил сообщение от бота")
                    break

                if last_task_msg_id and task_msg.id == last_task_msg_id:
                    logger.warning(f"⚠️ Обнаружен повтор сообщения ID {task_msg.id}. Пытаюсь пропустить...")
                    
                    skipped_by_button = False
                    if task_msg.buttons:
                        for row in task_msg.buttons:
                            for btn in row:
                                if "пропустить" in btn.text.lower():
                                    await btn.click()
                                    logger.info("⏭️ Нажал Пропустить для выхода из цикла повтора")
                                    skipped_by_button = True
                                    await human_delay(3, 5)
                                    break
                            if skipped_by_button:
                                break
                    
                    if skipped_by_button:
                        skipped += 1
                        continue
                    else:
                        await send_report(f"⚠️ Задание зациклилось, а кнопки 'Пропустить' нет. Сплю до следующего цикла.")
                        break

                last_task_msg_id = task_msg.id

                result, desc = await process_one_task(task_msg)

                if result == 'all_done':
                    interval = random_task_interval()
                    h, m = interval // 3600, (interval % 3600) // 60
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
                await send_report(
                    f"⚠️ Достигнут лимит заданий (30)\n"
                    f"✅ {completed} | ⏭️ {skipped}\n"
                    f"⏳ Следующая через {h}ч {m}м"
                )
                await asyncio.sleep(interval)

        except Exception as e:
            logger.error(f"Ошибка заданий: {e}")
            await send_report(f"❌ Ошибка заданий: {e}")
            await asyncio.sleep(1800)
            

# --- Промокоды из канала ---
_activated_promos = set()


async def activate_promo(code: str):
    """Активирует промокод в боте"""
    global _activated_promos

    if code.lower() in _activated_promos:
        logger.info(f"ℹ️ Промокод уже активировали: {code}")
        return

    _activated_promos.add(code.lower())
    if len(_activated_promos) > 50:
        _activated_promos = set(list(_activated_promos)[-20:])

    logger.info(f"🔔 Активирую промокод: {code}")
    await send_report(f"🚀 Попытка активации кода: `{code}`...")

    msg = await open_bot()
    if not msg or not msg.buttons:
        await send_report("⚠️ Промокод: бот не ответил")
        return

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
        return

    msg = await get_bot_response()
    if not msg or not msg.buttons:
        await send_report("⚠️ Промокод: нет ответа после Профиля")
        return

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
        return

    await client.send_message(TARGET_BOT, code)
    await human_delay(3, 5)

    resp = await get_bot_response()
    if not resp:
        await send_report(f"⚠️ Промокод `{code}`: нет ответа от бота")
        return

    resp_text = resp.text or ""

    if resp.buttons:
        for r in resp.buttons:
            for b in r:
                url = getattr(b, 'url', None)
                b_text = b.text.lower()
                if url:
                    tme = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                    if tme and not tme.group(1).lower().endswith('bot'):
                        await safe_join(tme.group(1))
                if any(w in b_text for w in [
                    "подписал", "проверить", "готово", "я подписал", "check"
                ]):
                    await human_delay(4, 7)
                    await b.click()
                    logger.info(f"✅ Нажал: {b.text}")

    await send_report(
        f"✅ Промокод `{code}` отправлен на обработку!\n"
        f"📩 Ответ бота: {resp_text[:120]}"
    )


def find_promo_in_text(text: str):
    """
    Ультимативный неубиваемый поиск промокода.
    """
    if not text:
        return None

    lower_text = text.lower()
    if not any(w in lower_text for w in ["промо", "активац", "promo", "код"]):
        return None

    clean_text = text.replace("**", " ").replace("__", " ").replace("`", " ").replace("||", " ")
    for char in [".", ",", "!", "?", ":", ";", "(", ")", "[", "]", "-", "+", "/", "»", "«", "'", '"']:
        clean_text = clean_text.replace(char, " ")

    words = clean_text.split()
    for word in words:
        word_clean = word.strip()
        if 4 <= len(word_clean) <= 15:
            if re.match(r'^[a-zA-Z0-9]+$', word_clean):
                if word_clean.lower() not in IGNORED_WORDS:
                    return word_clean
    return None


if client:
    @client.on(events.NewMessage(chats=CHANNEL_SOURCE))
    @client.on(events.MessageEdited(chats=CHANNEL_SOURCE))
    async def handle_promo(event):
        raw = event.raw_text or ""
        logger.info(f"📨 Новый пост в канале: {raw[:100]}...")
        
        if any(w in raw.lower() for w in ["промо", "активац", "promo", "конкурс"]):
            try:
                alert_text = f"🔔 **Обнаружен пост с промокодом в канале!**\n\n📝 **Текст поста:**\n{raw}"
                await client.send_message(TARGET_USER, alert_text)
                logger.info("🚀 Уведомление о посте отправлено админу в ЛС")
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление в ЛС: {e}")

        code = find_promo_in_text(raw)
        if code:
            await activate_promo(code)


# --- Кликер (10 раз в день, 8:00-00:00, рандомно) + ОБРАБОТКА КАПЧИ ПОСЛЕ КЛИКА ---
async def clicker_cycle():
    CLICKER_TIMES_PER_DAY = 10
    CLICKER_START_HOUR = 8
    CLICKER_END_HOUR = 24

    while True:
        try:
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)

            if now.hour < CLICKER_START_HOUR:
                wake_at = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
                wait_sec = (wake_at - now).total_seconds()
                logger.info(f"🕐 Кликер спит до 08:00 ({int(wait_sec//3600)}ч {int((wait_sec%3600)//60)}м)")
                await asyncio.sleep(wait_sec)
                continue

            window_sec = (CLICKER_END_HOUR - CLICKER_START_HOUR) * 3600
            part_sec = window_sec // CLICKER_TIMES_PER_DAY
            day_start = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)

            click_times = []
            for i in range(CLICKER_TIMES_PER_DAY):
                part_start = day_start + datetime.timedelta(seconds=i * part_sec)
                rand_sec = random.randint(0, int(part_sec * 0.8))
                rand_extra = random.randint(0, 300)
                click_time = part_start + datetime.timedelta(seconds=rand_sec + rand_extra)
                midnight = day_start + datetime.timedelta(hours=16)
                if click_time > midnight:
                    click_time = midnight - datetime.timedelta(seconds=random.randint(60, 600))
                click_times.append(click_time)

            click_times.sort()
            logger.info(f"🎮 Кликер: запланировано {len(click_times)} кликов на сегодня")

            for i, click_time in enumerate(click_times):
                now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)

                if click_time <= now:
                    logger.info(f"🎮 Клик #{i+1} пропущен (время прошло)")
                    continue

                wait_sec = (click_time - now).total_seconds()
                h, m = int(wait_sec // 3600), int((wait_sec % 3600) // 60)
                logger.info(f"🎮 Клик #{i+1}/10 через {h}ч {m}м")
                await asyncio.sleep(wait_sec)

                try:
                    await client.send_message(TARGET_BOT, '/start')
                    await human_delay(3, 5)

                    msg = await get_bot_response()
                    if not msg or not msg.buttons:
                        logger.warning("🎮 Кликер: нет ответа от бота")
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
                                    logger.info(f"🎮 Нажал кликер: {btn.text}")
                                    clicker_clicked = True
                                    await human_delay(2, 4)
                                    break
                            if clicker_clicked:
                                break

                    if clicker_clicked:
                        resp = await get_bot_response()
                        
                        # --- ПРОВЕРКА НА КАПЧУ ПОСЛЕ НАЖАТИЯ КНОПКИ КЛИКЕРА ---
                        if resp and "проверка" in (resp.text or "").lower():
                            logger.info("🤖 Капча выскочила прямо после клика! Решаем...")
                            solved_after = await solve_captcha_if_needed(resp)
                            if solved_after:
                                await asyncio.sleep(4)
                                resp = await get_bot_response() # Берем финальный ответ после ввода капчи
                        
                        resp_text = (resp.text or "") if resp else ""
                        logger.info(f"🎮 Кликер #{i+1} собран! {resp_text[:80]}")
                        
                        if i + 1 < len(click_times):
                            next_click_time = click_times[i + 1]
                            now_after = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
                            next_wait = (next_click_time - now_after).total_seconds()
                            next_info = f"{int(next_wait // 3600)}ч {int((next_wait % 3600) // 60)}м" if next_wait > 0 else "совсем скоро"
                        else:
                            next_info = "завтра после 08:00"

                        await send_report(
                            f"🎮 **Кликер: Успешный сбор!**\n"
                            f"✅ Выполнен клик #{i+1}/10\n"
                            f"⏳ Следующий клик через: `{next_info}`\n\n"
                            f"📩 **Ответ Патрика:**\n{resp_text[:120]}"
                        )
                    else:
                        logger.warning(f"🎮 Кнопка кликера не найдена")

                except Exception as e:
                    logger.error(f"🎮 Ошибка клика #{i+1}: {e}")

            now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
            tomorrow_8 = (now + datetime.timedelta(days=1)).replace(
                hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0
            )
            wait_sec = (tomorrow_8 - now).total_seconds()
            logger.info(f"🎮 Кликер завершил день. Следующий цикл через {int(wait_sec//3600)}ч")
            await asyncio.sleep(wait_sec)

        except Exception as e:
            logger.error(f"Ошибка кликера: {e}")
            await asyncio.sleep(3600)


# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if client:
        await client.start()
        logger.info("✅ Telegram client запущен")
        await send_report(
            "🤖 Бот запущен!\n"
            "📅 Ежедневка: каждый день в 00:00\n"
            "📋 Задания: каждые ~3 часа\n"
            "🔔 Промокоды: слежу за каналом 24/7 (перехват 10000000000%)\n"
            "🎮 Кликер: 10 раз в день (8:00-00:00)"
        )

        async def delayed_tasks():
            await asyncio.sleep(30)
            await tasks_cycle()

        t1 = asyncio.create_task(daily_cycle())
        t2 = asyncio.create_task(delayed_tasks())
        t3 = asyncio.create_task(clicker_cycle())
        yield
        t1.cancel()
        t2.cancel()
        t3.cancel()
        await client.disconnect()
    else:
        logger.warning("⚠️ SESSION_STRING не задан")
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok"}