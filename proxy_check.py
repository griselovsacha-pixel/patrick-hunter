"""
Проверка прокси аккаунта для админки.

Что проверяем (у каждого аккаунта свой ACCOUNT_<N>_PROXY, см. proxy_conf.py):
  1. TCP через прокси до дата-центра Telegram — прокси жив и пропускает MTProto-порт;
     время соединения = задержка до прокси;
  2. внешний IP через прокси (обычный HTTP до api.ipify.org) — «под каким IP видит
     Telegram этот аккаунт»; best-effort: если прокси не пускает порт 80, статус
     от этого не страдает;
  3. пинг по РЕАЛЬНОМУ соединению аккаунта (PingRequest) — то, что реально
     влияет на скорость ловли чеков.

Статусы: none (прямое соединение) | ok | slow | dead | unknown (ещё не проверяли).
Логин и пароль прокси никуда не отдаются — только схема и host:port.
"""
import asyncio
import logging
import os
import random
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TELEGRAM_DC = ("149.154.167.51", 443)   # DC2 — для проверки достижимости через прокси
IP_ECHO_HOST = "api.ipify.org"
CONNECT_TIMEOUT_SEC = 10
SLOW_MS = 1500
CHECK_INTERVAL_SEC = 300


def proxy_url(account_number: int) -> str:
    return (os.getenv(f"ACCOUNT_{account_number}_PROXY") or "").strip()


def describe_proxy(account_number: int) -> dict:
    """Публичное описание прокси (без логина/пароля)."""
    raw = proxy_url(account_number)
    if not raw:
        return {"configured": False}
    u = urlparse(raw)
    scheme = (u.scheme or "").lower()
    valid = scheme in ("socks5", "socks4", "http") and bool(u.hostname) and bool(u.port)
    return {
        "configured": True,
        "valid": valid,
        "scheme": scheme or None,
        "host": u.hostname,
        "port": u.port,
        "auth": bool(u.username),
        "display": f"{scheme}://{u.hostname}:{u.port}" if valid else "неверный формат",
    }


async def _exit_ip(proxy) -> str | None:
    """Внешний IP через прокси простым HTTP-запросом (без TLS)."""
    stream = await proxy.connect(dest_host=IP_ECHO_HOST, dest_port=80, timeout=CONNECT_TIMEOUT_SEC)
    try:
        await stream.write(
            f"GET / HTTP/1.1\r\nHost: {IP_ECHO_HOST}\r\nConnection: close\r\nUser-Agent: curl/8\r\n\r\n".encode()
        )
        data = await asyncio.wait_for(stream.read(2048), CONNECT_TIMEOUT_SEC)
    finally:
        await stream.close()
    body = data.split(b"\r\n\r\n", 1)[-1].decode("ascii", "ignore").strip()
    ip = body.splitlines()[0].strip() if body else ""
    return ip if ip and len(ip) <= 45 and all(c in "0123456789abcdefABCDEF.:" for c in ip) else None


async def _tcp_through_proxy(raw_url: str, dest=TELEGRAM_DC) -> tuple[float, str | None]:
    """-> (мс до соединения с Telegram через прокси, внешний IP или None)."""
    from python_socks.async_.asyncio.v2 import Proxy

    proxy = Proxy.from_url(raw_url)
    t = time.monotonic()
    stream = await proxy.connect(dest_host=dest[0], dest_port=dest[1], timeout=CONNECT_TIMEOUT_SEC)
    ms = (time.monotonic() - t) * 1000
    try:
        await stream.close()
    except Exception:
        pass
    try:
        ip = await _exit_ip(proxy)
    except Exception as e:
        logger.debug("exit ip через прокси: %r", e)
        ip = None
    return ms, ip


async def _client_ping(client) -> float | None:
    """Пинг (мс) по живому соединению аккаунта; None — соединения нет."""
    if not client.is_connected():
        return None
    from telethon.tl.functions import PingRequest

    t = time.monotonic()
    await asyncio.wait_for(client(PingRequest(ping_id=random.getrandbits(62))), CONNECT_TIMEOUT_SEC)
    return (time.monotonic() - t) * 1000


async def check_proxy(account_number: int, client=None, *, dest=TELEGRAM_DC) -> dict:
    """Полная проверка. Не бросает исключений — всегда возвращает словарь состояния."""
    info = describe_proxy(account_number)
    state = {
        **info,
        "status": "unknown",
        "latency_ms": None,      # до Telegram через прокси
        "client_ping_ms": None,  # по живому соединению аккаунта
        "exit_ip": None,
        "error": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    # --- прямое соединение --------------------------------------------
    if not info["configured"]:
        state["status"] = "none"
        if client is not None:
            try:
                p = await _client_ping(client)
                state["client_ping_ms"] = round(p) if p is not None else None
            except Exception as e:
                state["error"] = repr(e)[:150]
        return state

    if not info["valid"]:
        state["status"] = "dead"
        state["error"] = "неверный формат (нужно socks5://user:pass@host:port)"
        return state

    # --- прокси ---------------------------------------------------------
    try:
        ms, ip = await _tcp_through_proxy(proxy_url(account_number), dest)
        state["latency_ms"] = round(ms)
        state["exit_ip"] = ip
        state["status"] = "ok"
    except Exception as e:
        state["status"] = "dead"
        state["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return state

    if client is not None:
        try:
            p = await _client_ping(client)
            state["client_ping_ms"] = round(p) if p is not None else None
            if p is None:
                state["error"] = "прокси отвечает, но клиент аккаунта не подключён"
        except Exception as e:
            state["error"] = f"пинг клиента: {type(e).__name__}"

    worst = max(state["latency_ms"] or 0, state["client_ping_ms"] or 0)
    if worst > SLOW_MS:
        state["status"] = "slow"
    return state


class ProxyMonitor:
    """Периодически проверяет прокси аккаунта и хранит последний результат."""

    def __init__(self, account_number: int, client, label: str):
        self.number = account_number
        self.client = client
        self.label = label
        self.state = {**describe_proxy(account_number), "status": "unknown", "checked_at": None}
        self._lock = asyncio.Lock()

    async def refresh(self) -> dict:
        async with self._lock:  # ручная проверка и фоновая не пересекаются
            self.state = await check_proxy(self.number, self.client)
            if self.state["status"] in ("dead", "slow"):
                logger.warning("[%s] прокси: %s (%s)", self.label, self.state["status"], self.state.get("error"))
            return self.state

    async def run_forever(self):
        await asyncio.sleep(random.uniform(5, 20))  # не в момент старта аккаунта
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[%s] проверка прокси: %r", self.label, e)
            await asyncio.sleep(CHECK_INTERVAL_SEC * random.uniform(0.9, 1.1))
