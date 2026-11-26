"""
MiniAppRunner — настоящий запуск Telegram Mini App от имени аккаунта.

Раньше «запуск мини-аппа» был StartBotRequest(start_param) + пауза: приложение
по факту не открывалось, спонсор ничего не видел. Теперь:

1. Открываем приложение тем же MTProto-запросом, что и официальный клиент:
   - t.me/bot/short?startapp=X -> messages.RequestAppWebView
   - t.me/bot?startapp=X       -> messages.RequestMainWebView (нужен свежий Telethon)
   - кнопка web_view в сообщении -> messages.RequestWebView / RequestSimpleWebView
2. Из ответа берём url и вытаскиваем initData (#tgWebAppData=...).
3. Если Telegram вернул query_id — держим сессию живой через
   messages.ProlongWebView каждые ~50 с, пока приложение «открыто».
   (Для RequestAppWebView query_id не выдаётся — продлевать там нечего.)
4. Если для бота/хоста зарегистрирован обработчик (miniapp_handlers.py) —
   вызываем его: он делает HTTP-запросы к бэкенду спонсора с initData.
5. Выдерживаем «время пребывания» (как раньше 25–35 с), отдавая bot_lock,
   чтобы промокод не ждал.

Telethon импортируется лениво, чтобы разбор ссылок/initData можно было
тестировать без установленных зависимостей.
"""
import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlsplit
import re

import aiohttp

from config import (
    MINIAPP_PLATFORM,
    MINIAPP_DWELL_RANGE,
    MINIAPP_PROLONG_INTERVAL_SEC,
    MINIAPP_HANDLER_TIMEOUT_SEC,
)

logger = logging.getLogger(__name__)

_APP_LINK_RE = re.compile(
    r'(?:https?://)?t\.me/([A-Za-z0-9_]{4,})(?:/([A-Za-z][A-Za-z0-9_]*))?'
    r'\?(?:[^#\s]*&)?startapp(?:=([^&#\s]*))?(?=[&#\s]|$)',
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Чистые функции (без Telethon)
# --------------------------------------------------------------------------
def parse_app_link(url: str):
    """'https://t.me/bot/app?startapp=abc' -> ('bot', 'app', 'abc').
    Главное приложение бота (без short name): ('bot', None, 'abc').
    Не похоже на ссылку мини-аппа -> None."""
    m = _APP_LINK_RE.search(url or "")
    if not m:
        return None
    bot, short, start_param = m.group(1), m.group(2), m.group(3) or ""
    return bot, short, start_param


def extract_init_data(url: str) -> Optional[str]:
    """Достаёт initData из #tgWebAppData=... (уже декодированную один раз —
    именно в таком виде её ждут бэкенды: query_id=...&user=%7B...&hash=...)."""
    if not url:
        return None
    for part in urlsplit(url).fragment.split("&"):
        if part.startswith("tgWebAppData="):
            return unquote(part[len("tgWebAppData="):])
    return None


class ChallengeDetected(Exception):
    """Бэкенд/страница ответили проверкой Cloudflare (не человек — не пускаем).
    Обходить её мы не пытаемся: задание пропускается."""


def is_challenge(status: int, headers, body_head: str) -> bool:
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True
    if status in (403, 429, 503) and str(headers.get("server", "")).lower().startswith("cloudflare"):
        b = (body_head or "").lower()
        return any(m in b for m in ("just a moment", "cf-chl", "challenge-platform", "turnstile", "attention required"))
    return False


@dataclass
class HttpResult:
    status: int
    text: str
    json: object = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class MiniAppSession:
    bot: str                         # username бота ('' если неизвестен)
    url: str                         # полный url веб-вью (с #tgWebAppData)
    init_data: Optional[str]
    query_id: Optional[int] = None   # есть только у RequestWebView
    short_name: Optional[str] = None
    start_param: str = ""
    peer: object = None              # InputPeer для ProlongWebView
    bot_user: object = None          # InputUser для ProlongWebView
    opened_at: float = field(default_factory=time.monotonic)

    @property
    def page_url(self) -> str:
        """URL без фрагмента — initData тут нет, это можно писать в логи."""
        s = urlsplit(self.url)
        return f"{s.scheme}://{s.netloc}{s.path}"

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc.lower()

    @property
    def fields(self) -> dict:
        return dict(parse_qsl(self.init_data or "", keep_blank_values=True))

    @property
    def user(self) -> dict:
        try:
            return json.loads(self.fields.get("user", "{}"))
        except ValueError:
            return {}

    @property
    def user_id(self) -> Optional[int]:
        return self.user.get("id")


class MiniAppContext:
    """Всё, что нужно обработчику конкретного приложения."""

    def __init__(self, runner, session: MiniAppSession, http: aiohttp.ClientSession):
        self.runner = runner
        self.session = session
        self.http = http
        self.label = runner.label

    @property
    def init_data(self) -> str:
        return self.session.init_data or ""

    def tma_headers(self, **extra) -> dict:
        """Самый частый формат авторизации: Authorization: tma <initData>."""
        return {"Authorization": f"tma {self.init_data}", **extra}

    async def fetch(self, method: str, url: str, **kw) -> HttpResult:
        """HTTP-запрос к бэкенду приложения. Если в ответ пришла проверка
        Cloudflare — бросает ChallengeDetected (раннер пропустит задание).
        Используй его вместо ctx.http.request(...)."""
        async with self.http.request(method, url, **kw) as r:
            text = await r.text()
            if is_challenge(r.status, r.headers, text[:4000]):
                raise ChallengeDetected(f"{method} {url} -> {r.status}")
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            return HttpResult(r.status, text, data)

    async def log(self, text: str, error: str = None):
        await self.runner.tg_logger.log(f"🧩 [{self.session.bot or self.session.host}] {text}", error=error)

    async def sleep(self, lo: float, hi: float):
        """Пауза с отдачей bot_lock (как core.yield_sleep)."""
        await self.runner.core.yield_sleep(lo, hi)


# --------------------------------------------------------------------------
# Раннер
# --------------------------------------------------------------------------
class MiniAppRunner:
    def __init__(self, client, core, tg_logger, label: str):
        self.client = client
        self.core = core
        self.tg_logger = tg_logger
        self.label = label
        self._seen_unhandled = set()
        self._warned_main = False
        self.last_handled = None  # итог обработчика последнего запуска: None — обработчика нет

    # ---- открытие ---------------------------------------------------------
    async def open_link(self, url: str) -> Optional[MiniAppSession]:
        parsed = parse_app_link(url)
        if not parsed:
            return None
        bot, short, start_param = parsed

        from telethon import utils
        from telethon.tl import functions, types

        bot_peer = await self.client.get_input_entity(bot)
        bot_user = utils.get_input_user(bot_peer)

        if short:
            res = await self.core.api(
                lambda: self.client(functions.messages.RequestAppWebViewRequest(
                    peer=bot_peer,
                    app=types.InputBotAppShortName(bot_id=bot_user, short_name=short),
                    platform=MINIAPP_PLATFORM,
                    write_allowed=True,
                    start_param=start_param or None,
                )),
                action="request_app_webview",
            )
        else:
            req_cls = getattr(functions.messages, "RequestMainWebViewRequest", None)
            if req_cls is None:
                logger.warning(
                    "[%s] t.me/%s?startapp: главное приложение бота, а в этой версии Telethon нет "
                    "RequestMainWebViewRequest — обновите telethon (см. requirements.txt)",
                    self.label, bot,
                )
                if not self._warned_main:
                    self._warned_main = True
                    await self.tg_logger.log(
                        "⚠️ Ссылки t.me/бот?startapp=… (главное приложение) не открываются: "
                        "обновите telethon (в requirements.txt нужен >=1.40)", error="старый telethon")
                return None
            res = await self.core.api(
                lambda: self.client(req_cls(
                    peer=bot_peer, bot=bot_user, platform=MINIAPP_PLATFORM,
                    start_param=start_param or None,
                )),
                action="request_main_webview",
            )

        return self._session_from(res, bot=bot, short=short, start_param=start_param,
                                  peer=bot_peer, bot_user=bot_user)

    async def open_button(self, msg, btn) -> Optional[MiniAppSession]:
        """Кнопка web_view / simple_web_view из сообщения бота."""
        from telethon import utils
        from telethon.tl import functions

        raw = getattr(btn, "button", btn)
        kind = type(raw).__name__
        url = getattr(raw, "url", None)
        if kind not in ("KeyboardButtonWebView", "KeyboardButtonSimpleWebView") or not url:
            return None

        chat = await msg.get_input_chat()
        sender = await msg.get_input_sender()
        bot_user = utils.get_input_user(sender)
        try:
            bot_username = (await msg.get_sender()).username or ""
        except Exception:
            bot_username = ""

        if kind == "KeyboardButtonWebView":
            res = await self.core.api(
                lambda: self.client(functions.messages.RequestWebViewRequest(
                    peer=chat, bot=bot_user, platform=MINIAPP_PLATFORM, url=url,
                )),
                action="request_webview",
            )
        else:
            res = await self.core.api(
                lambda: self.client(functions.messages.RequestSimpleWebViewRequest(
                    bot=bot_user, platform=MINIAPP_PLATFORM, url=url,
                )),
                action="request_simple_webview",
            )
        return self._session_from(res, bot=bot_username, short=None, start_param="",
                                  peer=chat, bot_user=bot_user)

    @staticmethod
    def _session_from(res, *, bot, short, start_param, peer, bot_user) -> MiniAppSession:
        url = getattr(res, "url", "") or ""
        return MiniAppSession(
            bot=bot, short_name=short, start_param=start_param,
            url=url, init_data=extract_init_data(url),
            query_id=getattr(res, "query_id", None),
            peer=peer, bot_user=bot_user,
        )

    # ---- keepalive --------------------------------------------------------
    async def _keepalive(self, s: MiniAppSession):
        from telethon.tl.functions.messages import ProlongWebViewRequest
        while True:
            await asyncio.sleep(MINIAPP_PROLONG_INTERVAL_SEC)
            try:
                await self.core.api(
                    lambda: self.client(ProlongWebViewRequest(
                        peer=s.peer, bot=s.bot_user, query_id=s.query_id,
                    )),
                    action="prolong_webview",
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # QueryIdInvalid и т.п. — продлевать больше нечего
                logger.warning("[%s] ProlongWebView (%s): %s", self.label, s.bot, e)
                return

    # ---- полный сценарий --------------------------------------------------
    async def run(self, session: MiniAppSession) -> str:
        """Сессия уже открыта: keepalive + обработчик + время пребывания.
        Вызывать из кода, держащего bot_lock (как и раньше handle_startapp).
        Возвращает 'ok' или 'blocked' (Cloudflare-проверка — задание пропускаем)."""
        from miniapp_handlers import find_handler  # поздний импорт: реестр заполняется при импорте

        keepalive = None
        if session.query_id is not None:
            keepalive = asyncio.create_task(self._keepalive(session))

        handled = None
        self.last_handled = None
        who = session.bot or session.host
        try:
            target = random.uniform(*MINIAPP_DWELL_RANGE)
            handler = find_handler(session)
            if handler is None:
                await self._log_unhandled(session)
            else:
                try:
                    handled = await self._run_handler(handler, session)
                except ChallengeDetected as e:
                    await self.tg_logger.log(
                        f"🛡 Cloudflare-проверка у {who} — не обхожу, задание пропускаю", error=str(e)[:100]
                    )
                    return "blocked"

            remaining = max(target - (time.monotonic() - session.opened_at), random.uniform(2, 4))
            await self.core.yield_sleep(remaining, remaining)
        finally:
            if keepalive:
                keepalive.cancel()

        self.last_handled = handled
        extra = "" if handled is None else (" · обработчик ✅" if handled else " · обработчик ❌")
        await self.tg_logger.log(f"🧩 Мини-апп {who} открыт{extra}")
        return "ok"

    async def _run_handler(self, handler, session) -> bool:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=MINIAPP_HANDLER_TIMEOUT_SEC)
            ) as http:
                ctx = MiniAppContext(self, session, http)
                return bool(await asyncio.wait_for(handler.run(ctx), MINIAPP_HANDLER_TIMEOUT_SEC))
        except (asyncio.CancelledError, ChallengeDetected):
            raise
        except Exception as e:
            await self.tg_logger.log(
                f"Обработчик {type(handler).__name__} упал", error=f"{type(e).__name__}: {str(e)[:120]}"
            )
            return False

    async def _log_unhandled(self, s: MiniAppSession):
        """Нет обработчика — пишем, что именно открыли (без initData/hash),
        чтобы по этим данным можно было написать обработчик. Один раз на приложение."""
        key = (s.bot, s.short_name, s.host)
        if key in self._seen_unhandled:
            return
        self._seen_unhandled.add(key)
        await self.tg_logger.log(
            f"🧩 Мини-апп без обработчика: bot=@{s.bot or '?'} app={s.short_name or '(main)'} "
            f"startapp={s.start_param[:40] or '-'}\n🌐 {s.page_url}\n"
            f"🔁 keepalive: {'да' if s.query_id is not None else 'нет (нет query_id)'}"
        )

    # ---- удобные обёртки --------------------------------------------------
    async def run_link(self, url: str) -> str:
        """'ok' | 'blocked' | 'failed' (не смогли открыть — можно откатиться на старый путь)."""
        s = await self.open_link(url)
        return await self.run(s) if s else "failed"

    async def run_button(self, msg, btn) -> str:
        s = await self.open_button(msg, btn)
        return await self.run(s) if s else "failed"
