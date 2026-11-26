"""
ChannelWatcher — ловит новые/изменённые посты канала(ов) ДВУМЯ путями:

  1. события Telethon (NewMessage/MessageEdited) — мгновенно, когда
     Telegram их присылает;
  2. свой опрос канала раз в CHANNEL_POLL_INTERVAL_SEC секунд.

Почему нужен второй путь: со StringSession на Render (после рестарта кэш
сущностей пуст) и для каналов, куда аккаунт формально не вступал, Telegram
не гарантирует доставку обновлений в реальном времени. Раньше вместо опроса
был "touch" канала раз в 20с, который сам по себе обработчик НЕ вызывает —
отсюда «код ловится только когда сам открою канал руками».

Оба пути приходят в один on_post(); дубли отсекаются по ключу
(канал, id поста, время правки, хэш текста), то есть один пост — одна
реакция, а правка поста (бот дописал код) — новая.
"""
import asyncio
import datetime
import logging
import random

from telethon import events
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest

from config import CHANNEL_POLL_INTERVAL_SEC
from utils import BoundedSet
from antiban import safe_call

logger = logging.getLogger(__name__)

# При старте посты старше этого возраста считаем уже обработанными ранее и
# молча запоминаем (иначе после каждого рестарта пытались бы заново
# активировать вчерашние коды).
STARTUP_MAX_POST_AGE_SEC = 120


class ChannelWatcher:
    def __init__(self, client, label: str, channels, on_post, tg_logger=None):
        self.client = client
        self.label = label
        self.channels = list(channels)
        self.on_post = on_post
        self.tg_logger = tg_logger
        self.protection = None   # AccountProtection — ставится из account.py
        self._seen = BoundedSet(400)
        self._tasks = []
        self._entities = {}
        self._handler = None

    # ------------------------------------------------------------------
    async def start(self):
        self._register_events()
        for channel in self.channels:
            await self._prepare(channel)
            self._tasks.append(asyncio.create_task(self._poll_loop(channel)))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        if self._handler is not None:
            try:
                self.client.remove_event_handler(self._handler)
            except Exception:
                pass
            self._handler = None

    # ------------------------------------------------------------------
    def _register_events(self):
        async def handle(event):
            await self._dispatch(event.message)

        self._handler = handle
        self.client.add_event_handler(handle, events.NewMessage(chats=self.channels))
        self.client.add_event_handler(handle, events.MessageEdited(chats=self.channels))

    async def _prepare(self, channel: str):
        """Резолвим канал и вступаем (идемпотентно) — без членства события по
        каналу не приходят. Ошибки не фатальны: опрос работает и так."""
        try:
            entity = await self.client.get_entity(channel)
            self._entities[channel] = entity
        except Exception as e:
            logger.warning(f"[{self.label}] не удалось получить канал {channel}: {e}")
            return
        try:
            await safe_call(lambda: self.client(JoinChannelRequest(entity)), action=f"join_watch:{channel}",
                            protection=self.protection, high=True)
        except UserAlreadyParticipantError:
            pass
        except Exception as e:
            logger.warning(f"[{self.label}] не удалось вступить в {channel}: {e}")

    def _poll_scale(self) -> float:
        """Опрос канала каждые ~3 с у каждого из 5 аккаунтов с одного IP — это ~100 запросов GetHistory в минуту.
        Основной путь — события; опрос лишь страховка. Хрупкие аккаунты (ультра-прогрев) опрашивают реже
        (их коды всё равно приходят от соседей через Hub), после FloodWait — тоже."""
        p = self.protection
        if p is None:
            return 1.0
        scale = 1.0
        if p.sched is not None and p.sched.ultra_active:
            scale *= 3.0
        if p.cooldown_left() > 0 or p.recovering_left() > 0:
            scale *= 2.0
        return scale

    # ------------------------------------------------------------------
    @staticmethod
    def _key(message):
        edit = int(message.edit_date.timestamp()) if message.edit_date else 0
        return (getattr(message, "chat_id", None), message.id, edit, hash(message.raw_text or ""))

    async def _dispatch(self, message, *, silent: bool = False):
        key = self._key(message)
        if key in self._seen:
            return
        self._seen.add(key)
        if silent:
            return
        try:
            await self.on_post(message)
        except Exception as e:
            logger.error(f"[{self.label}] on_post: {e!r}")

    async def _poll_loop(self, channel: str):
        first = True
        errors = 0
        while True:
            try:
                entity = self._entities.get(channel)
                if entity is None:
                    entity = await self.client.get_entity(channel)
                    self._entities[channel] = entity
                messages = await self.client.get_messages(entity, limit=5)
                now = datetime.datetime.now(datetime.timezone.utc)
                for m in reversed(list(messages)):  # от старых к новым
                    age = (now - (m.edit_date or m.date)).total_seconds()
                    await self._dispatch(m, silent=first and age > STARTUP_MAX_POST_AGE_SEC)
                first = False
                errors = 0
                await asyncio.sleep(CHANNEL_POLL_INTERVAL_SEC * self._poll_scale() * random.uniform(0.75, 1.25))
            except asyncio.CancelledError:
                raise
            except FloodWaitError as e:
                wait = min(getattr(e, "seconds", 30), 300) + 1
                logger.warning(f"[{self.label}] опрос {channel}: FloodWait {wait}с")
                if self.protection is not None:
                    self.protection.note_flood(f"read_watch:{channel}", wait)   # в «нагрев» аккаунта; сам опрос ждёт как раньше
                await asyncio.sleep(wait)
            except Exception as e:
                errors += 1
                self._entities.pop(channel, None)
                logger.warning(f"[{self.label}] опрос {channel}: {e!r}")
                await asyncio.sleep(min(5 * errors, 60))
