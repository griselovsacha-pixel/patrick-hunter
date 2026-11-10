"""
BioManager — когда ежедневка просит "поставь свою личную ссылку в описание
профиля, чтобы получать бонус", этот модуль сам:
  1. ставит личную реф. ссылку аккаунта (config.referral_link_for) в About (описание) аккаунта;
  2. пытается открыть приватность "Био" на "видно всем" (иначе бот бота не
     увидит и бонус не засчитает).

Настройка приватности "Bio" — относительно новая часть Telegram API,
поддержка в MTProto может отличаться версия к версии. Поэтому шаг с
приватностью — best-effort с try/except: если конкретный вызов недоступен
в установленной версии Telethon, само описание всё равно уже проставлено,
и это главное.
"""
import logging

from telethon.tl.functions.account import UpdateProfileRequest, SetPrivacyRequest
from telethon.tl import types

from config import referral_link_for
from antiban import safe_call

logger = logging.getLogger(__name__)


class BioManager:
    def __init__(self, client, tg_logger, label: str):
        self.client = client
        self.tg_logger = tg_logger
        self.label = label
        self._bio_already_set = False
        digits = "".join(ch for ch in label if ch.isdigit())
        self.referral_link = referral_link_for(int(digits or "1"))

    async def ensure_referral_bio(self):
        """Идемпотентно: если уже проставляли в этом запуске процесса —
        не дёргаем API повторно."""
        if self._bio_already_set:
            return True
        try:
            await safe_call(
                lambda: self.client(UpdateProfileRequest(about=self.referral_link)),
                action="update_bio",
            )
            await self._make_bio_public()
            self._bio_already_set = True
            await self.tg_logger.log(f"📝 Реф. ссылка проставлена в описание профиля: {self.referral_link}")
            return True
        except Exception as e:
            await self.tg_logger.log("📝 Не удалось проставить реф. ссылку в описание", error=str(e))
            return False

    async def _make_bio_public(self):
        """Best-effort: пытаемся открыть видимость 'Био' всем.
        Если InputPrivacyKeyAbout недоступен в этой версии Telethon —
        просто логируем и идём дальше, само описание уже установлено."""
        try:
            key = types.InputPrivacyKeyAbout()
        except AttributeError:
            logger.info(f"[{self.label}] InputPrivacyKeyAbout недоступен в этой версии Telethon — пропускаю")
            return
        try:
            await safe_call(
                lambda: self.client(SetPrivacyRequest(
                    key=key,
                    rules=[types.InputPrivacyValueAllowAll()],
                )),
                action="set_bio_privacy",
            )
        except Exception as e:
            logger.warning(f"[{self.label}] Не удалось открыть приватность био: {e}")
