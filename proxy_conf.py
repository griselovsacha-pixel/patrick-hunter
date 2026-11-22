"""
Прокси аккаунта Telethon.

Адрес берётся (по приоритету):
  1. из админки — настройка аккаунта «proxy_url» (хранится в БД, меняется без деплоя);
  2. из переменной ACCOUNT_<N>_PROXY, например socks5://user:pass@host:port.
Без адреса аккаунт работает напрямую (с IP Render).

Включать/выключать прокси и менять режим (авто / только прокси / напрямую) — в админке,
см. proxy_manager.py. Прокси не логируется целиком: в логах только хост:порт.
Нужен пакет python-socks (есть в requirements.txt).
"""
import logging
import os
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

SCHEMES = ("socks5", "socks4", "http")


def env_proxy_url(account_number: int) -> str:
    return (os.getenv(f"ACCOUNT_{account_number}_PROXY") or "").strip()


def parse_proxy_url(raw: str):
    """-> dict(scheme, host, port, user, password) или None, если адрес пустой/неверный."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        u = urlparse(raw)
        scheme = (u.scheme or "").lower()
        if scheme not in SCHEMES or not u.hostname or not u.port:
            return None
        return {
            "scheme": scheme, "host": u.hostname, "port": u.port,
            "user": unquote(u.username or "") or None,
            "password": unquote(u.password or "") or None,
        }
    except ValueError:
        return None


def proxy_tuple(raw: str):
    """Прокси в формате Telethon (kind, host, port, rdns, user, password) или None."""
    p = parse_proxy_url(raw)
    if not p:
        return None
    from python_socks import ProxyType
    kind = {"socks5": ProxyType.SOCKS5, "socks4": ProxyType.SOCKS4, "http": ProxyType.HTTP}[p["scheme"]]
    return (kind, p["host"], p["port"], True, p["user"], p["password"])


def proxy_for(account_number: int):
    """Совместимость со старым кодом: прокси из env."""
    raw = env_proxy_url(account_number)
    if raw and not parse_proxy_url(raw):
        logger.warning("ACCOUNT_%s_PROXY: неверный формат (нужно socks5://user:pass@host:port)", account_number)
    return proxy_tuple(raw)
