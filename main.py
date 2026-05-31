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
TARGET_USER = '@sanzezzz'
# Исправлено: ищем оригинальный текст, не lowercased
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


# --- Фарм ежедневки (раз в 24 часа) ---
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
                                await human_delay(4, 6)
                                found = True

                                # Решение капчи
                                async for resp in client.iter_messages(TARGET_BOT, limit=1):
                                    # Исправлено: проверка на None перед работой с текстом
                                    if resp.text and any(op in resp.text for op in ["+", "-", "*", "/"]):
                                        res = solve_math(resp.text)
                                        if res is None:
                                            logger.warning("Не удалось решить капчу")
                                            break
                                        if resp.buttons:
                                            for r in resp.buttons:
                                                for b in r:
                                                    if str(res) == b.text.strip():
                                                        await b.click()
                                                        await client.send_message(
                                                            TARGET_USER,
                                                            f"✅ Ежедневка успешно забрана! Результат капчи: {res}"
                                                        )
                                                        break
                                break  # выходим из row после нажатия кнопки
                        if found:
                            break  # выходим из перебора сообщений

        except Exception as e:
            logger.error(f"Ошибка ежедневки: {e}")

        await asyncio.sleep(86400)  # 24 часа


# --- Мониторинг заданий (раз в час) ---
async def tasks_cycle():
    while True:
        try:
            await client.send_message(TARGET_BOT, '/start')
            await human_delay(2, 3)
            await client.send_message(TARGET_BOT, '📋 Задания')
            await human_delay(3, 5)

            async for msg in client.iter_messages(TARGET_BOT, limit=1):
                # Исправлено: защита от None в msg.text
                if msg.text and "новое задание" in msg.text.lower():
                    await client.send_message(TARGET_USER, f"✨ Новое задание найдено:\n{msg.text}")

        except Exception as e:
            logger.error(f"Ошибка заданий: {e}")

        await asyncio.sleep(random.randint(3000, 4200))  # 50-70 минут


# --- Обработка промокодов ---
@client.on(events.NewMessage(chats=CHANNEL_SOURCE))
async def handle_promo(event):
    # Исправлено: ищем в оригинальном тексте (не .lower()), флаг (?i) в regex
    match = re.search(PROMO_REGEX, event.raw_text)
    if match:
        code = match.group(1)  # сохраняет оригинальный регистр
        await client.send_message(TARGET_USER, f"🔔 Код: `{code}`. Активирую...")
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
                            return


# --- Lifespan (замена устаревшего on_event) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if client:
        await client.start()
        logger.info("Telegram client запущен")
        t1 = asyncio.create_task(daily_cycle())
        t2 = asyncio.create_task(tasks_cycle())
        yield
        t1.cancel()
        t2.cancel()
        await client.disconnect()
    else:
        logger.warning("SESSION_STRING не задан — клиент не запущен")
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}
