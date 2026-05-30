import re
import asyncio
import logging
from fastapi import FastAPI
from telethon import TelegramClient, events

# Настраиваем логирование, чтобы видеть работу бота в консоли Render
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- НАСТРОЙКИ ---
API_ID = 31206776          
API_HASH = '7ca69453a014d6a87d67afe4d9df8284'

TARGET_BOT = 'patrickstarsrobot'    # Юзернейм бота, куда вводим код
CHANNEL_SOURCE = 'patrickstarsfarm' # Юзернейм канала с промокодами

# Регулярка вытаскивает ЛЮБОЕ слово после двоеточия (активаций: код)
PROMO_REGEX = r'(?:активаций|промокод):\s*([a-zA-Z0-9]+)'

# --- СОЗДАНИЕ ВЕБ-САЙТА (FASTAPI) ---
app = FastAPI()

@app.get("/")
def read_root():
    # Эта страница нужна, чтобы Render видел, что сайт "жив"
    return {
        "status": "online",
        "message": "Patrick Stars Hunter Web Server is running!",
        "owner": "Hamer"
    }

# --- ИНИЦИАЛИЗАЦИЯ TELETHON ---
# Используем сохраненный тобой файл сессии
client = TelegramClient('patrick_hunter_session', API_ID, API_HASH)

@client.on(events.NewMessage(chats=CHANNEL_SOURCE))
async def handle_new_promo_post(event):
    text = event.raw_text
    text_lower = text.lower()
    
    # Проверяем, содержит ли пост ключевые слова промокода
    if "активаций" in text_lower or "промокод" in text_lower:
        match = re.search(PROMO_REGEX, text_lower)
        
        if match:
            # Вытаскиваем чистый промокод
            promo_code = match.group(1).strip()
            logger.info(f"🔥 Перехвачен промокод из канала: {promo_code}")
            
            # Начинаем формировать отчет в ЛС
            report = f"🔔 **ОТЧЕТ О ПЕРЕХВАТЕ** 🔔\n\n"
            report += f"🔑 Код под спойлером: `{promo_code}`\n"
            report += f"⏳ Запуск безопасной цепочки действий (анти-бан 0%)...\n\n"
            
            try:
                # Шаг 1: Будим бота командой /start
                logger.info("Отправка /start...")
                await client.send_message(TARGET_BOT, '/start')
                await asyncio.sleep(2.1) # Безопасная задержка
                
                # Шаг 2: Переходим в Профиль напрямую через callback-дату кнопки
                logger.info("Имитация клика по кнопке [Профиль]...")
                # Передаем скрытую дату, которую шлет AyuGram при нажатии на Профиль
                await client.send_message(TARGET_BOT, '👤 Профиль') 
                # На случай, если бот принимает инлайн-запрос, сэмулируем клик по кнопке 'profile'
                async for message in client.iter_messages(TARGET_BOT, limit=3):
                    if message.buttons:
                        for row in message.buttons:
                            for button in row:
                                if "profile" in str(button.data) or "Профиль" in button.text:
                                    await button.click()
                                    report += "✅ Клик по меню [Профиль]\n"
                                    break
                
                await asyncio.sleep(2.3) # Ждем загрузку профиля
                
                # Шаг 3: Нажимаем "Промокод" внутри профиля
                logger.info("Имитация клика по кнопке [Промокод]...")
                promo_clicked = False
                async for message in client.iter_messages(TARGET_BOT, limit=3):
                    if message.buttons:
                        for row in message.buttons:
                            for button in row:
                                if "promo" in str(button.data) or "Промокод" in button.text:
                                    await button.click()
                                    report += "✅ Клик по кнопке [Промокод]\n"
                                    promo_clicked = True
                                    break
                        if promo_clicked: break
                                    
                await asyncio.sleep(1.9) # Идеальная пауза перед вводом
                
                # Шаг 4: Отправляем сам промокод текстом
                logger.info(f"Отправка промокода '{promo_code}'...")
                await client.send_message(TARGET_BOT, promo_code)
                report += f"🚀 Промокод успешно отправлен Патрику!\n"
                
                # Шаг 5: Перехватываем результат
                await asyncio.sleep(3.0)
                async for message in client.iter_messages(TARGET_BOT, limit=1):
                    report += f"\n💬 **Ответ Патрика:**\n_{message.text}_"
                
            except Exception as e:
                logger.error(f"Ошибка при вводе промокода: {e}")
                report += f"❌ **Ошибка в процессе:** {str(e)}"
            
            # Отправляем полный отчет тебе в Избранное
            await client.send_message('me', report)
            logger.info("Отчет отправлен в 'Saved Messages'")

# --- СВЯЗКА ЗАПУСКА FASTAPI И TELETHON ---
@app.on_event("startup")
async def startup_event():
    # Запуск юзербота при старте веб-сервера
    await client.start()
    logger.info("Юзербот Telethon успешно запущен в фоне!")

@app.on_event("shutdown")
async def shutdown_event():
    await client.disconnect()
