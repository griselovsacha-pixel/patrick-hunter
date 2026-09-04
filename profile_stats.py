"""
ProfileStats — источник правды по балансу и статистике аккаунта.

Раньше баланс "звёзд" считался вручную в коде (+2 за промокод, +0.4 за
задание и т.п.) — это была оценка "с потолка", а не реальные цифры бота.
Теперь после каждого события (ежедневка / полный цикл заданий / каждые
10 кликов) мы заходим в Профиль, парсим настоящий текст бота и берём
баланс/друзей/etc оттуда. Статистика в 20:00 тоже читает это, а не
внутренний счётчик.
"""
import logging

from config import STAR_MILESTONES
from utils import parse_profile_text

logger = logging.getLogger(__name__)


class ProfileStats:
    def __init__(self, tg_logger):
        self.tg_logger = tg_logger
        self.balance = 0.0
        self.friends = 0
        self.activated = 0
        self.reactivations = 0
        self.tg_id = None
        self.username = None
        self._reached_milestones = set()

        # Счётчики за день — сбрасываются в 20:00 после отчёта (см. account.py)
        self.daily_collected_today = False
        self.tasks_completed_today = 0
        self.tasks_skipped_today = 0
        self.clicker_clicks_today = 0
        self.promos_activated_today = []
        self.errors_today = []

    def add_error(self, msg: str):
        import datetime
        self.errors_today.append(f"{datetime.datetime.now().strftime('%H:%M')} {msg[:60]}")
        if len(self.errors_today) > 20:
            self.errors_today = self.errors_today[-20:]

    async def update_from_profile_text(self, text: str):
        """Парсит текст экрана Профиль и обновляет реальные цифры.
        Если баланс пересёк веху (15/25/50/100) — шлём закреп в группу."""
        data = parse_profile_text(text)
        if not data:
            return

        old_balance = self.balance
        self.balance = data.get('balance', self.balance)
        self.friends = data.get('friends', self.friends)
        self.activated = data.get('activated', self.activated)
        self.reactivations = data.get('reactivations', self.reactivations)
        self.tg_id = data.get('tg_id', self.tg_id)
        self.username = data.get('username', self.username)

        for milestone in STAR_MILESTONES:
            if old_balance < milestone <= self.balance and milestone not in self._reached_milestones:
                self._reached_milestones.add(milestone)
                await self.tg_logger.milestone(self.balance)

    def reset_daily(self):
        self.daily_collected_today = False
        self.tasks_completed_today = 0
        self.tasks_skipped_today = 0
        self.clicker_clicks_today = 0
        self.promos_activated_today = []
        self.errors_today = []

    def daily_report_text(self, clicker_target) -> str:
        promos_str = ", ".join(self.promos_activated_today) if self.promos_activated_today else "нет"
        errors_str = "\n".join(self.errors_today[-5:]) if self.errors_today else "нет"
        return (
            f"📊 СТАТИСТИКА ЗА ДЕНЬ (реальные данные бота)\n"
            f"{'━' * 25}\n"
            f"💰 Баланс сейчас: {self.balance:.2f}⭐\n"
            f"👥 Друзей: {self.friends} | Активировали: {self.activated} | Повторно: {self.reactivations}\n"
            f"📅 Ежедневка: {'✅ собрана' if self.daily_collected_today else '❌ не собрана'}\n"
            f"📋 Задания: ✅{self.tasks_completed_today} | ⏭️{self.tasks_skipped_today}\n"
            f"🎮 Кликер: {self.clicker_clicks_today}/{clicker_target} кликов\n"
            f"🔔 Промокоды: {promos_str}\n"
            f"{'━' * 25}\n"
            f"❌ Ошибки:\n{errors_str}"
        )
