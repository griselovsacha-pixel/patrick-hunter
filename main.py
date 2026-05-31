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
TARGET_USER = 5458870502
TIMEZONE_OFFSET = 3  # UTC+3 Москва

PROMO_REGEX = r'(?i)активаций[:\s]+([a-zA-Z0-9]{4,})'

ALL_DONE_PHRASES = [
    "выполнил все задания",
    "все задания выполнены",
    "приходи через",
    "новых заданий нет",
    "заданий больше нет",
    "респект",
    "молодец",
]

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


async def get_fresh_message(max_age: int = 60):
    await human_delay(3, 5)
    now = datetime.datetime.now(datetime.timezone.utc)
    async for msg in client.iter_messages(TARGET_BOT, limit=5):
        age = (now - msg.date).total_seconds()
        if age < max_age and (msg.text or msg.buttons):
            return msg
    async for msg in client.iter_messages(TARGET_BOT, limit=1):
        return msg
    return None


async def open_bot():
    await client.send_message(TARGET_BOT, '/start')
    await human_delay(3, 5)


# --- Определить WebApp кнопку ---
def is_webapp_button(btn) -> bool:
    btn_text = btn.text.lower()
    if any(w in btn_text for w in [
        "запустить приложение",
        "открыть приложение",
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


# --- Запуск другого бота ---
async def start_other_bot(bot_username: str):
    try:
        bot = await client.get_entity(bot_username)
        await client(StartBotRequest(bot=bot, peer=bot, start_param=''))
        logger.info(f"✅ Запустил бота: @{bot_username}")
        await human_delay(3, 5)
    except Exception as e:
        logger.error(f"Ошибка запуска бота {bot_username}: {e}")


# --- Сбор ежедневки ---
async def collect_daily():
    try:
        logger.info("📅 Собираю ежедневку...")
        await send_report("📅 Начинаю сбор ежедневки...")

        await open_bot()

        # Нажать "Профиль"
        profile_clicked = False
        async for msg in client.iter_messages(TARGET_BOT, limit=5):
            if msg.buttons:
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
            if profile_clicked:
                break

        if not profile_clicked:
            logger.warning("⚠️ Кнопка Профиль не найдена")
            await send_report("⚠️ Ежедневка: кнопка Профиль не найдена")
            return False

        # Нажать "Ежедневка"
        daily_clicked = False
        async for msg in client.iter_messages(TARGET_BOT, limit=5):
            if msg.buttons:
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
            if daily_clicked:
                break

        if not daily_clicked:
            logger.warning("⚠️ Кнопка Ежедневка не найдена")
            await send_report("⚠️ Ежедневка: кнопка не найдена")
            return False

        # Решаем капчу
        await human_delay(2, 3)
        async for msg in client.iter_messages(TARGET_BOT, limit=3):
            if not msg.buttons:
                continue

            text = msg.text or ""

            if any(w in text.lower() for w in ["уже", "завтра", "получил"]):
                logger.info("ℹ️ Ежедневка уже забрана")
                await send_report("ℹ️ Ежедневка уже забрана сегодня")
                return True

            res = solve_math(text)
            if res is None:
                logger.warning(f"⚠️ Нет математики в: {text[:100]}")
                continue

            logger.info(f"🔢 Капча: {text[:60]} | Ответ: {res}")

            found_answer = False
            for row in msg.buttons:
                for btn in row:
                    if btn.text.strip() == str(res):
                        await btn.click()
                        logger.info(f"✅ Нажал ответ: {res}")
                        found_answer = True
                        await human_delay(2, 3)
                        async for resp in client.iter_messages(TARGET_BOT, limit=1):
                            resp_text = resp.text or ""
                            await send_report(
                                f"📅 Ежедневка собрана!\n"
                                f"🔢 Капча решена: {res}\n"
                                f"📩 Бот: {resp_text[:150]}"
                            )
                        return True

            if not found_answer:
                all_btns = [b.text for row in msg.buttons for b in row]
                logger.warning(f"⚠️ Ответ {res} не найден: {all_btns}")
                await send_report(f"⚠️ Капча: ответ {res} не найден среди {all_btns}")
                return False

    except Exception as e:
        logger.error(f"Ошибка ежедневки: {e}")
        await send_report(f"❌ Ошибка ежедневки: {e}")
        return False


# --- Ждать до 00:00 ---
async def wait_until_midnight():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=5, microsecond=0
    )
    wait_sec = (tomorrow - now).total_seconds()
    h, m = int(wait_sec // 3600), int((wait_sec % 3600) // 60)
    logger.info(f"⏳ Ежедневка через {h}ч {m}м (в 00:00 UTC+{TIMEZONE_OFFSET})")
    await asyncio.sleep(wait_sec)


# --- Цикл ежедневки ---
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

    # WebApp / мини-апп
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
        logger.info("🎮 Мини-апп — пробуем подтвердить без открытия")
        await human_delay(8, 15)

        confirmed = False
        if task_msg.buttons:
            for row in task_msg.buttons:
                for btn in row:
                    if any(w in btn.text.lower() for w in ["подтвер", "подтвердить запуск"]):
                        await btn.click()
                        logger.info(f"✅ Подтвердил мини-апп: {btn.text}")
                        confirmed = True
                        await human_delay(2, 3)
                        break
                if confirmed:
                    break

        if confirmed:
            await send_report(f"🎮 Мини-апп подтверждён!\n📄 {text[:100]}")
            return 'done', 'мини-апп (подтверждён)'
        else:
            await send_report(
                f"⚠️ Мини-апп — не смог подтвердить\n"
                f"📄 {text[:150]}\n"
                f"👆 Выполни вручную!"
            )
            if task_msg.buttons:
                for row in task_msg.buttons:
                    for btn in row:
                        if "пропустить" in btn.text.lower():
                            await btn.click()
                            await human_delay(2, 3)
                            break
            return 'skip', 'мини-апп (пропущен)'

    action_done = False
    action_desc = ""

    # Кнопки со ссылками
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
                            await start_other_bot(target)
                            action_desc = f"запустил бота @{target}"
                        else:
                            await safe_join(target)
                            action_desc = f"подписался на {target}"
                        action_done = True

    # Ссылки в тексте
    if not action_done:
        for target in re.findall(r't\.me/(\+?[a-zA-Z0-9_]+)', text):
            if target.lower().endswith('bot'):
                await start_other_bot(target)
                action_desc = f"запустил бота @{target}"
            else:
                await safe_join(target)
                action_desc = f"подписался на {target}"
            action_done = True

        for username in re.findall(r'@([a-zA-Z0-9_]{5,})', text):
            if username.lower().endswith('bot'):
                await start_other_bot(username)
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

    fresh_msg = await get_fresh_message()
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

            await open_bot()

            task_opened = False
            async for msg in client.iter_messages(TARGET_BOT, limit=5):
                if msg.buttons:
                    for row in msg.buttons:
                        for btn in row:
                            if "задани" in btn.text.lower():
                                await btn.click()
                                logger.info(f"📋 Нажал: {btn.text}")
                                task_opened = True
                                break
                        if task_opened:
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

            for i in range(30):
                logger.info(f"🔄 Задание #{i + 1}")
                task_msg = await get_fresh_message()
                if not task_msg:
                    logger.warning("⚠️ Сообщение не получено")
                    await send_report("⚠️ Не получил сообщение от бота")
                    break

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
if client:
    @client.on(events.NewMessage(chats=CHANNEL_SOURCE))
    async def handle_promo(event):
        logger.info(f"📨 Канал: {event.raw_text[:150]}")
        match = re.search(PROMO_REGEX, event.raw_text)
        if match:
            code = match.group(1)
            logger.info(f"🔔 Промокод: {code}")
            await send_report(f"🔔 Найден промокод: `{code}`\nАктивирую...")

            await open_bot()

            profile_clicked = False
            async for msg in client.iter_messages(TARGET_BOT, limit=5):
                if msg.buttons:
                    for row in msg.buttons:
                        for btn in row:
                            if any(w in btn.text.lower() for w in ["профил", "profile"]):
                                await btn.click()
                                profile_clicked = True
                                await human_delay(2, 3)
                                break
                        if profile_clicked:
                            break
                if profile_clicked:
                    break

            if not profile_clicked:
                await send_report("⚠️ Промокод: кнопка Профиль не найдена")
                return

            activated = False
            async for msg in client.iter_messages(TARGET_BOT, limit=5):
                if msg.buttons:
                    for row in msg.buttons:
                        for btn in row:
                            if "промо" in btn.text.lower():
                                await btn.click()
                                await human_delay(2, 3)
                                await client.send_message(TARGET_BOT, code)
                                await human_delay(3, 5)

                                async for resp in client.iter_messages(TARGET_BOT, limit=1):
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
                                                    "подписал", "проверить", "готово",
                                                    "я подписал", "check"
                                                ]):
                                                    await human_delay(4, 7)
                                                    await b.click()
                                                    logger.info(f"✅ Нажал: {b.text}")

                                    activated = True
                                    await send_report(
                                        f"✅ Промокод `{code}` активирован!\n"
                                        f"📩 Бот: {resp_text[:100]}"
                                    )
                                return

            if not activated:
                await send_report(f"⚠️ Не нашёл кнопку промокода для: `{code}`")


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
            "🔔 Промокоды: слежу за каналом 24/7"
        )
        t1 = asyncio.create_task(daily_cycle())
        t2 = asyncio.create_task(tasks_cycle())
        yield
        t1.cancel()
        t2.cancel()
        await client.disconnect()
    else:
        logger.warning("⚠️ SESSION_STRING не задан")
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok"}
