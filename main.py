import os
import sys
import random
import asyncio
import logging
import urllib.request
import base64
from pathlib import Path
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from config import load_account_sessions
from account import PatrickAccount


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)


# =========================================================
# GLOBALS
# =========================================================

accounts = []
_background_tasks = []


# =========================================================
# KEEP ALIVE
# =========================================================

async def keep_alive_cycle():

    external_url = os.getenv("RENDER_EXTERNAL_URL")

    if not external_url:
        logger.info(
            "ℹ️ RENDER_EXTERNAL_URL не задан — self-ping пропущен"
        )
        return

    ping_url = external_url.rstrip("/") + "/health"

    logger.info(
        f"💓 Self-ping включён: {ping_url}"
    )

    while True:

        await asyncio.sleep(
            240 + random.randint(-15, 15)
        )

        try:

            await asyncio.to_thread(
                lambda: urllib.request.urlopen(
                    ping_url,
                    timeout=15
                ).read()
            )

        except Exception as e:

            logger.warning(
                f"⚠️ Self-ping ошибка: {e}"
            )


# =========================================================
# APPLICATION
# =========================================================

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

    for index, account in enumerate(accounts):

        if index:
            await asyncio.sleep(index * 2)

        try:

            await account.start()

            logger.info(
                f"✅ [{account.label}] запущен"
            )

        except Exception as e:

            logger.exception(
                f"❌ [{account.label}] ошибка запуска: {e}"
            )

    _background_tasks.append(
        asyncio.create_task(
            keep_alive_cycle()
        )
    )

    yield

    for task in _background_tasks:

        task.cancel()

    for account in accounts:

        try:

            await account.stop()

        except Exception as e:

            logger.warning(
                f"Ошибка остановки {account.label}: {e}"
            )


app = FastAPI(
    lifespan=lifespan
)


# =========================================================
# HEALTH
# =========================================================

@app.get("/")
@app.get("/health")
async def health():

    return {
        "status": "ok",
        "accounts": len(accounts)
    }


# =========================================================
# ADMIN AUTH
# =========================================================

ADMIN_USER = os.getenv(
    "ADMIN_USER",
    "admin"
)

ADMIN_PASSWORD = os.getenv(
    "ADMIN_PASSWORD"
)


def check_admin_auth(
    authorization: str | None
):

    if not ADMIN_PASSWORD:

        raise HTTPException(
            status_code=503,
            detail="ADMIN_PASSWORD is not configured"
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
            }
        )

    try:

        decoded = base64.b64decode(
            authorization[6:]
        ).decode("utf-8")

        username, password = decoded.split(
            ":",
            1
        )

    except Exception:

        raise HTTPException(
            status_code=401,
            detail="Invalid authentication"
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
            }
        )


# =========================================================
# SUPABASE
# =========================================================

async def supabase_get(
    table: str,
    params: dict | None = None
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
            detail="SUPABASE_URL is not configured"
        )

    if not service_key:

        raise HTTPException(
            status_code=503,
            detail=(
                "SUPABASE_SERVICE_ROLE_KEY "
                "is not configured"
            )
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

    timeout = aiohttp.ClientTimeout(
        total=15
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        async with session.get(
            url,
            params=params,
            headers=headers
        ) as response:

            if response.status >= 400:

                text = await response.text()

                logger.error(
                    f"Supabase {table}: "
                    f"{response.status} {text}"
                )

                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Supabase error "
                        f"{response.status}"
                    )
                )

            return await response.json()


# =========================================================
# ADMIN FILES
# =========================================================

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
        media_type="text/css"
    )


@app.get("/admin.js")
async def admin_js():

    return FileResponse(
        Path(__file__).with_name(
            "admin.js"
        ),
        media_type="application/javascript"
    )


# =========================================================
# ADMIN API
# =========================================================

@app.get("/api/admin/overview")
async def admin_overview(
    authorization: str | None = Header(
        default=None
    )
):

    check_admin_auth(
        authorization
    )

    (
        accounts_data,
        daily_stats,
        app_logs,
        promo_events,
        task_events,
        click_events,
        daily_bonus_events
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
                    "account_number.asc"
            }
        ),

        supabase_get(
            "daily_stats",
            {
                "select":
                    "stat_date,"
                    "account_id,"
                    "clicks,"
                    "tasks_completed,"
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
                    "1000"
            }
        ),

        supabase_get(
            "app_logs",
            {
                "select":
                    "id,"
                    "account_id,"
                    "level,"
                    "event_type,"
                    "message,"
                    "error,"
                    "metadata,"
                    "created_at",

                "order":
                    "created_at.desc",

                "limit":
                    "300"
            }
        ),

        supabase_get(
            "promo_events",
            {
                "select":
                    "id,"
                    "account_id,"
                    "promo_code,"
                    "found_at,"
                    "activated_at,"
                    "status,"
                    "error_reason,"
                    "stars",

                "order":
                    "found_at.desc",

                "limit":
                    "200"
            }
        ),

        supabase_get(
            "task_events",
            {
                "select":
                    "id,"
                    "account_id,"
                    "task_type,"
                    "completed_at,"
                    "stars",

                "order":
                    "completed_at.desc",

                "limit":
                    "200"
            }
        ),

        supabase_get(
            "click_events",
            {
                "select":
                    "id,"
                    "account_id,"
                    "clicked_at,"
                    "stars",

                "order":
                    "clicked_at.desc",

                "limit":
                    "200"
            }
        ),

        supabase_get(
            "daily_bonus_events",
            {
                "select":
                    "id,"
                    "account_id,"
                    "claimed_at,"
                    "stars",

                "order":
                    "claimed_at.desc",

                "limit":
                    "200"
            }
        )
    )


    # -----------------------------------------------------
    # TOTALS
    # -----------------------------------------------------

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


    for row in daily_stats:

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


    # -----------------------------------------------------
    # RESPONSE
    # -----------------------------------------------------

    return {

        "accounts":
            accounts_data,

        "daily_stats":
            daily_stats,

        "logs":
            app_logs,

        "promos":
            promo_events,

        "tasks":
            task_events,

        "clicks":
            click_events,

        "daily_bonus":
            daily_bonus_events,

        "totals":
            totals,

    }
