"""Supabase persistence for Patrick Hunter."""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv(
    "SUPABASE_URL",
    "https://wqzpzuifdkfcspowyncf.supabase.co",
).rstrip("/")

SUPABASE_SERVICE_ROLE_KEY = os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY"
)


class SupabaseStore:
    def __init__(self, label: str):
        self.label = label
        self.enabled = bool(
            SUPABASE_URL
            and SUPABASE_SERVICE_ROLE_KEY
        )
        self.account_id: Optional[int] = None
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if not self.enabled:
            logger.warning(
                "[%s] Supabase disabled: "
                "SUPABASE_SERVICE_ROLE_KEY is not set",
                self.label,
            )
            return

        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )

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
                logger.warning(
                    "[%s] Supabase account row not found",
                    self.label,
                )

        except Exception:
            logger.exception(
                "[%s] Supabase initialization failed",
                self.label,
            )

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
    ):
        if not self.enabled or not self.session:
            return None

        headers = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization":
                f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type":
                "application/json",
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
        if self.account_id is None:
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
        if self.account_id is None:
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
        if self.account_id is None:
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
        if self.account_id is None:
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
