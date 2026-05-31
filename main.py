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

                                # Решение капчи
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
                                                        await client.send_message(
                                                            TARGET_USER,
                                                            f"✅ Ежедневка забрана! Капча: {res}"
                                                        )
                                                        break
                                break
                        if found:
                            break

        except Exception as e:
            logger.error(f"Ошибка ежедневки: {e}")

        logger.info("⏳ Следующая ежедневка через 24 часа")
        await asyncio.sleep(86400)


# --- Задания (раз в 3 часа) ---
async def tasks_cycle():
    while True:
        try:
            logger.info("📋 Запуск цикла заданий...")

            # 1. Нажать /start
            await client.send_message(TARGET_BOT, '/start')
            await human_delay(3, 5)

            # 2. Найти и нажать кнопку "Задания"
            task_opened = False
            async for msg in client.iter_messages(TARGET_BOT, limit=5):
                if msg.buttons:
                    for row in msg.buttons:
                        for btn in row:
                            if "задани" in btn.text.lower():
                                await btn.click()
                                logger.info(f"📋 Нажал: {btn.text}")
                                await human_delay(3, 5)
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

            await human_delay(2, 4)

            # 3. Читаем задание
            async for msg in client.iter_messages(TARGET_BOT, limit=3):
                if not msg.text and not msg.buttons:
                    continue

                text = msg.text or ""
                logger.info(f"📄 Задание: {text[:100]}")

                # 4. Определяем тип и выполняем
                action_done = False

                # Сначала смотрим кнопки со ссылками
                if msg.buttons:
                    for row in msg.buttons:
                        for btn in row:
                            if not hasattr(btn, 'url') or not btn.url:
                                continue

                            tme_match = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', btn.url)
                            if not tme_match:
                                continue

                            target = tme_match.group(1)

                            if target.lower().endswith('bot'):
                                logger.info(f"🤖 Запускаю бота: @{target}")
                                await start_other_bot(target)
                            else:
                                logger.info(f"📢 Подписываюсь: {target}")
                                await safe_join(target)

                            action_done = True

                # Если кнопок со ссылками не было — парсим текст
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

                if not action_done:
                    logger.warning("⚠️ Тип задания не определён")
                    break

                # 5. Пауза перед подтверждением
                await human_delay(6, 10)

                # 6. Нажимаем кнопку подтверждения
                confirmed = False
                async for fresh_msg in client.iter_messages(TARGET_BOT, limit=3):
                    if fresh_msg.buttons:
                        for row in fresh_msg.buttons:
                            for btn in row:
                                if any(w in btn.text.lower() for w in [
                                    "подтвер", "выдать", "проверить", "claim", "check", "✅"
                                ]):
                                    await btn.click()
                                    logger.info(f"✅ Подтвердил: {btn.text}")
                                    confirmed = True
                                    await human_delay(2, 3)
                                    break
                            if confirmed:
                                break
                    if confirmed:
                        break

                # 7. Уведомление
                if confirmed:
                    await client.send_message(TARGET_USER, f"✅ Задание выполнено!\n📄 {text[:150]}")
                else:
                    await client.send_message(TARGET_USER, f"⚠️ Выполнено, кнопка подтверждения не найдена\n📄 {text[:150]}")

                break  # одно задание за цикл

        except Exception as e:
            logger.error(f"Ошибка заданий: {e}")

        logger.info("⏳ Следующее задание через 3 часа")
        await asyncio.sleep(10800)


# --- Промокоды из канала ---
@client.on(events.NewMessage(chats=CHANNEL_SOURCE))
async def handle_promo(event):
    match = re.search(PROMO_REGEX, event.raw_text)
    if match:
        code = match.group(1)
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
