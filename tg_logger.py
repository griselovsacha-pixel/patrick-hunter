"""
TgLogger — единая точка логирования в группу (см. LOG_GROUP_INVITE в config.py).

Формат каждой записи: точное время, что сделал, была ли ошибка (и какая).
Для достижения звёздных вех и для ошибок — дополнительно закрепляем сообщение.
"""
import datetime
import logging

from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import UserAlreadyParticipantError, InviteHashInvalidError

from config import LOG_GROUP_INVITE, TIMEZONE_OFFSET
from antiban import safe_call

logger = logging.getLogger(__name__)


def _now_str() -> str:
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
    return now.strftime("%d.%m %H:%M:%S")


class TgLogger:
    def __init__(self, client, label: str, db=None):
        self.client = client
        self.label = label
        self.db = db
        self.group_entity = None  # резолвится в setup()

    async def setup(self):
        """Вступаем в группу логов (если ещё не вступили) и резолвим entity.
        Если группа недоступна — не роняем весь бот, просто пишем в консоль."""
        invite_hash = LOG_GROUP_INVITE.split('+')[-1]
        try:
            updates = await self.client(ImportChatInviteRequest(invite_hash))
            self.group_entity = updates.chats[0]
        except UserAlreadyParticipantError:
            try:
                self.group_entity = await self.client.get_entity(LOG_GROUP_INVITE)
            except Exception as e:
                logger.error(f"[{self.label}] Не смог зарезолвить группу логов: {e}")
        except InviteHashInvalidError:
            logger.error(f"[{self.label}] Невалидная ссылка группы логов: {LOG_GROUP_INVITE}")
        except Exception as e:
            logger.error(f"[{self.label}] Не смог вступить в группу логов: {e}")

    async def log(self, action: str, error: str = None, pin: bool = False):
        """Основной метод: отправляет запись 'время / действие / ошибка'.
        Всегда пишет и в консоль (Render-логи), группа — best-effort."""
        status = f"❌ ОШИБКА: {error}" if error else "✅ OK"
        line = f"🕐 {_now_str()} [{self.label}]\n{action}\n{status}"
        (logger.error if error else logger.info)(f"[{self.label}] {action} | {status}")
        if self.db:
            await self.db.log("ERROR" if error else "INFO", "telegram", action, error=error)

        if not self.group_entity:
            return
        try:
            msg = await safe_call(
                lambda: self.client.send_message(self.group_entity, line),
                action="log_to_group",
            )
            if pin or error:
                try:
                    await self.client.pin_message(self.group_entity, msg, notify=bool(error))
                except Exception as e:
                    logger.warning(f"[{self.label}] Не удалось закрепить сообщение: {e}")
        except Exception as e:
            logger.error(f"[{self.label}] Ошибка отправки в группу логов: {e}")

    async def milestone(self, stars: float):
        """Закреплённое сообщение о достижении звёздной вехи."""
        text = f"🌟 Накоплено {stars:.2f}⭐ — можно выводить!"
        await self.log(text, pin=True)
