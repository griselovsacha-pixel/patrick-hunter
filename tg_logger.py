"""
TgLogger — единая точка логирования в группу (см. LOG_GROUP_INVITE в config.py).

Формат каждой записи: точное время, что сделал, была ли ошибка (и какая).
Для достижения звёздных вех и для ошибок — дополнительно закрепляем сообщение.
"""
import asyncio
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
        self.protection = None    # AccountProtection — ставится из account.py (kind='log': логи ничего не ждут)
        # Лог НИКОГДА не должен тормозить рабочий код. Раньше log() делал
        # запись в Supabase и send_message в группу прямо в вызывающем
        # коде: на горячем пути промокода это +1-3с до первой попытки, а
        # при FloodWait на отправке в группу — сон до 10 минут ПРЯМО внутри
        # активации. Теперь log() только кладёт запись в очередь, а
        # отправляет отдельный воркер со своим темпом.
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._worker = None

    async def setup(self):
        """Вступаем в группу логов (если ещё не вступили) и резолвим entity.
        Если группа недоступна — не роняем весь бот, просто пишем в консоль."""
        invite_hash = LOG_GROUP_INVITE.split('+')[-1]
        try:
            updates = await safe_call(lambda: self.client(ImportChatInviteRequest(invite_hash)),
                                          action="log_join_group", protection=self.protection, kind="log")
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

    def start_worker(self):
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_worker())

    async def stop_worker(self):
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        self._worker = None

    async def _run_worker(self):
        while True:
            action, error, pin = await self._queue.get()
            try:
                await self._deliver(action, error, pin)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[{self.label}] Воркер логов: {e}")
            await asyncio.sleep(1.2)  # темп: не упираемся во FloodWait группы

    async def log(self, action: str, error: str = None, pin: bool = False):
        """Основной метод: 'время / действие / ошибка'. Возвращается сразу —
        консоль пишем тут же (Render-логи), Supabase и группу — воркер."""
        status = f"❌ ОШИБКА: {error}" if error else "✅ OK"
        (logger.error if error else logger.info)(f"[{self.label}] {action} | {status}")
        item = (action, error, pin)
        if self._worker is None:
            # воркер ещё не запущен (ранний старт) — не теряем запись
            self.start_worker()
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()  # выбрасываем самую старую
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                pass

    async def _deliver(self, action: str, error: str, pin: bool):
        line = f"🕐 {_now_str()} [{self.label}]\n{action}\n" + (f"❌ ОШИБКА: {error}" if error else "✅ OK")
        if self.db:
            await self.db.log("ERROR" if error else "INFO", "telegram", action, error=error)

        if not self.group_entity:
            return
        try:
            msg = await safe_call(
                lambda: self.client.send_message(self.group_entity, line),
                action="log_to_group", protection=self.protection, kind="log",
            )
            if pin or error:
                try:
                    await safe_call(lambda: self.client.pin_message(self.group_entity, msg, notify=bool(error)),
                                    action="log_pin", protection=self.protection, kind="log", max_retries=2)
                except Exception as e:
                    logger.warning(f"[{self.label}] Не удалось закрепить сообщение: {e}")
        except Exception as e:
            logger.error(f"[{self.label}] Ошибка отправки в группу логов: {e}")

    async def milestone(self, stars: float):
        """Закреплённое сообщение о достижении звёздной вехи."""
        text = f"🌟 Накоплено {stars:.2f}⭐ — можно выводить!"
        await self.log(text, pin=True)
