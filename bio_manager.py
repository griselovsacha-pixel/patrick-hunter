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
import os

from telethon.tl.functions.account import UpdateProfileRequest, SetPrivacyRequest
from telethon.tl import types

from config import referral_link_for, TARGET_BOT
from antiban import safe_call

logger = logging.getLogger(__name__)


class BioManager:
    def __init__(self, client, tg_logger, label: str):
        self.client = client
        self.tg_logger = tg_logger
        self.label = label
        self._bio_already_set = False
        digits = "".join(ch for ch in label if ch.isdigit())
        self.number = int(digits or "1")
        self.protection = None   # AccountProtection — ставится из account.py
        self.user_id = None   # Telegram ID аккаунта — ставится из account.start() после входа

    @property
    def referral_link(self):
        """Личная ссылка аккаунта. Раньше ссылки были ЗАШИТЫ в config по номеру слота — при замене
        аккаунта (новая покупка в тот же слот) в био ставилась ссылка СТАРОГО аккаунта, и рефералы
        шли ему. Теперь по умолчанию — t.me/<бот>?start=<ID самого аккаунта> (формат подтверждён
        экраном «Заработать звезды»); REFERRAL_LINK_<N> в env по-прежнему главнее."""
        env = (os.getenv(f"REFERRAL_LINK_{self.number}") or "").strip()
        if env:
            return env
        if self.user_id:
            return f"https://t.me/{TARGET_BOT}?start={self.user_id}"
        return referral_link_for(self.number)

    async def ensure_referral_bio(self, force: bool = False):
        """Проставить реф. ссылку в описание. force=True — бот только что пожаловался
        на отсутствие ссылки: перепроверяем фактическое описание и ставим заново
        (раньше флаг _bio_already_set блокировал повтор, и цикл «поставил → бот
        снова просит» шёл бесконечно, хотя в профиле ссылки уже могло не быть)."""
        if not self.referral_link:
            await self.tg_logger.log("📝 Не знаю реф. ссылку аккаунта (нет ID и REFERRAL_LINK_<N>)", error="нет ссылки")
            return False
        if self._bio_already_set and not force:
            return True
        if not force:
            # При каждом рестарте НЕ пишем в профиль вслепую (UpdateProfile + SetPrivacy
            # на старте добавляли FloodWait): сначала читаем текущее описание.
            current = await self._read_bio()
            if current is not None and current == self.referral_link:
                self._bio_already_set = True
                return True
        try:
            await safe_call(
                lambda: self.client(UpdateProfileRequest(about=self.referral_link)),
                action="update_bio", protection=self.protection,
            )
            await self._make_bio_public()
            actual = await self._read_bio()
            self._bio_already_set = actual == self.referral_link
            if self._bio_already_set:
                await self.tg_logger.log(f"📝 Реф. ссылка проставлена в описание профиля: {self.referral_link}")
            else:
                await self.tg_logger.log(
                    f"📝 Описание после установки НЕ совпадает с ожидаемым: «{(actual or '')[:80]}» "
                    f"(ждали {self.referral_link})", error="био не применилось")
            return self._bio_already_set
        except Exception as e:
            await self.tg_logger.log("📝 Не удалось проставить реф. ссылку в описание", error=str(e))
            return False

    async def _read_bio(self):
        """Реальное описание профиля из Telegram (а не «мы же вызвали UpdateProfile»)."""
        try:
            from telethon.tl.functions.users import GetFullUserRequest
            full = await self.client(GetFullUserRequest(await self.client.get_me()))
            return (full.full_user.about or "").strip()
        except Exception as e:
            logger.warning(f"[{self.label}] Не удалось прочитать описание: {e}")
            return None

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
                action="set_bio_privacy", protection=self.protection,
            )
        except Exception as e:
            logger.warning(f"[{self.label}] Не удалось открыть приватность био: {e}")
