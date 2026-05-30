import re
import os
import sys
import random
import asyncio
import logging
from fastapi import FastAPI
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# Настройка логирования в консоль Render
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

API_ID = 31206776          
API_HASH = '7ca69453a014d6a87d67afe4d9df8284'

TARGET_BOT = 'patrickstarsrobot'    
CHANNEL_SOURCE = 'patrickstarsfarm' 
PROMO_REGEX = r'(?:активаций|промокод|промо|дейли):\s*([a-zA-Z0-9]+)'

# Читаем строку сессии из переменных окружения Render
SESSION_STRING = os.getenv("TELEGRAM_SESSION")

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "active", "bot_worker": "running"}

if not SESSION_STRING:
    logger.error("❌ КРИТИЧЕСКАЯ ОШИБКА: Переменная TELEGRAM_SESSION не найдена в настройках Render!")
    client = None
else:
    # Запускаем чистую сессию прямо из строки текста в памяти
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

if client:
    @client.on(events.NewMessage(chats=CHANNEL_SOURCE))
    async def handle_new_promo_post(event):
        text = event.raw_text
        if not text:
            return
        text_lower = text.lower()
        
        if any(trigger in text_lower for trigger in ["активаций", "промокод", "дейли", "промо"]):
            clean_text = re.sub(r'[\*_`~]', '', text_lower)
            match = re.search(PROMO_REGEX, clean_text)
            
            if match:
                promo_code = match.group(1).strip()
                logger.info(f"🚀 Перехвачен новый промокод: {promo_code}")
                
                report = f"🔔 **ПЕРЕХВАТ ПРОМОКОДА** 🔔\n\n🔑 Код: `{promo_code}`\n🛡️ Имитирую действия человека...\n\n"
                
                try:
                    logger.info("Будим бота командой /start...")
                    await client.send_message(TARGET_BOT, '/start')
                    await asyncio.sleep(random.uniform(2.0, 2.6))
                    
                    logger.info("Открываем меню [Профиль]...")
                    await client.send_message(TARGET_BOT, '👤 Профиль')
                    
                    profile_clicked = False
                    async for message in client.iter_messages(TARGET_BOT, limit=3):
                        if message.buttons:
                            for row in message.buttons:
                                for button in row:
                                    if "profile" in str(button.data) or "Профиль" in button.text:
                                        await button.click()
                                        report += "✅ Успешный переход в [Профиль]\n"
                                        profile_clicked = True
                                        break
                            if profile_clicked: break
                    
                    await asyncio.sleep(random.uniform(2.1, 2.7))
                    
                    logger.info("Запрашиваем поле ввода [Промокод]...")
                    promo_clicked = False
                    async for message in client.iter_messages(TARGET_BOT, limit=3):
                        if message.buttons:
                            for row in message.buttons:
                                for button in row:
                                    if "promo" in str(button.data) or "Промокод" in button.text:
                                        await button.click()
                                        report += "✅ Активировано поле [Промокод]\n"
                                        promo_clicked = True
                                        break
                            if promo_clicked: break
                    
                    if not promo_clicked:
                        await client.send_message(TARGET_BOT, '🎫 Промокод')
                    
                    await asyncio.sleep(random.uniform(1.8, 2.3))
                    
                    logger.info(f"Отправляем код: {promo_code}")
                    await client.send_message(TARGET_BOT, promo_code)
                    report += f"🚀 Код `{promo_code}` отправлен на активацию!\n"
                    
                    await asyncio.sleep(3.5)
                    async for message in client.iter_messages(TARGET_BOT, limit=1):
                        report += f"\n💬 **Результат от бота:**\n_{message.text}_"
                    
                except Exception as e:
                    logger.error(f"Ошибка автоматизации: {e}")
                    report += f"❌ **Сбой:** {str(e)}"
                
                await client.send_message('me', report)

@app.on_event("startup")
async def startup_event():
    if client:
        logger.info("⏳ Авторизация юзербота через StringSession...")
        try:
            await client.start()
            logger.info("🤖 ЮЗЕРБОТ УСПЕШНО ЗАПУЩЕН И РАБОТАЕТ ИЗ ПАМЯТИ!")
        except Exception as e:
            logger.error(f"💥 Ошибка старта Telethon: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    if client:
        await client.disconnect()

