
"""
ProfileStats — реальные данные профиля Patrick Hunter.

Баланс берётся не из арифметики событий, а из экрана «Профиль» бота.
Каждый успешный разбор баланса сохраняется в Supabase как snapshot.
"""
import logging

from config import STAR_MILESTONES
from utils import parse_profile_text

logger = logging.getLogger(__name__)


class ProfileStats:
    def __init__(self, tg_logger, db=None):
        self.tg_logger = tg_logger
        self.db = db

        self.balance = 0.0
        self.balance_updated_at = None

        self.friends = 0
        self.activated = 0
        self.reactivations = 0
        self.tg_id = None
        self.username = None
        self._reached_milestones = set()

        self.daily_collected_today = False
        self.tasks_completed_today = 0
        self.tasks_skipped_today = 0
        self.clicker_clicks_today = 0
        self.promos_activated_today = []
        self.errors_today = []

    def add_error(self, msg: str):
        import datetime

        self.errors_today.append(
            f"{datetime.datetime.now().strftime('%H:%M')} "
            f"{msg[:60]}"
        )

        if len(self.errors_today) > 20:
            self.errors_today = self.errors_today[-20:]

    async def update_from_profile_text(self, text: str):
        """Разбирает реальный экран Профиль и сохраняет баланс."""
        data = parse_profile_text(text)

        if not data:
            return False

        old_balance = self.balance

        if "balance" in data:
            self.balance = float(data["balance"])

            import datetime

            self.balance_updated_at = (
                datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat()
            )

            if self.db:
                await self.db.balance_snapshot(
                    self.balance
                )

        self.friends = data.get(
            "friends",
            self.friends,
        )
        self.activated = data.get(
            "activated",
            self.activated,
        )
        self.reactivations = data.get(
            "reactivations",
            self.reactivations,
        )
        self.tg_id = data.get(
            "tg_id",
            self.tg_id,
        )
        self.username = data.get(
            "username",
            self.username,
        )

        for milestone in STAR_MILESTONES:
            if (
                old_balance < milestone <= self.balance
                and milestone not in self._reached_milestones
            ):
                self._reached_milestones.add(milestone)
                await self.tg_logger.milestone(
                    self.balance
                )

        return True

    def reset_daily(self):
        self.daily_collected_today = False
        self.tasks_completed_today = 0
        self.tasks_skipped_today = 0
        self.clicker_clicks_today = 0
        self.promos_activated_today = []
        self.errors_today = []

    def daily_report_text(self, clicker_target) -> str:
        promos_str = (
            ", ".join(self.promos_activated_today)
            if self.promos_activated_today
            else "нет"
        )

        errors_str = (
            "\n".join(self.errors_today[-5:])
            if self.errors_today
            else "нет"
        )

        return (
            "📊 СТАТИСТИКА ЗА ДЕНЬ (реальные данные бота)\n"
            f"{'━' * 25}\n"
            f"💰 Баланс сейчас: {self.balance:.2f}⭐\n"
            f"👥 Друзей: {self.friends} | "
            f"Активировали: {self.activated} | "
            f"Повторно: {self.reactivations}\n"
            f"📅 Ежедневка: "
            f"{'✅ собрана' if self.daily_collected_today else '❌ не собрана'}\n"
            f"📋 Задания: "
            f"✅{self.tasks_completed_today} | "
            f"⏭️{self.tasks_skipped_today}\n"
            f"🎮 Кликер: "
            f"{self.clicker_clicks_today}/{clicker_target} кликов\n"
            f"🔔 Промокоды: {promos_str}\n"
            f"{'━' * 25}\n"
            f"❌ Ошибки:\n{errors_str}"
        )
