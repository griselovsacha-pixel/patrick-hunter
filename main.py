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
from datetime import datetime, timedelta, timezone

import aiohttp
from fastapi import Header, HTTPException
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from fastapi import FastAPI

from config import load_account_sessions
from account import PatrickAccount

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

accounts: list = []
_background_tasks: list = []

async def keep_alive_cycle():
    external_url = os.getenv("RENDER_EXTERNAL_URL")
    if not external_url:
        logger.info("ℹ️ RENDER_EXTERNAL_URL не задан — self-ping пропущен")
        return
    ping_url = external_url.rstrip('/') + '/health'
    logger.info(f"💓 Self-ping включён: {ping_url} каждые ~4 мин")
    while True:
        await asyncio.sleep(240 + random.randint(-15, 15))
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(ping_url, timeout=15).read())
        except Exception as e:
            logger.warning(f"⚠️ Self-ping ошибка: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global accounts, _background_tasks
    sessions = load_account_sessions()
    if not sessions:
        logger.warning("⚠️ TELEGRAM_SESSION не задан")
        yield
        return
    accounts = [PatrickAccount(label, session) for label, session in sessions]
    for index, acc in enumerate(accounts):
        if index:
            await asyncio.sleep(index * 2)
        try:
            await acc.start()
            logger.info(f"✅ [{acc.label}] запущен")
        except Exception as e:
            logger.exception(f"❌ [{acc.label}] не запущен, остальные продолжают работу: {e}")
    _background_tasks.append(asyncio.create_task(keep_alive_cycle()))
    yield
    for t in _background_tasks:
        t.cancel()
    for acc in accounts:
        await acc.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok", "accounts": len(accounts)}

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_HTML = Path(__file__).with_name("admin.html")


def _admin_auth(authorization: str | None):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not configured")
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Admin authentication required", headers={"WWW-Authenticate": 'Basic realm="Patrick Hunter Admin"'})
    try:
        raw = base64.b64decode(authorization[6:]).decode("utf-8")
        user, password = raw.split(":", 1)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    if user != ADMIN_USER or password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin credentials", headers={"WWW-Authenticate": 'Basic realm="Patrick Hunter Admin"'})

async def _json_get(base: str, path: str, key: str, params: dict | None = None):
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(base.rstrip('/') + path, params=params, headers=headers) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise HTTPException(status_code=502, detail=f"Upstream error {resp.status}")
            return json.loads(body) if body else []

async def _admin_query(path: str, params: dict | None = None):
    url = os.getenv("SUPABASE_URL", "https://wqzpzuifdkfcspowyncf.supabase.co")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="Supabase service role is not configured")
    return await _json_get(url, path, key, params)

@app.get("/admin")
async def admin_page():
    return FileResponse(ADMIN_HTML)

@app.get("/admin.css")
async def admin_css():
    return FileResponse(Path(__file__).with_name("admin.css"), media_type="text/css")

@app.get("/admin.js")
async def admin_js():
    return FileResponse(Path(__file__).with_name("admin.js"), media_type="application/javascript")

@app.get("/api/admin/overview")
async def admin_overview(authorization: str | None = Header(default=None)):
    _admin_auth(authorization)
    accounts_data, stats, logs, promos, tasks, clicks, daily_bonus = await asyncio.gather(
        _admin_query("/rest/v1/accounts", {"select": "id,account_number,name,enabled,connected,last_connected_at,updated_at", "order": "account_number.asc"}),
        _admin_query("/rest/v1/daily_stats", {"select": "stat_date,account_id,clicks,tasks_completed,mini_apps_completed,daily_bonuses,promo_codes_found,promo_codes_activated,promo_codes_failed,stars_from_clicks,stars_from_tasks,stars_from_promos,stars_from_daily,total_stars", "order": "stat_date.asc", "limit": "1000"}),
        _admin_query("/rest/v1/app_logs", {"select": "id,account_id,level,event_type,message,error,metadata,created_at", "order": "created_at.desc", "limit": "300"}),
        _admin_query("/rest/v1/promo_events", {"select": "id,account_id,promo_code,found_at,activated_at,status,error_reason,stars", "order": "found_at.desc", "limit": "200"}),
        _admin_query("/rest/v1/task_events", {"select": "id,account_id,task_type,completed_at,stars", "order": "completed_at.desc", "limit": "200"}),
        _admin_query("/rest/v1/click_events", {"select": "id,account_id,clicked_at,stars", "order": "clicked_at.desc", "limit": "200"}),
        _admin_query("/rest/v1/daily_bonus_events", {"select": "id,account_id,claimed_at,stars", "order": "claimed_at.desc", "limit": "200"}),
    )
    totals = {"clicks": 0, "tasks": 0, "mini_apps": 0, "daily": 0, "promos_found": 0, "promos_activated": 0, "promos_failed": 0, "stars": 0.0}
    for row in stats:
        totals["clicks"] += int(row.get("clicks") or 0)
        totals["tasks"] += int(row.get("tasks_completed") or 0)
        totals["mini_apps"] += int(row.get("mini_apps_completed") or 0)
        totals["daily"] += int(row.get("daily_bonuses") or 0)
        totals["promos_found"] += int(row.get("promo_codes_found") or 0)
        totals["promos_activated"] += int(row.get("promo_codes_activated") or 0)
        totals["promos_failed"] += int(row.get("promo_codes_failed") or 0)
        totals["stars"] += float(row.get("total_stars") or 0)
    return {"accounts": accounts_data, "daily_stats": stats, "logs": logs, "promos": promos, "tasks": tasks, "clicks": clicks, "daily_bonus": daily_bonus, "totals": totals}

@app.get("/api/admin/render")
async def admin_render(authorization: str | None = Header(default=None)):
    _admin_auth(authorization)
    key = os.getenv("RENDER_API_KEY")
    service_id = os.getenv("RENDER_SERVICE_ID", "srv-d8denh740ujc73ck8mfg")
    workspace_id = os.getenv("RENDER_WORKSPACE_ID", "tea-d72m3295pdvs739f5nc0")
    if not key:
        return {"configured": False, "service": {"id": service_id}, "deploys": [], "logs": [], "message": "RENDER_API_KEY не настроен"}
    base = "https://api.render.com/v1"
    try:
        service, deploys, logs = await asyncio.gather(
            _json_get(base, f"/services/{service_id}", key),
            _json_get(base, f"/services/{service_id}/deploys", key, {"limit": 20}),
            _json_get(base, "/logs", key, {"ownerId": workspace_id, "resource": service_id, "direction": "backward", "limit": 100}),
        )
        return {"configured": True, "service": service, "deploys": deploys, "logs": logs}
    except HTTPException as e:
        return {"configured": False, "service": {"id": service_id}, "deploys": [], "logs": [], "message": e.detail}
    except Exception as e:
        logger.warning(f"Render API error: {e}")
        return {"configured": False, "service": {"id": service_id}, "deploys": [], "logs": [], "message": "Render API недоступен"}
