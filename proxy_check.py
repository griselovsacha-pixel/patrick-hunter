"""
Проверка прокси аккаунта для админки (чистые функции, состояние хранит proxy_manager.py).

Что проверяем:
  1. TCP через прокси до дата-центра Telegram — прокси жив и пропускает MTProto-порт;
     время соединения = задержка до прокси;
  2. внешний IP через прокси (обычный HTTP до api.ipify.org) — «под каким IP видит
     Telegram этот аккаунт»; best-effort: если прокси не пускает порт 80, статус
     от этого не страдает;
  3. пинг по РЕАЛЬНОМУ соединению аккаунта (PingRequest) — то, что реально
     влияет на скорость ловли чеков.

Статусы прокси: none (не задан) | ok | slow | dead | unknown (ещё не проверяли).
Статус относится к САМОМУ прокси, независимо от того, идёт ли аккаунт сейчас через него
или (из-за режима/аварии) напрямую с IP Render. Логин и пароль никуда не отдаются.
"""
import asyncio
import logging
import random
import time
from datetime import datetime, timezone

from proxy_conf import parse_proxy_url

logger = logging.getLogger(__name__)

TELEGRAM_DC = ("149.154.167.51", 443)   # DC2 — для проверки достижимости через прокси
IP_ECHO_HOST = "api.ipify.org"
CONNECT_TIMEOUT_SEC = 10
SLOW_MS = 1500

_direct_ip_cache = {"ip": None, "at": 0.0}


def describe_proxy(raw_url: str) -> dict:
    """Публичное описание прокси (без логина/пароля)."""
    raw = (raw_url or "").strip()
    if not raw:
        return {"configured": False}
    p = parse_proxy_url(raw)
    if not p:
        return {"configured": True, "valid": False, "scheme": None, "host": None, "port": None,
                "auth": False, "display": "неверный формат"}
    return {
        "configured": True, "valid": True, "scheme": p["scheme"], "host": p["host"], "port": p["port"],
        "auth": bool(p["user"]), "display": f"{p['scheme']}://{p['host']}:{p['port']}",
    }


async def direct_exit_ip(max_age_sec: float = 600) -> str | None:
    """Внешний IP самого сервера (Render) — для админки, когда аккаунт идёт напрямую."""
    now = time.monotonic()
    if _direct_ip_cache["ip"] and now - _direct_ip_cache["at"] < max_age_sec:
        return _direct_ip_cache["ip"]
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(f"https://{IP_ECHO_HOST}") as r:
                ip = (await r.text()).strip()
        if ip and len(ip) <= 45 and all(c in "0123456789abcdefABCDEF.:" for c in ip):
            _direct_ip_cache.update(ip=ip, at=now)
            return ip
    except Exception as e:
        logger.debug("direct exit ip: %r", e)
        _direct_ip_cache["at"] = now - max_age_sec + 60   # не долбим сервис: повтор не раньше чем через минуту
    return _direct_ip_cache["ip"]


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


async def tcp_through_proxy(raw_url: str, dest=TELEGRAM_DC, with_ip: bool = True) -> tuple[float, str | None]:
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
    ip = None
    if with_ip:
        try:
            ip = await _exit_ip(proxy)
        except Exception as e:
            logger.debug("exit ip через прокси: %r", e)
    return ms, ip


async def client_ping(client) -> float | None:
    """Пинг (мс) по живому соединению аккаунта; None — соединения нет."""
    if not client.is_connected():
        return None
    from telethon.tl.functions import PingRequest

    t = time.monotonic()
    await asyncio.wait_for(client(PingRequest(ping_id=random.getrandbits(62))), CONNECT_TIMEOUT_SEC)
    return (time.monotonic() - t) * 1000


async def check_proxy(raw_url: str, client=None, *, dest=TELEGRAM_DC) -> dict:
    """Полная проверка. Не бросает исключений — всегда возвращает словарь состояния."""
    info = describe_proxy(raw_url)
    state = {
        **info,
        "status": "unknown",
        "latency_ms": None,      # до Telegram через прокси
        "client_ping_ms": None,  # по живому соединению аккаунта
        "exit_ip": None,
        "error": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    async def _ping():
        if client is None:
            return
        try:
            p = await client_ping(client)
            state["client_ping_ms"] = round(p) if p is not None else None
        except Exception as e:
            state["error"] = f"пинг клиента: {type(e).__name__}"

    # --- прокси не задан --------------------------------------------------
    if not info["configured"]:
        state["status"] = "none"
        await _ping()
        return state

    if not info["valid"]:
        state["status"] = "dead"
        state["error"] = "неверный формат (нужно socks5://user:pass@host:port)"
        await _ping()
        return state

    # --- прокси ---------------------------------------------------------
    try:
        ms, ip = await tcp_through_proxy(raw_url, dest)
        state["latency_ms"] = round(ms)
        state["exit_ip"] = ip
        state["status"] = "ok"
    except Exception as e:
        state["status"] = "dead"
        state["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        await _ping()
        return state

    await _ping()
    worst = max(state["latency_ms"] or 0, state["client_ping_ms"] or 0)
    if worst > SLOW_MS:
        state["status"] = "slow"
    return state
