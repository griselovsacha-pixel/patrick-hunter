import re
import os
import sys
import random
import asyncio
import logging
import datetime
import urllib.request
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, Tuple
from datetime import timedelta
import time

from fastapi import FastAPI
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, StartBotRequest
from telethon.tl.functions.folders import EditPeerFoldersRequest
from telethon.tl.types import InputFolderPeer
from telethon.errors import (
    UserAlreadyParticipantError,
    InviteHashInvalidError,
    FloodWaitError,
    RPCError
)

from config import *

# --- Логирование ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# --- Константы ---
ALL_DONE_PHRASES = [
    "выполнил все задания", "все задания выполнены",
    "приходи через", "новых заданий нет",
    "заданий больше нет", "респект", "молодец"
]

CONFIRM_WORDS = [
    "подтвер", "выдать", "проверить", "claim", "check",
    "выполнил", "готов", "получить", "забрать", "collect",
    "confirm", "done", "изучил", "ознакомил", "принять",
    "я подписал", "продолжить"
]

SPONSOR_GATE_HINTS = ["для продолжения", "выполни всего", "выполни задани", "чтобы продолжить"]

IGNORED_WORDS = {
    "html", "http", "https", "main", "patrickstarsrobot", "patrickstarsfarm",
    "profile", "claim", "check", "start", "webapp", "join", "bot", "stars",
    "tgwebapp", "startapp", "go", "app", "link", "channel", "group", "send",
    "ловите", "вводим", "боте", "профиль", "промокод", "дейли", "друзья"
}

FRUIT_MAP = {
    "🍎": ["яблок", "apple", "red apple"],
    "🍏": ["зелен яблок", "green apple"],
    "🍊": ["апельсин", "orange", "мандарин", "mandarin", "tangerine"],
    "🍋": ["лимон", "lemon"],
    "🍋‍🟩": ["лайм", "lime"],
    "🍇": ["виноград", "grape"],
    "🍓": ["клубник", "strawberry"],
    "🫐": ["черник", "голубик", "blueberry"],
    "🍒": ["вишн", "черешн", "cherry"],
    "🍑": ["персик", "peach"],
    "🍍": ["ананас", "pineapple"],
    "🥭": ["манго", "mango"],
    "🍌": ["банан", "banana"],
    "🍉": ["арбуз", "watermelon"],
    "🍈": ["дын", "melon"],
    "🥝": ["киви", "kiwi"],
    "🍐": ["груш", "pear"],
    "🍅": ["помидор", "томат", "tomato"],
    "🥥": ["кокос", "coconut"],
    "🥑": ["авокадо", "avocado"],
}

# --- Глобальные хранилища ---
_activated_promos = set()
_promo_in_progress = set()
_claimed_checks = set()
account_stats = {}
promo_queue = asyncio.Queue()
is_processing_promo = False
monitors = []

# --- Утилиты ---
def reset_stats():
    return {
        "daily_collected": False,
        "daily_errors": 0,
        "tasks_completed": 0,
        "tasks_skipped": 0,
        "promos_activated": [],
        "clicker_clicks": 0,
        "errors": [],
        "stars_earned": 0.0,
        "last_activity": datetime.datetime.now()
    }

def solve_math(text: str):
    match = re.search(r'(\d+)\s*([\+\-\*\/])\s*(\d+)\s*=\s*\?', text)
    if match:
        a, op, b = int(match.group(1)), match.group(2), int(match.group(3))
        if op == '+': return a + b
        if op == '-': return a - b
        if op == '*': return a * b
        if op == '/' and b != 0: return a // b
    return None

async def human_delay(config: Dict, action: str = "click"):
    """Задержка с учётом аккаунта"""
    delay_config = DELAYS.get(action, (1.0, 3.0))
    delay_min = delay_config[0] * config.get("delay_multiplier", 1.0)
    delay_max = delay_config[1] * config.get("delay_multiplier", 1.0)
    delay = random.uniform(delay_min, delay_max)
    await asyncio.sleep(delay)

async def send_report(client, config: Dict, text: str):
    """Отправка отчёта"""
    try:
        target = config.get("report_to", "sanzezzz")
        account_name = config.get("name", "unknown")
        if len(text) > 4000:
            text = text[:4000] + "..."
        await client.send_message(target, f"[{account_name}] {text}")
        logger.info(f"📤 [{account_name}] Отчёт: {text[:80]}")
    except FloodWaitError as e:
        logger.warning(f"⚠️ FloodWait: {e.seconds}с")
        await asyncio.sleep(e.seconds + 5)
    except Exception as e:
        logger.error(f"Ошибка отправки отчёта: {e}")

def find_promo_in_text(text: str):
    """Поиск промокода в тексте - УСКОРЕННЫЙ"""
    if not text:
        return None
    
    lower_text = text.lower()
    if not any(w in lower_text for w in ["промо", "активац", "promo"]):
        return None

    exact = re.search(r'активаций\s*:\s*([a-zA-Z0-9]{3,20})', text, re.IGNORECASE)
    if exact:
        return exact.group(1)

    clean_text = re.sub(r'[^\w\s]', ' ', text)
    words = clean_text.split()
    
    for i, word in enumerate(words):
        if word.lower() in ["активаций", "промокод", "promo"]:
            if i + 1 < len(words):
                candidate = words[i + 1].strip()
                if re.match(r'^[a-zA-Z0-9]{3,20}$', candidate):
                    return candidate
    
    for i, word in enumerate(words):
        if word.lower() in ["активаций", "промокод", "promo", "код"]:
            if i + 1 < len(words):
                candidate = words[i + 1].strip()
                if re.match(r'^[a-zA-Z0-9]{5,15}$', candidate):
                    return candidate
    
    return None

# --- Работа с Telegram ---
async def get_bot_response(client, config: Dict, max_age: int = 120, attempts: int = 5):
    """Быстрое получение ответа бота"""
    target_bot = config.get("target_bot", TARGET_BOT)
    
    for i in range(attempts):
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            async for msg in client.iter_messages(target_bot, limit=5):
                age = (now - msg.date).total_seconds()
                if age < max_age and (msg.text or msg.buttons):
                    return msg
        except Exception as e:
            logger.debug(f"Ошибка получения ответа: {e}")
        
        await asyncio.sleep(0.8)
    
    try:
        async for msg in client.iter_messages(target_bot, limit=1):
            return msg
    except:
        pass
    
    return None

async def archive_chat(client, entity):
    try:
        peer = await client.get_input_entity(entity)
        await client(EditPeerFoldersRequest(
            folder_peers=[InputFolderPeer(peer=peer, folder_id=1)]
        ))
    except Exception as e:
        logger.debug(f"Не удалось архивировать: {e}")

async def safe_join(client, target: str):
    try:
        if target.startswith('+'):
            await client(ImportChatInviteRequest(target[1:]))
        else:
            await client(JoinChannelRequest(target))
        logger.info(f"✅ Подписался: @{target}")
        await archive_chat(client, target)
        await human_delay({}, "join")
    except UserAlreadyParticipantError:
        logger.info(f"ℹ️ Уже подписан: {target}")
        await archive_chat(client, target)
    except FloodWaitError as e:
        logger.warning(f"⚠️ FloodWait: {e.seconds}с")
        await asyncio.sleep(e.seconds + 5)
    except Exception as e:
        logger.error(f"Ошибка вступления {target}: {e}")

async def solve_captcha_if_needed(client, config: Dict, msg) -> bool:
    """БЫСТРОЕ решение капчи"""
    if not msg:
        return False
    text = msg.text or ""
    if "проверка на робота" not in text.lower():
        return True

    logger.info(f"🤖 Капча: {text[:80]}")
    if not msg.buttons:
        return False

    res = solve_math(text)
    if res is not None:
        for row in msg.buttons:
            for btn in row:
                if btn.text.strip() == str(res):
                    await btn.click()
                    await human_delay(config, "captcha")
                    return True
        return False

    clean_text = re.sub(r"\*+", "", text).lower()
    word_match = re.search(r"[«\"'“”‘’](.+?)[»\"'“”‘’]", clean_text)
    if word_match:
        target_word = word_match.group(1).lower().strip()
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
                    await human_delay(config, "captcha")
                    return True

    return False

async def _open_bot_unlocked(client, config: Dict, retries: int = 2):
    """БЫСТРОЕ открытие бота"""
    target_bot = config.get("target_bot", TARGET_BOT)
    
    for attempt in range(retries):
        try:
            await client.send_message(target_bot, '/start')
            await human_delay(config, "click")
            
            msg = await get_bot_response(client, config)
            if not msg:
                if attempt < retries - 1:
                    await asyncio.sleep(2)
                    continue
                return None

            for _ in range(3):
                if not msg:
                    return None
                text_lower = (msg.text or "").lower()

                if "проверка" in text_lower:
                    if not await solve_captcha_if_needed(client, config, msg):
                        return None
                    await human_delay(config, "captcha")
                    msg = await get_bot_response(client, config)
                    continue

                if looks_like_sponsor_gate(msg):
                    msg = await handle_sponsor_gate(client, config, msg)
                    continue

                break

            return msg
            
        except FloodWaitError as e:
            logger.warning(f"⚠️ FloodWait: {e.seconds}с")
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            logger.error(f"Ошибка открытия: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(3)
                continue
            return None
    
    return None

def looks_like_sponsor_gate(msg) -> bool:
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

async def handle_sponsor_gate(client, config: Dict, msg):
    logger.info(f"🚧 Спонсорский гейт")
    confirm_btn = None
    
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
                        await safe_join(client, target)

    if confirm_btn:
        await human_delay(config, "click")
        await confirm_btn.click()
        logger.info(f"✅ Гейт пройден")
    
    await human_delay(config, "click")
    return await get_bot_response(client, config)

# --- Обработчик очереди промокодов (УСКОРЕННЫЙ) ---
async def promo_queue_processor():
    """Быстрая обработка промокодов из очереди"""
    global is_processing_promo
    
    while True:
        try:
            if promo_queue.empty():
                await asyncio.sleep(0.5)
                continue
            
            if is_processing_promo:
                await asyncio.sleep(1)
                continue
            
            client, config, code = await promo_queue.get()
            
            if code.lower() in _activated_promos:
                promo_queue.task_done()
                continue
            
            is_processing_promo = True
            
            try:
                logger.info(f"🚀 Активация: {code}")
                success = await activate_promo_fast(client, config, code)
                
                if success:
                    _activated_promos.add(code.lower())
                    stats = account_stats.get(config.get("name", "unknown"), {})
                    if "promos_activated" in stats:
                        stats["promos_activated"].append(code)
                    await send_report(client, config, f"✅ Промокод `{code}` +2⭐")
                else:
                    await send_report(client, config, f"❌ Промокод `{code}` не активирован")
                    
            except Exception as e:
                logger.error(f"Ошибка: {e}")
            finally:
                is_processing_promo = False
                promo_queue.task_done()
                
            await asyncio.sleep(random.uniform(5, 12))
            
        except Exception as e:
            logger.error(f"Ошибка в очереди: {e}")
            is_processing_promo = False
            await asyncio.sleep(3)

async def activate_promo_fast(client, config: Dict, code: str) -> bool:
    """БЫСТРАЯ активация промокода"""
    target_bot = config.get("target_bot", TARGET_BOT)
    
    try:
        msg = await _open_bot_unlocked(client, config)
        if not msg or not msg.buttons:
            return False

        profile_clicked = False
        for row in msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in ["профил", "profile"]):
                    await btn.click()
                    profile_clicked = True
                    await human_delay(config, "click")
                    break
            if profile_clicked:
                break

        if not profile_clicked:
            return False

        msg = await get_bot_response(client, config)
        if not msg or not msg.buttons:
            return False

        promo_clicked = False
        for row in msg.buttons:
            for btn in row:
                if "промо" in btn.text.lower():
                    await btn.click()
                    promo_clicked = True
                    await human_delay(config, "click")
                    break
            if promo_clicked:
                break

        if not promo_clicked:
            return False

        await client.send_message(target_bot, code)
        await human_delay(config, "promo")

        resp = await get_bot_response(client, config)
        if not resp:
            return False

        resp_text = resp.text or ""

        if any(w in resp_text.lower() for w in ["неверн", "не найден", "истёк", "уже использован"]):
            return True

        if resp.buttons:
            for r in resp.buttons:
                for b in r:
                    url = getattr(b, 'url', None)
                    if url:
                        tme = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                        if tme and not tme.group(1).lower().endswith('bot'):
                            await safe_join(client, tme.group(1))
                    if any(w in b.text.lower() for w in ["подписал", "проверить", "готово", "check"]):
                        await human_delay(config, "click")
                        await b.click()

        return True

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return False

# --- МОНИТОР КАНАЛА (УСКОРЕННЫЙ) ---
class ChannelMonitor:
    def __init__(self, account_name: str, config: Dict):
        self.account_name = account_name
        self.config = config
        self.last_checked_id = None
        self.check_interval = config.get("check_interval", 3)
        self._last_check = 0
        
    async def monitor(self, client):
        """БЫСТРЫЙ мониторинг канала"""
        while True:
            try:
                now = time.time()
                if now - self._last_check < 1:
                    await asyncio.sleep(0.5)
                    continue
                self._last_check = now
                
                async for msg in client.iter_messages(CHANNEL_SOURCE, limit=3):
                    if self.last_checked_id and msg.id <= self.last_checked_id:
                        continue
                    
                    raw = msg.text or ""
                    
                    if msg.buttons:
                        asyncio.create_task(try_claim_check_instant(client, self.config, msg))
                    
                    code = find_promo_in_text(raw)
                    if code:
                        logger.info(f"🔔 [{self.account_name}] Промо: {code}")
                        await promo_queue.put((client, self.config, code))
                    
                    if self.last_checked_id is None or msg.id > self.last_checked_id:
                        self.last_checked_id = msg.id
                
                interval = self.check_interval + random.uniform(-0.5, 1.0)
                await asyncio.sleep(max(0.5, interval))
                
            except FloodWaitError as e:
                logger.warning(f"⚠️ FloodWait: {e.seconds}с")
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:
                logger.error(f"Ошибка мониторинга [{self.account_name}]: {e}")
                await asyncio.sleep(5)

# --- Функции для аккаунтов ---
async def create_client(config: Dict) -> Optional[TelegramClient]:
    """Создаёт клиент БЕЗ ПРОКСИ"""
    session_string = config.get("session_string")
    if not session_string:
        logger.warning(f"⚠️ Нет session для {config.get('name')}")
        return None
    
    return TelegramClient(StringSession(session_string), API_ID, API_HASH)

async def try_claim_check_instant(client, config: Dict, msg):
    """Мгновенный захват чека"""
    if not msg.buttons:
        return

    text = (msg.text or "").lower()
    if not any(w in text for w in ["чек", "check", "получить", "claim", "⭐", "звезд"]):
        return

    msg_id = msg.id
    if msg_id in _claimed_checks:
        return

    for row in msg.buttons:
        for btn in row:
            if any(w in btn.text.lower() for w in ["получить", "claim", "забрать", "взять", "🎁"]):
                logger.info(f"🎁 ЧЕК!")
                _claimed_checks.add(msg_id)
                try:
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    await btn.click()
                    await send_report(client, config, f"🎁 ЧЕК ПОЙМАН!")
                except Exception as e:
                    logger.error(f"Ошибка: {e}")
                return

# --- ОСНОВНЫЕ ЦИКЛЫ ---

async def collect_daily(client, config: Dict) -> int:
    """Сбор ежедневки"""
    account_name = config.get("name", "unknown")
    stats = account_stats.get(account_name, reset_stats())
    
    try:
        logger.info(f"📅 [{account_name}] Ежедневка...")
        
        msg = await _open_bot_unlocked(client, config)
        if not msg or not msg.buttons:
            return 3600

        profile_clicked = False
        for row in msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in ["профил", "profile"]):
                    await btn.click()
                    profile_clicked = True
                    await human_delay(config, "daily")
                    break
            if profile_clicked:
                break

        if not profile_clicked:
            return 3600

        msg = await get_bot_response(client, config)
        if not msg or not msg.buttons:
            return 3600

        daily_clicked = False
        for row in msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in ["еже", "daily", "бонус"]):
                    await btn.click()
                    daily_clicked = True
                    await human_delay(config, "daily")
                    break
            if daily_clicked:
                break

        if not daily_clicked:
            return 3600

        msg = await get_bot_response(client, config)
        if not msg:
            return 3600

        text = msg.text or ""

        if any(w in text.lower() for w in ["уже", "завтра", "получил", "следующ"]):
            return 24 * 3600

        if "проверка" in text.lower():
            if await solve_captcha_if_needed(client, config, msg):
                stats["daily_collected"] = True
                stats["stars_earned"] += 2.0
                await send_report(client, config, "📅 Ежедневка +2⭐")
                return 24 * 3600

        return 24 * 3600

    except Exception as e:
        logger.error(f"Ошибка ежедневки [{account_name}]: {e}")
        return 3600

async def daily_cycle(client, config: Dict):
    """Цикл ежедневки"""
    account_name = config.get("name", "unknown")
    daily_offset = config.get("daily_offset", (0, 300))
    
    while True:
        offset = random.randint(daily_offset[0], daily_offset[1])
        await asyncio.sleep(offset)
        
        wait_sec = await collect_daily(client, config)
        h, m = wait_sec // 3600, (wait_sec % 3600) // 60
        logger.info(f"⏳ [{account_name}] Ежедневка через {h}ч {m}м")
        await asyncio.sleep(wait_sec)

# --- ЗАДАНИЯ (НЕ ПРОПУСКАЕМ) ---
async def tasks_cycle(client, config: Dict):
    """Цикл заданий - ВСЕ ЗАДАНИЯ ВЫПОЛНЯЮТСЯ"""
    account_name = config.get("name", "unknown")
    stats = account_stats.get(account_name, reset_stats())
    
    while True:
        try:
            logger.info(f"📋 [{account_name}] Задания...")
            
            msg = await _open_bot_unlocked(client, config)
            if not msg or not msg.buttons:
                await asyncio.sleep(1800)
                continue

            task_opened = False
            for row in msg.buttons:
                for btn in row:
                    if "задани" in btn.text.lower():
                        await btn.click()
                        task_opened = True
                        await human_delay(config, "task")
                        break
                if task_opened:
                    break

            if not task_opened:
                await asyncio.sleep(1800)
                continue

            completed = 0
            skipped = 0
            
            for i in range(30):
                task_msg = await get_bot_response(client, config)
                if not task_msg:
                    break

                if any(phrase in (task_msg.text or "").lower() for phrase in ALL_DONE_PHRASES):
                    logger.info(f"✅ [{account_name}] Все задания выполнены!")
                    break

                result, desc = await process_one_task(client, config, task_msg)

                if result == 'done':
                    completed += 1
                    stats["tasks_completed"] += 1
                    await send_report(client, config, f"✅ Задание #{completed}")
                    await human_delay(config, "task")
                elif result == 'skip':
                    skipped += 1
                    stats["tasks_skipped"] += 1
                    logger.info(f"⏭️ [{account_name}] Пропущено: {desc[:50]}")
                    await human_delay(config, "task")

                if completed > 0 and completed % 5 == 0:
                    await asyncio.sleep(random.uniform(5, 10))

            account_name_key = config.get("name", "main")
            intervals = TASK_INTERVALS.get(account_name_key, (5400, 9000))
            interval = random.randint(intervals[0], intervals[1])
            h, m = interval // 3600, (interval % 3600) // 60
            
            await send_report(
                client, config,
                f"📊 Цикл: ✅{completed} | ⏭️{skipped} | ⏳{h}ч{m}м"
            )
            await asyncio.sleep(interval)

        except Exception as e:
            logger.error(f"Ошибка [{account_name}]: {e}")
            await asyncio.sleep(1800)

# --- ИСПРАВЛЕННАЯ ФУНКЦИЯ ОБРАБОТКИ ЗАДАНИЙ ---
async def process_one_task(client, config: Dict, task_msg) -> tuple:
    """Обработка одного задания - ВСЕ ВЫПОЛНЯЕМ"""
    text = task_msg.text or ""
    
    # === НОВАЯ ПРОВЕРКА НА /start ===
    if text.strip() == "/start" or text.strip().lower() == "start":
        # Ищем кнопку "Пропустить"
        if task_msg.buttons:
            for row in task_msg.buttons:
                for btn in row:
                    if "пропустить" in btn.text.lower():
                        await btn.click()
                        return 'skip', 'команда start (пропущено)'
        return 'skip', 'команда start'
    
    if any(phrase in text.lower() for phrase in ALL_DONE_PHRASES):
        return 'all_done', text

    # WebApp
    if task_msg.buttons:
        for row in task_msg.buttons:
            for btn in row:
                if is_webapp_button(btn):
                    return await handle_webapp_task(client, config, task_msg)

    # Обычное задание
    action_done = False
    action_parts = []
    confirm_btn = None

    if task_msg.buttons:
        for row in task_msg.buttons:
            for btn in row:
                btn_text = btn.text.lower()
                url = getattr(btn, 'url', None)
                
                if any(w in btn_text for w in CONFIRM_WORDS):
                    confirm_btn = btn
                    continue
                
                if any(w in btn_text for w in ["пропустить", "меню", "назад"]):
                    continue
                
                if url:
                    tme_match = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                    if tme_match:
                        target = tme_match.group(1)
                        if target.lower().endswith('bot'):
                            await client.send_message(target, '/start')
                        else:
                            await safe_join(client, target)
                        action_parts.append(f"подписался на {target}")
                        action_done = True

    if not action_done:
        for target in re.findall(r't\.me/(\+?[a-zA-Z0-9_]+)', text):
            if target.lower().endswith('bot'):
                await client.send_message(target, '/start')
            else:
                await safe_join(client, target)
            action_parts.append(f"подписался на {target}")
            action_done = True

    if not action_done and confirm_btn:
        action_done = True
        action_parts.append("ознакомился")

    if not action_done:
        if task_msg.buttons:
            for row in task_msg.buttons:
                for btn in row:
                    if "пропустить" in btn.text.lower():
                        await btn.click()
                        return 'skip', 'пропущено'
        return 'skip', 'не найдено'

    await human_delay(config, "click")

    if confirm_btn:
        await confirm_btn.click()
        await human_delay(config, "click")
        return 'done', ", ".join(action_parts)

    fresh_msg = await get_bot_response(client, config)
    if fresh_msg and fresh_msg.buttons:
        for row in fresh_msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in CONFIRM_WORDS):
                    await btn.click()
                    await human_delay(config, "click")
                    return 'done', ", ".join(action_parts) + " ✅"

    return 'done', ", ".join(action_parts)

def is_webapp_button(btn) -> bool:
    btn_text = btn.text.lower()
    if any(w in btn_text for w in ["запустить приложение", "webapp", "open app"]):
        return True
    if type(btn).__name__ in ['KeyboardButtonWebView', 'InlineKeyboardButtonWebApp']:
        return True
    url = getattr(btn, 'url', None)
    if url and any(w in url.lower() for w in ['startapp', 'tgwebapp']):
        return True
    return False

async def handle_webapp_task(client, config: Dict, task_msg):
    """Обработка WebApp"""
    target_bot = config.get("target_bot", TARGET_BOT)
    
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
        match = re.search(r't\.me/([a-zA-Z0-9_]+)(?:/\w+)?\?startapp=([a-zA-Z0-9_]+)', startapp_url)
        if match:
            bot_username = match.group(1)
            start_param = match.group(2)
            bot = await client.get_entity(bot_username)
            await client(StartBotRequest(bot=bot, peer=bot, start_param=start_param))
            await human_delay(config, "task")
            
            fresh_msg = await get_bot_response(client, config)
            if fresh_msg and fresh_msg.buttons:
                for row in fresh_msg.buttons:
                    for btn in row:
                        if any(w in btn.text.lower() for w in CONFIRM_WORDS):
                            await btn.click()
                            await human_delay(config, "click")
                            return 'done', 'startapp ✅'
            return 'done', 'startapp'

    for row in task_msg.buttons:
        for btn in row:
            if "пропустить" in btn.text.lower():
                await btn.click()
                return 'skip', 'пропущен'

    return 'skip', 'не удалось'

# --- КЛИКЕР ---
async def clicker_cycle(client, config: Dict):
    """Цикл кликера"""
    account_name = config.get("name", "unknown")
    target_bot = config.get("target_bot", TARGET_BOT)
    stats = account_stats.get(account_name, reset_stats())
    
    CLICKER_START_HOUR = 8
    CLICKER_END_HOUR = 24
    clicker_times = config.get("clicker_times", 10)

    while True:
        try:
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)

            if now.hour < CLICKER_START_HOUR:
                wake_at = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
                wait_sec = (wake_at - now).total_seconds()
                logger.info(f"🕐 [{account_name}] Кликер спит")
                await asyncio.sleep(wait_sec)
                continue

            window_sec = (CLICKER_END_HOUR - CLICKER_START_HOUR) * 3600
            part_sec = window_sec // clicker_times
            day_start = now.replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)

            click_times = []
            for i in range(clicker_times):
                part_start = day_start + datetime.timedelta(seconds=i * part_sec)
                rand_sec = random.randint(0, int(part_sec * 0.8))
                click_time = part_start + datetime.timedelta(seconds=rand_sec + random.randint(0, 300))
                midnight = day_start + datetime.timedelta(hours=16)
                if click_time > midnight:
                    click_time = midnight - datetime.timedelta(seconds=random.randint(60, 600))
                click_times.append(click_time)

            for i, click_time in enumerate(click_times):
                now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
                if click_time <= now:
                    continue

                wait_sec = (click_time - now).total_seconds()
                h, m = int(wait_sec // 3600), int((wait_sec % 3600) // 60)
                logger.info(f"🎮 [{account_name}] Клик #{i+1} через {h}ч {m}м")
                await asyncio.sleep(wait_sec)

                try:
                    await client.send_message(target_bot, '/start')
                    await human_delay(config, "click")
                    
                    msg = await get_bot_response(client, config)
                    if not msg or not msg.buttons:
                        continue

                    if "проверка" in (msg.text or "").lower():
                        if not await solve_captcha_if_needed(client, config, msg):
                            continue

                    clicker_clicked = False
                    if msg and msg.buttons:
                        for row in msg.buttons:
                            for btn in row:
                                if any(w in btn.text.lower() for w in ["кликер", "clicker", "farm", "✨"]):
                                    await btn.click()
                                    clicker_clicked = True
                                    await human_delay(config, "click")
                                    break
                            if clicker_clicked:
                                break

                    if clicker_clicked:
                        stats["clicker_clicks"] += 1
                        stats["stars_earned"] += 0.1
                        logger.info(f"🎮 [{account_name}] Клик #{i+1} собран!")

                except FloodWaitError as e:
                    logger.warning(f"⚠️ FloodWait: {e.seconds}с")
                    await asyncio.sleep(e.seconds + 5)
                except Exception as e:
                    logger.error(f"🎮 Ошибка: {e}")

            tomorrow_8 = (now + datetime.timedelta(days=1)).replace(hour=CLICKER_START_HOUR, minute=0, second=0, microsecond=0)
            wait_sec = (tomorrow_8 - now).total_seconds()
            await asyncio.sleep(wait_sec)

        except Exception as e:
            logger.error(f"Ошибка кликера [{account_name}]: {e}")
            await asyncio.sleep(3600)

# --- СТАТИСТИКА ---
async def stats_cycle(client, config: Dict):
    """Цикл статистики"""
    account_name = config.get("name", "unknown")
    stats = account_stats.get(account_name, reset_stats())
    
    while True:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
        target = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        await asyncio.sleep(wait_sec)

        s = stats
        promos_str = ", ".join(s["promos_activated"][-5:]) if s["promos_activated"] else "нет"
        
        report = (
            f"📊 СТАТИСТИКА [{account_name}]\n"
            f"{'━' * 25}\n"
            f"📅 Ежедневка: {'✅' if s['daily_collected'] else '❌'}\n"
            f"📋 Задания: ✅{s['tasks_completed']} | ⏭️{s['tasks_skipped']}\n"
            f"🎮 Кликер: {s['clicker_clicks']} кликов\n"
            f"🔔 Промокоды: {promos_str}\n"
            f"⭐ ~{s['stars_earned']:.1f}⭐"
        )
        await send_report(client, config, report)
        account_stats[account_name] = reset_stats()

# --- SELF-PING ---
async def keep_alive_cycle():
    external_url = os.getenv("RENDER_EXTERNAL_URL")
    if not external_url:
        return

    ping_url = external_url.rstrip('/') + '/health'
    while True:
        await asyncio.sleep(240 + random.randint(-30, 30))
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(ping_url, timeout=15).read())
        except Exception as e:
            logger.warning(f"⚠️ Self-ping: {e}")

# --- ЗАПУСК АККАУНТА ---
async def run_account(account_config: Dict):
    """Запуск одного аккаунта"""
    account_name = account_config.get("name", "unknown")
    
    try:
        client = await create_client(account_config)
        if not client:
            logger.error(f"❌ Не удалось создать клиент для {account_name}")
            return
        
        await client.start()
        logger.info(f"✅ [{account_name}] Запущен")
        
        account_stats[account_name] = reset_stats()
        
        monitor = ChannelMonitor(account_name, account_config)
        monitors.append(monitor)
        
        @client.on(events.NewMessage(chats=CHANNEL_SOURCE))
        @client.on(events.MessageEdited(chats=CHANNEL_SOURCE))
        async def handle_channel(event):
            raw = event.raw_text or ""
            code = find_promo_in_text(raw)
            if code:
                logger.info(f"🔔 [{account_name}] Промо: {code}")
                await promo_queue.put((client, account_config, code))
            
            if event.message.buttons:
                asyncio.create_task(try_claim_check_instant(client, account_config, event.message))

        await asyncio.gather(
            daily_cycle(client, account_config),
            tasks_cycle(client, account_config),
            clicker_cycle(client, account_config),
            monitor.monitor(client),
            stats_cycle(client, account_config),
            promo_queue_processor()
        )
        
    except Exception as e:
        logger.error(f"[{account_name}] Ошибка: {e}")
    finally:
        await client.disconnect()

# --- FASTAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = []
    for account_config in ACCOUNTS_CONFIG:
        if account_config.get("session_string"):
            tasks.append(asyncio.create_task(run_account(account_config)))
    
    tasks.append(asyncio.create_task(keep_alive_cycle()))
    
    yield
    
    for task in tasks:
        task.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/")
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "accounts": len([a for a in ACCOUNTS_CONFIG if a.get("session_string")]),
        "promo_queue": promo_queue.qsize()
    }
