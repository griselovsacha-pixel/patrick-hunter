import re
import os
import sys
import random
import asyncio
import logging
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
PROMO_REGEX = r'(?i)(?:активаций|промокод|промо|дейли):\s*([a-zA-Z0-9]+)'

ALL_DONE_PHRASES = [
    "выполнил все задания",
    "все задания выполнены",
    "приходи через",
    "новых заданий нет",
    "заданий больше нет",
    "респект",
]

SESSION_STRING = os.getenv("TELEGRAM_SESSION")
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) if SESSION_STRING else None


# --- Утилиты ---
def solve_math(text: str):
    expression = re.sub(r'[^\d\+\-\*\/\(\)\s]', '', text)
    try:
        return int(eval(expression))
    except Exception:
        return None


async def human_delay(min_sec: float = 2.0, max_sec: float = 4.0):
    await asyncio.sleep(random.uniform(min_sec, max_sec))


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


# --- Получить свежее сообщение от бота ---
async def get_fresh_message():
    await human_delay(3, 5)
    async for msg in client.iter_messages(TARGET_BOT, limit=2):
        if msg.text or msg.buttons:
            return msg
    return None


# --- Ежедневка (раз в 24 часа) ---
async def daily_cycle():
    while True:
        try:
            logger.info("📅 Запуск ежедневки...")
            await client.send_message(TARGET_BOT, '/start')
            await human_delay(3, 5)

            found = False
            async for msg in client.iter_messages(TARGET_BOT, limit=5):
                if msg.buttons and not found:
                    for row in msg.buttons:
                        for btn in row:
                            if any(w in btn.text.lower() for w in ["еже", "daily"]):
                                await btn.click()
                                logger.info(f"📅 Нажал кнопку ежедневки: {btn.text}")
                                await human_delay(4, 6)
                                found = True

                                async for resp in client.iter_messages(TARGET_BOT, limit=1):
                                    if resp.text and any(op in resp.text for op in ["+", "-", "*", "/"]):
                                        res = solve_math(resp.text)
                                        if res is None:
                                            logger.warning("⚠️ Не удалось решить капчу")
                                            break
                                        if resp.buttons:
                                            for r in resp.buttons:
                                                for b in r:
                                                    if str(res) == b.text.strip():
                                                        await b.click()
                                                        logger.info(f"✅ Капча решена: {res}")
                                                        await client.send_message(TARGET_USER,
                                                            f"✅ Ежедневка забрана! Капча: {res}")
                                                        break
                                break
                        if found:
                            break

        except Exception as e:
            logger.error(f"Ошибка ежедневки: {e}")
            await client.send_message(TARGET_USER, f"❌ Ошибка ежедневки: {e}")

        logger.info("⏳ Следующая ежедневка через 24 часа")
        await asyncio.sleep(86400)


# --- Обработка одного задания ---
async def process_one_task(task_msg) -> str:
    """
    Возвращает:
      'done'     — задание выполнено
      'skip'     — пропущено (не определили тип)
      'all_done' — все задания выполнены
    """
    text = task_msg.text or ""

    # Проверяем — все задания выполнены?
    if any(phrase in text.lower() for phrase in ALL_DONE_PHRASES):
        return 'all_done'

    # Логируем кнопки для диагностики
    if task_msg.buttons:
        for row in task_msg.buttons:
            for btn in row:
                url = getattr(btn, 'url', None)
                logger.info(f"🔘 Кнопка: '{btn.text}' | url: {url}")

    action_done = False

    # Ищем кнопки со ссылками (канал или бот)
    if task_msg.buttons:
        for row in task_msg.buttons:
            for btn in row:
                url = getattr(btn, 'url', None)
                btn_text = btn.text.lower()

                # Пропускаем навигационные кнопки
                if any(w in btn_text for w in ["подтвер", "пропустить", "меню", "назад", "главное"]):
                    continue

                if url:
                    tme_match = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                    if tme_match:
                        target = tme_match.group(1)
                        if target.lower().endswith('bot'):
                            logger.info(f"🤖 Запускаю бота: @{target}")
                            await start_other_bot(target)
                        else:
                            logger.info(f"📢 Подписываюсь: {target}")
                            await safe_join(target)
                        action_done = True

    # Ищем в тексте если кнопок со ссылками не было
    if not action_done:
        links = re.findall(r't\.me/(\+?[a-zA-Z0-9_]+)', text)
        for target in links:
            if target.lower().endswith('bot'):
                await start_other_bot(target)
            else:
                await safe_join(target)
            action_done = True

        usernames = re.findall(r'@([a-zA-Z0-9_]{5,})', text)
        for username in usernames:
            if username.lower().endswith('bot'):
                await start_other_bot(username)
            else:
                await safe_join(username)
            action_done = True

    # Тип не определён — пропускаем
    if not action_done:
        logger.warning(f"⚠️ Тип задания не определён, пропускаю\nТекст: {text[:200]}")
        if task_msg.buttons:
            for row in task_msg.buttons:
                for btn in row:
                    if "пропустить" in btn.text.lower():
                        await btn.click()
                        logger.info("⏭️ Нажал Пропустить")
                        await human_delay(2, 3)
                        return 'skip'
        return 'skip'

    # Пауза перед подтверждением — имитируем живого пользователя
    await human_delay(6, 10)

    # Нажимаем кнопку подтверждения
    fresh_msg = await get_fresh_message()
    if fresh_msg and fresh_msg.buttons:
        for row in fresh_msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in [
                    "подтвер", "выдать", "проверить", "claim", "check"
                ]):
                    await btn.click()
                    logger.info(f"✅ Подтвердил: {btn.text}")
                    await human_delay(3, 5)
                    return 'done'

    logger.warning("⚠️ Кнопка подтверждения не найдена")
    return 'done'


# --- Цикл заданий (крутим пока все не выполнены, потом 3 часа) ---
async def tasks_cycle():
    while True:
        try:
            logger.info("📋 Запуск цикла заданий...")

            # 1. Открываем бота
            await client.send_message(TARGET_BOT, '/start')
            await human_delay(3, 5)

            # 2. Нажимаем кнопку "Задания"
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
                await asyncio.sleep(10800)
                continue

            # 3. Крутим задания в цикле
            completed = 0
            skipped = 0
            max_tasks = 30  # защита от бесконечного цикла

            for i in range(max_tasks):
                logger.info(f"🔄 Задание #{i + 1}")

                task_msg = await get_fresh_message()
                if not task_msg:
                    logger.warning("⚠️ Не получили сообщение от бота")
                    break

                result = await process_one_task(task_msg)

                if result == 'all_done':
                    logger.info(f"🎉 Все задания выполнены! ✅{completed} ⏭️{skipped}")
                    await client.send_message(TARGET_USER,
                        f"🎉 Все задания выполнены!\n"
                        f"✅ Выполнено: {completed}\n"
                        f"⏭️ Пропущено: {skipped}\n"
                        f"⏳ Следующая проверка через 3 часа"
                    )
                    break

                elif result == 'done':
                    completed += 1
                    logger.info(f"✅ Задание выполнено ({completed} всего)")

                elif result == 'skip':
                    skipped += 1
                    logger.info(f"⏭️ Пропущено ({skipped} всего)")

            else:
                logger.warning(f"⚠️ Достигнут лимит {max_tasks} заданий")
                await client.send_message(TARGET_USER,
                    f"⚠️ Достигнут лимит заданий ({max_tasks})\n"
                    f"✅ Выполнено: {completed} | ⏭️ Пропущено: {skipped}"
                )

        except Exception as e:
            logger.error(f"Ошибка заданий: {e}")
            await client.send_message(TARGET_USER, f"❌ Ошибка в заданиях: {e}")

        logger.info("⏳ Следующая проверка заданий через 3 часа")
        await asyncio.sleep(10800)


# --- Промокоды из канала ---
@client.on(events.NewMessage(chats=CHANNEL_SOURCE))
async def handle_promo(event):
    logger.info(f"📨 Сообщение в канале: {event.raw_text[:100]}")
    match = re.search(PROMO_REGEX, event.raw_text)
    if match:
        code = match.group(1)
        logger.info(f"🔔 Найден промокод: {code}")
        await client.send_message(TARGET_USER, f"🔔 Найден промокод: `{code}`. Активирую...")

        await client.send_message(TARGET_BOT, '/start')
        await human_delay(2, 3)

        async for msg in client.iter_messages(TARGET_BOT, limit=2):
            if msg.buttons:
                for row in msg.buttons:
                    for btn in row:
                        if "промо" in btn.text.lower():
                            await btn.click()
                            await human_delay(2, 3)
                            await client.send_message(TARGET_BOT, code)
                            logger.info(f"✅ Промокод активирован: {code}")
                            await client.send_message(TARGET_USER, f"✅ Промокод `{code}` активирован!")
                            return


# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if client:
        await client.start()
        logger.info("✅ Telegram client запущен")
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
