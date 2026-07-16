import os
from typing import Dict, Any

# --- Основные настройки ---
API_ID = 31206776
API_HASH = '7ca69453a014d6a87d67afe4d9df8284'
CHANNEL_SOURCE = 'patrickstarsfarm'
TARGET_BOT = 'patrickstarsrobot'
TIMEZONE_OFFSET = 3

# --- Конфигурация аккаунтов (БЕЗ ПРОКСИ) ---
ACCOUNTS_CONFIG = [
    {
        "name": "main",
        "session_string": os.getenv("TELEGRAM_SESSION"),
        "target_bot": "patrickstarsrobot",
        "report_to": "sanzezzz",
        "delay_min": 1.5,
        "delay_max": 3.5,
        "check_interval": 3,          # Мониторинг каждые 3 секунды
        "daily_offset": (0, 180),
        "clicker_times": 10,
    },
    {
        "name": "heroesxdz",
        "session_string": os.getenv("TELEGRAM_SESSION_2"),
        "target_bot": "patrickstarsrobot",
        "report_to": "sanzezzz",
        "delay_min": 2.5,
        "delay_max": 5.0,              # Чуть медленнее для второго
        "check_interval": 4,           # Мониторинг каждые 4 секунды
        "daily_offset": (900, 1800),   # Смещение 15-30 минут
        "clicker_times": 8,
    }
]

# --- Задержки для разных операций ---
DELAYS = {
    "check": (0.3, 1.0),      # Проверка чека
    "promo": (1.0, 2.5),      # Активация промо
    "click": (0.5, 1.5),      # Клик по кнопке
    "join": (1.0, 3.0),       # Подписка
    "captcha": (1.5, 3.0),    # Решение капчи
    "daily": (2.0, 4.0),      # Ежедневка
    "task": (2.0, 5.0),       # Задания
}

# --- Интервалы заданий ---
TASK_INTERVALS = {
    "main": (5400, 9000),     # 1.5-2.5 часа
    "heroesxdz": (7200, 10800), # 2-3 часа (реже)
}
