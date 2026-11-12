"""Supabase persistence for Patrick Hunter."""
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Адрес проекта БЕЗ значения по умолчанию: старый проект удалён, нужен адрес нового
# (Supabase -> Project Settings -> API -> Project URL).
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")

# service_role (legacy, JWT «eyJ…») или новый secret-ключ («sb_secret_…»).
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

ACCOUNT_RETRY_SEC = 60


def supabase_headers(key: str) -> dict:
    """Новые secret-ключи (sb_secret_…) — не JWT и передаются только в apikey;
    legacy service_role (eyJ…) нужен ещё и в Authorization."""
    headers = {"apikey": key}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


class SupabaseStore:
    def __init__(self, label: str):
        self.label = label
        self.enabled = bool(
            SUPABASE_URL
            and SUPABASE_SERVICE_ROLE_KEY
        )
        self.account_id: Optional[int] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._last_resolve = 0.0

    async def start(self):
        if not self.enabled:
            logger.warning(
                "[%s] Supabase disabled: нужны переменные "
                "SUPABASE_URL и SUPABASE_SERVICE_ROLE_KEY",
                self.label,
            )
            return

        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )
        await self._resolve_account()

    async def _resolve_account(self) -> bool:
        """Находит (или создаёт) строку accounts этого аккаунта. Безопасно вызывать повторно."""
        self._last_resolve = time.monotonic()
        try:
            rows = await self._request(
                "GET",
                "/rest/v1/accounts",
                params={
                    "account_number":
                        f"eq.{self._account_number()}",
                    "select":
                        "id,account_number,name",
                },
            )

            if rows:
                self.account_id = int(rows[0]["id"])
            else:
                # Строки нет (БД новая и без DATABASE_URL) — создаём сами
                created = await self._request(
                    "POST",
                    "/rest/v1/accounts",
                    params={"on_conflict": "account_number"},
                    payload={
                        "account_number": self._account_number(),
                        "name": self.label,
                    },
                    extra_headers={
                        "Prefer": "return=representation,resolution=merge-duplicates",
                    },
                )
                if created:
                    self.account_id = int(created[0]["id"])
                    logger.info("[%s] Supabase: создана строка accounts", self.label)

        except Exception as e:
            logger.warning(
                "[%s] Supabase: не удалось получить строку аккаунта (%s) — повторю позже",
                self.label, str(e)[:200],
            )
        return self.account_id is not None

    async def _ensure_account(self) -> bool:
        """Если БД была недоступна/пуста на старте — раз в минуту пробуем снова,
        а не молчим до следующего деплоя."""
        if self.account_id is not None:
            return True
        if not self.enabled or not self.session:
            return False
        if time.monotonic() - self._last_resolve < ACCOUNT_RETRY_SEC:
            return False
        return await self._resolve_account()

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    def _account_number(self) -> int:
        digits = "".join(
            ch for ch in self.label
            if ch.isdigit()
        )
        return int(digits or "1")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params=None,
        payload=None,
        extra_headers=None,
    ):
        if not self.enabled or not self.session:
            return None

        headers = {
            **supabase_headers(SUPABASE_SERVICE_ROLE_KEY),
            "Content-Type": "application/json",
            **(extra_headers or {}),
        }

        async with self.session.request(
            method,
            SUPABASE_URL + path,
            headers=headers,
            params=params,
            json=payload,
        ) as resp:
            body = await resp.text()

            if resp.status >= 400:
                raise RuntimeError(
                    f"Supabase HTTP {resp.status}: "
                    f"{body[:500]}"
                )

            if not body:
                return None

            return json.loads(body)

    async def set_connected(self, connected: bool):
        if not await self._ensure_account():
            return

        try:
            payload = {
                "connected": connected,
            }

            if connected:
                payload["last_connected_at"] = (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                )

            await self._request(
                "PATCH",
                "/rest/v1/accounts",
                params={
                    "id":
                        f"eq.{self.account_id}"
                },
                payload=payload,
            )

        except Exception:
            logger.exception(
                "[%s] Supabase connection status "
                "update failed",
                self.label,
            )

    async def balance_snapshot(self, balance: float):
        """Сохраняет реальный баланс, прочитанный из Профиля."""
        if not await self._ensure_account():
            return

        try:
            await self._request(
                "POST",
                "/rest/v1/balance_snapshots",
                payload={
                    "account_id": self.account_id,
                    "balance": round(float(balance), 2),
                    "captured_at":
                        datetime.now(
                            timezone.utc
                        ).isoformat(),
                },
            )
        except Exception:
            logger.exception(
                "[%s] Balance snapshot write failed",
                self.label,
            )

    async def event(
        self,
        event_type: str,
        *,
        stars: float = 0,
        task_type=None,
        promo_code=None,
        status=None,
        error_reason=None,
    ):
        if not await self._ensure_account():
            return

        payload = {
            "p_account_id":
                self.account_id,
            "p_event_type":
                event_type,
            "p_stars":
                stars,
            "p_task_type":
                task_type,
            "p_promo_code":
                promo_code,
            "p_status":
                status,
            "p_error_reason":
                error_reason,
        }

        try:
            await self._request(
                "POST",
                "/rest/v1/rpc/record_patrick_event",
                payload=payload,
            )
        except Exception:
            logger.exception(
                "[%s] Supabase event write failed: %s",
                self.label,
                event_type,
            )

    async def log(
        self,
        level: str,
        event_type: str,
        message: str,
        error: str = None,
        metadata: dict[str, Any] | None = None,
    ):
        if not await self._ensure_account():
            return

        payload = {
            "account_id":
                self.account_id,
            "level":
                level.upper(),
            "event_type":
                event_type,
            "message":
                message[:4000],
            "error":
                error[:4000]
                if error
                else None,
            "metadata":
                metadata or {},
        }

        try:
            await self._request(
                "POST",
                "/rest/v1/app_logs",
                payload=payload,
            )
        except Exception:
            logger.exception(
                "[%s] Supabase log write failed",
                self.label,
            )

    async def knowledge(self, fingerprint, kind, title, text, data, account):
        """«Обучение»: запись о том, что отдал бот (knowledge.py). Ошибки глотаем."""
        if not self.enabled or not self.session:
            return
        try:
            await self._request(
                "POST",
                "/rest/v1/rpc/patrick_knowledge_upsert",
                payload={
                    "p_fingerprint": fingerprint,
                    "p_kind": kind,
                    "p_title": (title or "")[:200],
                    "p_text": (text or "")[:3000],
                    "p_data": data or {},
                    "p_account": account,
                },
            )
        except Exception as e:
            logger.debug("[%s] knowledge write failed: %s", self.label, e)
