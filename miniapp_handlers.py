"""
Обработчики конкретных мини-аппов.

Универсального способа «засчитать визит» нет: каждый спонсор проверяет своё
(вызов /auth, /visit, /claim, определённый заголовок, пинг по таймеру…).
Поэтому на каждое приложение — свой небольшой класс.

Как добавить приложение:
1. Скопируй ExampleHandler ниже, поменяй `bots` (username бота, без @, в нижнем
   регистре) и/или `hosts` (домен веб-вью, из лога «Мини-апп без обработчика»).
2. В run(ctx) сделай запросы, которые делает само приложение при открытии
   (смотри вкладку Network в DevTools/HAR). initData: ctx.init_data,
   готовый заголовок: ctx.tma_headers().
3. Верни True, если бэкенд подтвердил визит, иначе False.
4. Зарегистрируй: register(MyHandler()).

Пока обработчика нет, раннер просто открывает приложение, держит сессию
(ProlongWebView, если Telegram выдал query_id) и пишет в лог группы, что именно
было открыто — по этой строке можно написать обработчик.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_BY_BOT: dict = {}
_BY_HOST: dict = {}


class MiniAppHandler:
    bots: tuple = ()    # usernames ботов в нижнем регистре, без @
    hosts: tuple = ()   # домены веб-вью в нижнем регистре

    async def run(self, ctx) -> bool:
        raise NotImplementedError


def register(handler: MiniAppHandler) -> MiniAppHandler:
    for b in handler.bots:
        _BY_BOT[b.lower().lstrip("@")] = handler
    for h in handler.hosts:
        _BY_HOST[h.lower()] = handler
    return handler


def find_handler(session) -> Optional[MiniAppHandler]:
    return _BY_BOT.get((session.bot or "").lower()) or _BY_HOST.get(session.host)


# ---------------------------------------------------------------------------
# ШАБЛОН (не зарегистрирован — просто пример формы обработчика)
# ---------------------------------------------------------------------------
class ExampleHandler(MiniAppHandler):
    bots = ("example_bot",)
    hosts = ("app.example.com",)

    async def run(self, ctx) -> bool:
        # ctx.fetch сам бросает ChallengeDetected, если ответила проверка Cloudflare —
        # раннер тогда пропустит задание (ловить это исключение в обработчике не нужно).
        r = await ctx.fetch("POST", "https://api.example.com/auth", headers=ctx.tma_headers())
        if not r.ok or not isinstance(r.json, dict):
            await ctx.log(f"auth -> HTTP {r.status}", error="auth")
            return False

        await ctx.sleep(3, 6)  # пауза с отдачей bot_lock
        r = await ctx.fetch(
            "POST", "https://api.example.com/visit",
            headers=ctx.tma_headers(**{"X-Token": r.json.get("token", "")}),
        )
        return r.ok


# register(ExampleHandler())   # <- раскомментируй, когда подставишь реальные адреса
