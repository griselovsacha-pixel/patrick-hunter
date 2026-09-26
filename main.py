"""Patrick Hunter FastAPI entrypoint + admin analytics API."""
import os
import sys
import random
import asyncio
import logging
import urllib.request
import base64
import json
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import aiohttp
from fastapi import Header, HTTPException
from fastapi.responses import FileResponse
from fastapi import FastAPI

from config import load_account_sessions
from account import PatrickAccount

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)

accounts: list = []
_background_tasks: list = []

ADMIN_TIMEZONE = ZoneInfo("Europe/Kyiv")


async def keep_alive_cycle():
    external_url = os.getenv("RENDER_EXTERNAL_URL")

    if not external_url:
        logger.info(
            "ℹ️ RENDER_EXTERNAL_URL не задан — self-ping пропущен"
        )
        return

    ping_url = external_url.rstrip("/") + "/health"

    while True:
        await asyncio.sleep(
            240 + random.randint(-15, 15)
        )

        try:
            await asyncio.to_thread(
                lambda: urllib.request.urlopen(
                    ping_url,
                    timeout=15,
                ).read()
            )
        except Exception as e:
            logger.warning(
                "⚠️ Self-ping ошибка: %s",
                e,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global accounts
    global _background_tasks

    sessions = load_account_sessions()

    if not sessions:
        logger.warning(
            "⚠️ Telegram sessions не найдены"
        )
        yield
        return

    accounts = [
        PatrickAccount(label, session)
        for label, session in sessions
    ]

    for index, acc in enumerate(accounts):
        if index:
            await asyncio.sleep(index * 2)

        try:
            await acc.start()
            logger.info(
                "✅ [%s] запущен",
                acc.label,
            )
        except Exception as e:
            logger.exception(
                "❌ [%s] не запущен: %s",
                acc.label,
                e,
            )

    _background_tasks.append(
        asyncio.create_task(
            keep_alive_cycle()
        )
    )

    yield

    for task in _background_tasks:
        task.cancel()

    for acc in accounts:
        try:
            await acc.stop()
        except Exception as e:
            logger.warning(
                "Ошибка остановки %s: %s",
                acc.label,
                e,
            )


app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "accounts": len(accounts),
    }


ADMIN_USER = os.getenv(
    "ADMIN_USER",
    "admin",
)

ADMIN_PASSWORD = os.getenv(
    "ADMIN_PASSWORD"
)


def check_admin_auth(
    authorization: str | None,
):
    if not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_PASSWORD is not configured",
        )

    if (
        not authorization
        or not authorization.startswith("Basic ")
    ):
        raise HTTPException(
            status_code=401,
            detail="Admin authentication required",
            headers={
                "WWW-Authenticate":
                    'Basic realm="Patrick Hunter Admin"'
            },
        )

    try:
        decoded = base64.b64decode(
            authorization[6:]
        ).decode("utf-8")

        username, password = decoded.split(
            ":",
            1,
        )
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication",
        )

    if (
        username != ADMIN_USER
        or password != ADMIN_PASSWORD
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid admin credentials",
            headers={
                "WWW-Authenticate":
                    'Basic realm="Patrick Hunter Admin"'
            },
        )


async def supabase_get(
    table: str,
    params: dict | None = None,
):
    supabase_url = os.getenv(
        "SUPABASE_URL"
    )
    service_key = os.getenv(
        "SUPABASE_SERVICE_ROLE_KEY"
    )

    if not supabase_url:
        raise HTTPException(
            status_code=503,
            detail="SUPABASE_URL is not configured",
        )

    if not service_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "SUPABASE_SERVICE_ROLE_KEY "
                "is not configured"
            ),
        )

    headers = {
        "apikey": service_key,
        "Authorization":
            f"Bearer {service_key}",
    }

    url = (
        supabase_url.rstrip("/")
        + "/rest/v1/"
        + table
    )

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15)
    ) as session:
        async with session.get(
            url,
            params=params,
            headers=headers,
        ) as response:
            if response.status >= 400:
                text = await response.text()

                logger.error(
                    "Supabase %s: %s %s",
                    table,
                    response.status,
                    text,
                )

                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Supabase error "
                        f"{response.status}"
                    ),
                )

            return await response.json()


def _parse_datetime(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt

    except Exception:
        return None


def _is_today(value):
    dt = _parse_datetime(value)

    if not dt:
        return False

    today = datetime.now(
        timezone.utc
    ).astimezone(
        ADMIN_TIMEZONE
    ).date()

    return (
        dt.astimezone(
            ADMIN_TIMEZONE
        ).date()
        == today
    )


def _calculate_today(
    tasks,
    clicks,
    daily_bonus,
    promos,
):
    result = {
        "clicks": 0,
        "tasks": 0,
        "mini_apps": 0,
        "daily": 0,
        "promos_found": 0,
        "promos_activated": 0,
        "promos_failed": 0,
        "stars": 0.0,
        "stars_from_clicks": 0.0,
        "stars_from_tasks": 0.0,
        "stars_from_promos": 0.0,
        "stars_from_daily": 0.0,
    }

    for row in clicks:
        if _is_today(
            row.get("clicked_at")
        ):
            stars = float(
                row.get("stars") or 0
            )

            result["clicks"] += 1
            result["stars"] += stars
            result["stars_from_clicks"] += stars

    for row in tasks:
        if not _is_today(
            row.get("completed_at")
        ):
            continue

        task_type = (
            row.get("task_type")
            or "task"
        )

        stars = float(
            row.get("stars") or 0
        )

        if task_type == "mini_app":
            result["mini_apps"] += 1
        else:
            result["tasks"] += 1
            result["stars"] += stars
            result["stars_from_tasks"] += stars

    for row in daily_bonus:
        if _is_today(
            row.get("claimed_at")
        ):
            stars = float(
                row.get("stars") or 0
            )

            result["daily"] += 1
            result["stars"] += stars
            result["stars_from_daily"] += stars

    for row in promos:
        if _is_today(
            row.get("found_at")
        ):
            result["promos_found"] += 1

        if (
            row.get("activated_at")
            and _is_today(
                row.get("activated_at")
            )
        ):
            result["promos_activated"] += 1

            stars = float(
                row.get("stars") or 0
            )

            result["stars"] += stars
            result["stars_from_promos"] += stars

        if (
            row.get("status") == "failed"
            and _is_today(
                row.get("found_at")
            )
        ):
            result["promos_failed"] += 1

    return result


def _real_balance_summary(
    accounts_data,
    snapshots,
):
    latest = {}
    first_today = {}

    for row in snapshots:
        account_id = row.get("account_id")

        if account_id is None:
            continue

        captured = _parse_datetime(
            row.get("captured_at")
        )

        if not captured:
            continue

        if account_id not in latest:
            latest[account_id] = {
                "balance":
                    float(
                        row.get("balance") or 0
                    ),
                "captured_at":
                    row.get("captured_at"),
            }

        if _is_today(row.get("captured_at")):
            existing = first_today.get(account_id)
            existing_dt = (
                _parse_datetime(existing["captured_at"])
                if existing
                else None
            )
            if existing_dt is None or captured < existing_dt:
                first_today[account_id] = {
                    "balance": float(row.get("balance") or 0),
                    "captured_at": row.get("captured_at"),
                }

    total_current = 0.0
    total_delta = 0.0

    by_account = []

    for account in accounts_data:
        account_id = account.get("id")
        current = latest.get(account_id)

        if current is None:
            balance = None
            captured_at = None
            delta = None
        else:
            balance = current["balance"]
            captured_at = current["captured_at"]

            baseline = first_today.get(account_id)
            baseline_balance = (
                baseline["balance"]
                if baseline is not None
                else None
            )

            delta = (
                balance - baseline_balance
                if baseline_balance is not None
                else None
            )

            total_current += balance

            if delta is not None:
                total_delta += delta

        by_account.append({
            "account_id": account_id,
            "account_number":
                account.get("account_number"),
            "name":
                account.get("name"),
            "balance": balance,
            "captured_at": captured_at,
            "delta_today": delta,
        })

    return {
        "current_balance": total_current,
        "delta_today": total_delta,
        "by_account": by_account,
    }


@app.get("/admin")
async def admin_page():
    return FileResponse(
        Path(__file__).with_name(
            "admin.html"
        )
    )


@app.get("/admin.css")
async def admin_css():
    return FileResponse(
        Path(__file__).with_name(
            "admin.css"
        ),
        media_type="text/css",
    )


@app.get("/admin.js")
async def admin_js():
    return FileResponse(
        Path(__file__).with_name(
            "admin.js"
        ),
        media_type="application/javascript",
    )


@app.get("/api/admin/overview")
async def admin_overview(
    authorization: str | None = Header(
        default=None
    ),
):
    check_admin_auth(authorization)

    (
        accounts_data,
        stats,
        logs,
        promos,
        tasks,
        clicks,
        daily_bonus,
        balance_snapshots,
    ) = await asyncio.gather(
        supabase_get(
            "accounts",
            {
                "select":
                    "id,account_number,name,"
                    "enabled,connected,"
                    "last_connected_at,"
                    "updated_at",
                "order":
                    "account_number.asc",
            },
        ),
        supabase_get(
            "daily_stats",
            {
                "select":
                    "stat_date,account_id,"
                    "clicks,tasks_completed,"
                    "mini_apps_completed,"
                    "daily_bonuses,"
                    "promo_codes_found,"
                    "promo_codes_activated,"
                    "promo_codes_failed,"
                    "stars_from_clicks,"
                    "stars_from_tasks,"
                    "stars_from_promos,"
                    "stars_from_daily,"
                    "total_stars",
                "order":
                    "stat_date.asc",
                "limit":
                    "1000",
            },
        ),
        supabase_get(
            "app_logs",
            {
                "select":
                    "id,account_id,level,"
                    "event_type,message,error,"
                    "metadata,created_at",
                "order":
                    "created_at.desc",
                "limit":
                    "300",
            },
        ),
        supabase_get(
            "promo_events",
            {
                "select":
                    "id,account_id,promo_code,"
                    "found_at,activated_at,status,"
                    "error_reason,stars",
                "order":
                    "found_at.desc",
                "limit":
                    "200",
            },
        ),
        supabase_get(
            "task_events",
            {
                "select":
                    "id,account_id,task_type,"
                    "completed_at,stars",
                "order":
                    "completed_at.desc",
                "limit":
                    "500",
            },
        ),
        supabase_get(
            "click_events",
            {
                "select":
                    "id,account_id,clicked_at,stars",
                "order":
                    "clicked_at.desc",
                "limit":
                    "500",
            },
        ),
        supabase_get(
            "daily_bonus_events",
            {
                "select":
                    "id,account_id,claimed_at,stars",
                "order":
                    "claimed_at.desc",
                "limit":
                    "200",
            },
        ),
        supabase_get(
            "balance_snapshots",
            {
                "select":
                    "id,account_id,balance,captured_at",
                "order":
                    "captured_at.desc",
                "limit":
                    "2000",
            },
        ),
    )

    runtime_status = {}

    for acc in accounts:
        try:
            number = int(
                "".join(
                    ch for ch in acc.label
                    if ch.isdigit()
                )
                or "1"
            )

            runtime_status[number] = bool(
                acc.client.is_connected()
            )

        except Exception:
            continue

    for row in accounts_data:
        number = int(
            row.get("account_number")
            or 0
        )

        if number in runtime_status:
            row["connected"] = (
                runtime_status[number]
            )
            row["runtime_connected"] = True
        else:
            row["connected"] = False
            row["runtime_connected"] = False

    totals = {
        "clicks": 0,
        "tasks": 0,
        "mini_apps": 0,
        "daily": 0,
        "promos_found": 0,
        "promos_activated": 0,
        "promos_failed": 0,
        "stars": 0.0,
        "stars_from_clicks": 0.0,
        "stars_from_tasks": 0.0,
        "stars_from_promos": 0.0,
        "stars_from_daily": 0.0,
    }

    for row in stats:
        totals["clicks"] += int(
            row.get("clicks") or 0
        )
        totals["tasks"] += int(
            row.get("tasks_completed") or 0
        )
        totals["mini_apps"] += int(
            row.get("mini_apps_completed") or 0
        )
        totals["daily"] += int(
            row.get("daily_bonuses") or 0
        )
        totals["promos_found"] += int(
            row.get("promo_codes_found") or 0
        )
        totals["promos_activated"] += int(
            row.get("promo_codes_activated") or 0
        )
        totals["promos_failed"] += int(
            row.get("promo_codes_failed") or 0
        )
        totals["stars"] += float(
            row.get("total_stars") or 0
        )
        totals["stars_from_clicks"] += float(
            row.get("stars_from_clicks") or 0
        )
        totals["stars_from_tasks"] += float(
            row.get("stars_from_tasks") or 0
        )
        totals["stars_from_promos"] += float(
            row.get("stars_from_promos") or 0
        )
        totals["stars_from_daily"] += float(
            row.get("stars_from_daily") or 0
        )

    today = _calculate_today(
        tasks,
        clicks,
        daily_bonus,
        promos,
    )

    real_balance = _real_balance_summary(
        accounts_data,
        balance_snapshots,
    )

    return {
        "accounts": accounts_data,
        "daily_stats": stats,
        "logs": logs,
        "promos": promos,
        "tasks": tasks,
        "clicks": clicks,
        "daily_bonus": daily_bonus,
        "balance_snapshots": balance_snapshots,
        "totals": totals,
        "today": today,
        "real_balance": real_balance,
    }
