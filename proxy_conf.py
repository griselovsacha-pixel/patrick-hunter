"""
Прокси для аккаунта Telethon: переменная ACCOUNT_<N>_PROXY, например
  ACCOUNT_1_PROXY=socks5://user:pass@host:port
Без переменной аккаунт работает напрямую, как раньше.
Нужен пакет python-socks (уже указан в requirements.txt).
Прокси не логируется целиком: в логах только хост:порт.
"""
import logging
import os
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)


def proxy_for(account_number: int):
    raw = (os.getenv(f"ACCOUNT_{account_number}_PROXY") or "").strip()
    if not raw:
        return None
    u = urlparse(raw)
    scheme = (u.scheme or "").lower()
    if scheme not in ("socks5", "socks4", "http") or not u.hostname or not u.port:
        logger.warning("ACCOUNT_%s_PROXY: неверный формат (нужно socks5://user:pass@host:port)", account_number)
        return None
    from python_socks import ProxyType
    kind = {"socks5": ProxyType.SOCKS5, "socks4": ProxyType.SOCKS4, "http": ProxyType.HTTP}[scheme]
    logger.info("аккаунт %s: прокси %s %s:%s", account_number, scheme, u.hostname, u.port)
    return (kind, u.hostname, u.port, True, unquote(u.username or "") or None, unquote(u.password or "") or None)
