import re
import random
import asyncio
import logging
from fastapi import FastAPI
from telethon import TelegramClient, events

# Логирование для отслеживания деплоя в консоли Render
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
API_ID = 31206776          
API_HASH = '7ca69453a014d6a87d67afe4d9df8284'

TARGET_BOT = 'patrickstarsrobot'    # Куда шлем промокод
CHANNEL_SOURCE = 'patrickstarsfarm' # Откуда берем код

# Улучшенная регулярка: ищет латинские буквы/цифры [a-zA-Z0-9] после триггеров
PROMO_REGEX = r'(?:активаций|промокод|промо|дейли):\s*([a-zA-Z0-9]+)'

# --- ВЕБ-СЕРВЕР ДЛЯ АНТИ-СНА ---
app = FastAPI()

@app.get("/")
def read_root():
    return {
        "status": "active",
        "bot_worker": "running",
        "security_level": "maximum"
    }

# --- ИНИЦИАЛИЗАЦИЯ КЛИЕНТА ТЕЛЕГРАМ ---
# ПОДСТРОИЛ ПОД ТВОЙ ФАЙЛ НА GITHUB СО СКОБОЧКАМИ
client = TelegramClient('patrick_hunter_session (1)', API_ID, API_HASH)

@client.on(events.NewMessage(chats=CHANNEL_SOURCE))
async def handle_new_promo_post(event):
    text = event.raw_text
    if not text:
        return
        
    text_lower = text.lower()
    
    # Проверяем, целевой ли это пост
    if any(trigger in text_lower for trigger in ["активаций", "промокод", "дейли", "промо"]):
        # Очищаем текст от лишних спецсимволов, которые могут мешать регулярке
        clean_text = re.sub(r'[\*_`~]', '', text_lower)
        match = re.search(PROMO_REGEX, clean_text)
        
        if match:
            promo_code = match.group(1).strip()
            logger.info(f"🚀 [Hamer British] Перехвачен новый промокод: {promo_code}")
            
            # Готовим лог-отчет для отправки в Избранное
            report = f"🔔 **ПЕРЕХВАТ ПРОМОКОДА** 🔔\n\n"
            report += f"🔑 Найденный код: `{promo_code}`\n"
            report += f"🛡️ Имитирую действия человека (рандомный обход антифрода)...\n\n"
            
            try:
                # Шаг 1: Будим бота Патрика
                logger.info("Будим бота командой /start...")
                await client.send_message(TARGET_BOT, '/start')
                await asyncio.sleep(random.uniform(2.0, 2.6)) # Рандомная пауза
                
                # Шаг 2: Переходим в Профиль
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
                
                await asyncio.sleep(random.uniform(2.1, 2.7)) # Рандомная пауза
                
                # Шаг 3: Нажимаем кнопку ввода промокода
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
                
                # Если инлайн-кнопка залипла, шлем текстовый триггер для подстраховки
                if not promo_clicked:
                    await client.send_message(TARGET_BOT, '🎫 Промокод')
                
                await asyncio.sleep(random.uniform(1.8, 2.3)) # Финальная имитация размышления
                
                # Шаг 4: Отправляем промокод в текстовое поле
                logger.info(f"Отправляем секретный код: {promo_code}")
                await client.send_message(TARGET_BOT, promo_code)
                report += f"🚀 Код `{promo_code}` отправлен на активацию!\n"
                
                # Шаг 5: Перехватываем ответ от Патрика
                await asyncio.sleep(3.5)
                async for message in client.iter_messages(TARGET_BOT, limit=1):
                    report += f"\n💬 **Результат от бота:**\n_{message.text}_"
                
            except Exception as e:
                logger.error(f"Ошибка в цепочке автоматизации: {e}")
                report += f"❌ **Сбой:** {str(e)}"
            
            # Отправка отчета тебе в Избранное (Saved Messages)
            await client.send_message('me', report)
            logger.info("Отчет успешно отправлен в ЛС владельцу.")

# --- УПРАВЛЕНИЕ ЗАПУСКОМ ---
@app.on_event("startup")
async def startup_event():
    await client.start()
    logger.info("🤖 Юзербот успешно авторизован по файлу сессии и запущен!")

@app.on_event("shutdown")
async def shutdown_event():
    await client.disconnect()
    logger.info("Юзербот отключен.")
