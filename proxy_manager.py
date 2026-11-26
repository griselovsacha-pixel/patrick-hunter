"""
ProxyManager — маршрут аккаунта в Telegram: через прокси или напрямую (с IP Render).

Режим хранится в БД (настройка аккаунта «proxy_mode») и меняется из админки без деплоя:
  auto — по умолчанию. Идём через прокси; если он не работает (несколько проверок подряд
         или клиент не может подключиться) — САМИ переходим на IP Render. Вернуться на
         прокси можно, только когда он стабильно здоров FAILBACK_AFTER_SEC (без «прыжков»
         туда-сюда: частая смена IP у сессии — плохо для аккаунта);
  on   — только прокси, без аварийного перехода;
  off  — только напрямую (прокси выключен), что бы ни происходило.

СТРОГИЙ режим (ультра-прогрев хрупких аккаунтов, например с азиатскими номерами): аварийный переход на
IP Render ЗАПРЕЩЁН — внезапная смена страны IP у сессии почти гарантированно приводит к заморозке.
Если прокси временно мёртв, аккаунт ждёт его возвращения, а не «прыгает» на другой IP.

Адрес прокси: из админки (настройка «proxy_url») либо из ACCOUNT_<N>_PROXY (proxy_conf.py).

Переключение «на лету»: client.set_proxy() + переподключение. Пока смена идёт, аккаунт 2–5 с
не на связи — Telethon потом сам догоняет пропущенные обновления (catch_up).
"""
import asyncio
import logging
import random
import time
from datetime import datetime, timezone

import proxy_check
from proxy_conf import env_proxy_url, parse_proxy_url, proxy_tuple

logger = logging.getLogger(__name__)

MODES = ("auto", "on", "off")
MODE_TITLES = {"auto": "авто", "on": "только прокси", "off": "напрямую (Render)"}

FAILOVER_AFTER = 3            # столько проверок подряд «прокси не работает» -> уходим на Render
FAILBACK_AFTER_SEC = 600      # прокси должен быть здоров столько, чтобы вернуться на него
MIN_SWITCH_GAP_SEC = 300      # не возвращаемся на прокси чаще, чем раз в N секунд
NORMAL_INTERVAL_SEC = 300     # проверка, когда всё хорошо
FAST_INTERVAL_SEC = 20        # проверка, когда с прокси проблемы
PREFLIGHT_TRIES = 2


class ProxyManager:
    def __init__(self, number: int, client, label: str, sched, tg_logger=None, is_live=None):
        self.number = number
        self.client = client
        self.label = label
        self.sched = sched
        self.tg_logger = tg_logger
        self.is_live = is_live or (lambda: False)   # аккаунт уже запущен (можно переподключать клиента)

        self.route = None             # "proxy" | "direct" | None — пока не выбран
        self.failed_over = False      # режим auto: ушли на Render из-за аварии прокси
        self.events: list[dict] = []  # последние смены маршрута (для админки)

        self._fail = 0
        self._ok_since: float | None = None
        self._last_switch = 0.0
        self._switch_lock = asyncio.Lock()
        self._check_lock = asyncio.Lock()
        self.state = {**proxy_check.describe_proxy(self.url), "status": "unknown", "checked_at": None}

    # ------------------------------------------------------------------ настройки
    @property
    def strict(self) -> bool:
        """Ультра-прогрев + настроен прокси: аварийный переход на IP Render запрещён."""
        try:
            return bool(self.sched.ultra_active and self.has_proxy)
        except Exception:
            return False

    @property
    def mode(self) -> str:
        m = self.sched.setting("proxy_mode", "auto")
        return m if m in MODES else "auto"

    @property
    def url(self) -> str:
        return (self.sched.setting("proxy_url") or env_proxy_url(self.number) or "").strip()

    @property
    def url_source(self):
        if (self.sched.setting("proxy_url") or "").strip():
            return "admin"
        return "env" if env_proxy_url(self.number) else None

    @property
    def has_proxy(self) -> bool:
        return parse_proxy_url(self.url) is not None

    def target_route(self) -> str:
        """Куда аккаунт ДОЛЖЕН идти при текущих настройках."""
        if self.mode == "off" or not self.has_proxy:
            return "direct"
        if self.mode == "on":
            return "proxy"
        return "direct" if self.failed_over else "proxy"

    # ------------------------------------------------------------------ события
    def _event(self, text: str, notify: bool = True):
        self.events.append({"at": datetime.now(timezone.utc).isoformat(), "text": text})
        self.events = self.events[-10:]
        logger.warning("[%s] прокси: %s", self.label, text)
        if notify and self.tg_logger is not None:
            try:
                asyncio.get_running_loop().create_task(self.tg_logger.log(f"🔀 Маршрут: {text}"))
            except RuntimeError:
                pass

    # ------------------------------------------------------------------ применение маршрута
    def _apply_to_client(self, route: str):
        self.client.set_proxy(proxy_tuple(self.url) if route == "proxy" else None)

    async def _preflight(self) -> bool:
        """Быстрая проверка, что через прокси вообще достаётся до Telegram."""
        for i in range(PREFLIGHT_TRIES):
            try:
                await asyncio.wait_for(proxy_check.tcp_through_proxy(self.url, with_ip=False), 14)
                return True
            except Exception as e:
                logger.info("[%s] прокси: preflight %d/%d не прошёл: %r", self.label, i + 1, PREFLIGHT_TRIES, e)
                await asyncio.sleep(1.5)
        return False

    async def initial_route(self) -> str:
        """Вызывать ПЕРЕД первым подключением клиента. Если прокси мёртв — не зависаем в
        бесконечных повторах старта, а стартуем с IP Render (режим auto)."""
        if self.mode == "auto" and self.has_proxy and not self.failed_over and not self.strict:
            if not await self._preflight():
                self.failed_over = True
                self._event("прокси не отвечает при старте — стартую с IP Render (проверяю прокси дальше)")
        route = self.target_route()
        self._apply_to_client(route)
        self.route = route
        return route

    async def _reconnect(self):
        c = self.client
        try:
            await asyncio.wait_for(c.disconnect(), 15)
        except Exception as e:
            logger.debug("[%s] прокси: disconnect: %r", self.label, e)
        last = None
        for _ in range(3):
            try:
                await asyncio.wait_for(c.connect(), 30)
                last = None
                break
            except Exception as e:
                last = e
                await asyncio.sleep(3)
        if last is not None:
            raise ConnectionError(f"не удалось переподключиться: {type(last).__name__}")
        try:
            await c.catch_up()
        except Exception as e:
            logger.debug("[%s] прокси: catch_up: %r", self.label, e)

    async def switch(self, route: str, reason: str, *, force: bool = False) -> bool:
        """Сменить маршрут. force — применить заново, даже если маршрут тот же (сменился адрес)."""
        async with self._switch_lock:
            if route == self.route and not force:
                return False
            old = self.route
            self._apply_to_client(route)
            self.route = route
            self._last_switch = time.monotonic()
            self._fail = 0
            self._ok_since = None
            names = {"proxy": "прокси", "direct": "IP Render"}
            self._event(f"{names.get(old, '—')} → {names[route]}: {reason}")
            if self.is_live():
                await self._reconnect()
            return True

    # ------------------------------------------------------------------ команды из админки
    async def set_mode(self, mode: str) -> bool:
        """-> True, если настройка сохранена в БД (иначе действует до перезапуска)."""
        if mode not in MODES:
            raise ValueError("режим: auto | on | off")
        persisted = await self.sched.set_settings(proxy_mode=mode)
        if mode == "auto":
            self.failed_over = False
            if self.has_proxy and self.route != "proxy" and not self.strict and not await self._preflight():
                self.failed_over = True   # прокси не отвечает — остаёмся на Render, дальше следим
        await self.switch(self.target_route(), f"режим «{MODE_TITLES[mode]}» (админка)")
        await self.refresh()
        return persisted

    async def set_url(self, url: str) -> bool:
        """Задать адрес прокси из админки (пусто — убрать и вернуться к ACCOUNT_<N>_PROXY)."""
        url = (url or "").strip()
        if url and not parse_proxy_url(url):
            raise ValueError("неверный формат, нужно socks5://user:pass@host:port (или socks4://, http://)")
        persisted = await self.sched.set_settings(proxy_url=url or None)
        self.failed_over = False
        if self.mode == "auto" and self.has_proxy and not self.strict and not await self._preflight():
            self.failed_over = True
        await self.switch(self.target_route(), "адрес прокси изменён (админка)", force=True)
        await self.refresh()
        return persisted

    # ------------------------------------------------------------------ проверка и решения
    async def refresh(self) -> dict:
        async with self._check_lock:   # ручная проверка и фоновая не пересекаются
            st = await proxy_check.check_proxy(self.url, self.client)
            self.state = st
            if st["status"] in ("dead", "slow"):
                logger.warning("[%s] прокси: %s (%s)", self.label, st["status"], st.get("error"))
            self._observe(st)
            return st

    def _observe(self, st: dict):
        ok = st["status"] in ("ok", "slow")
        if self.route == "proxy":
            # прокси «отвечает по TCP», но клиент аккаунта не может подключиться — тоже авария
            bad = st["status"] == "dead" or (self.is_live() and not self.client.is_connected())
            self._fail = self._fail + 1 if bad else 0
        elif self.route == "direct" and self.failed_over:
            if ok:
                if self._ok_since is None:
                    self._ok_since = time.monotonic()
            else:
                self._ok_since = None

    async def _decide(self):
        target = self.target_route()
        if self.mode != "auto":
            if target != self.route:
                await self.switch(target, f"режим «{MODE_TITLES[self.mode]}»")
            return
        if not self.has_proxy:
            return
        now = time.monotonic()
        if self.strict:
            if self.route == "direct" and self.failed_over:   # ушли на Render до включения ультра — возвращаемся
                self.failed_over = False
                await self.switch("proxy", "ультра-прогрев: возвращаюсь на прокси (смена IP недопустима)")
            return
        if self.route == "proxy" and self._fail >= FAILOVER_AFTER:
            self.failed_over = True
            await self.switch("direct", f"прокси не работает ({self._fail} проверок подряд) — перехожу на IP Render")
        elif (self.route == "direct" and self.failed_over and self._ok_since
              and now - self._ok_since >= FAILBACK_AFTER_SEC and now - self._last_switch >= MIN_SWITCH_GAP_SEC):
            self.failed_over = False
            await self.switch("proxy", "прокси снова стабильно работает — возвращаюсь")

    async def on_client_down(self) -> bool:
        """Сторож аккаунта: клиент не подключается уже ~1.5 минуты. Если идём через прокси в режиме
        auto — немедленно уходим на IP Render (switch сам переподключит). -> True, если сменили."""
        if self.strict:
            self._event("клиент не подключается через прокси, но строгий режим ультра-прогрева запрещает "
                        "переход на IP Render — жду возвращения прокси", notify=False)
            return False
        if self.mode == "auto" and self.has_proxy and self.route == "proxy":
            self.failed_over = True
            return await self.switch("direct", "клиент не подключается через прокси — перехожу на IP Render")
        return False

    def _interval(self) -> float:
        troubled = (self.route == "proxy" and self._fail > 0) or (self.route == "direct" and self.failed_over)
        return (FAST_INTERVAL_SEC if troubled else NORMAL_INTERVAL_SEC) * random.uniform(0.9, 1.1)

    async def run_forever(self):
        await asyncio.sleep(random.uniform(5, 20))   # не в момент старта аккаунта
        while True:
            try:
                await self.refresh()
                await self._decide()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[%s] проверка прокси: %r", self.label, e)
            await asyncio.sleep(self._interval())

    # ------------------------------------------------------------------ для админки
    async def public(self) -> dict:
        """Состояние для админки (без логина/пароля)."""
        st = dict(self.state)
        st.update(describe_for_ui(self))
        if self.route == "direct" or not self.has_proxy:
            st["direct_ip"] = await proxy_check.direct_exit_ip()
        return st


def describe_for_ui(m: ProxyManager) -> dict:
    return {
        "mode": m.mode, "mode_title": MODE_TITLES[m.mode], "strict": m.strict,
        "route": m.route, "failed_over": m.failed_over,
        "url_source": m.url_source, "connected": bool(m.client.is_connected()),
        "events": m.events[-5:],
        "failback_sec": FAILBACK_AFTER_SEC,
    }
