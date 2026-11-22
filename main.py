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
from fastapi.responses import FileResponse, Response
from fastapi import FastAPI, Request

from config import load_account_sessions, ACCOUNT_START_STAGGER_SEC
from account import PatrickAccount, FATAL_AUTH_ERRORS
from hub import Hub
from memguard import memory_guard_cycle, rss_mb
from supabase_db import supabase_headers
import db_migrate
from resources import ResourceSampler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)

accounts: list = []
_background_tasks: list = []
hub = Hub()

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


async def run_account(acc: PatrickAccount):
    """Супервизор одного аккаунта: стартует со сдвигом и пытается подняться
    снова при сбое (сеть, временный FloodWait). Падение/зависание одного
    аккаунта на другой не влияет — у каждого свой супервизор и своя задача."""
    if acc.index:
        await asyncio.sleep(acc.index * ACCOUNT_START_STAGGER_SEC)

    backoff = 30
    while True:
        try:
            await acc.start()
            logger.info("✅ [%s] запущен", acc.label)
            return
        except asyncio.CancelledError:
            raise
        except FATAL_AUTH_ERRORS as e:
            acc.dead = f"{type(e).__name__}: сессия недействительна"
            logger.error("❌ [%s] сессия недействительна (%s) — аккаунт отключён, "
                         "нужна новая TELEGRAM_SESSION", acc.label, type(e).__name__)
            try:
                await acc.stop()
            except Exception:
                pass
            return
        except Exception as e:
            logger.exception("❌ [%s] не запустился: %s — повтор через %dс", acc.label, e, backoff)
            try:
                await acc.stop()
            except Exception:
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global accounts
    global _background_tasks

    sessions = load_account_sessions()

    # Замер ресурсов: база (RSS до запуска аккаунтов) фиксируется ЗДЕСЬ
    global sampler
    sampler = ResourceSampler(lambda: accounts)
    try:
        await asyncio.wait_for(sampler.load_history(), timeout=15)
    except Exception:
        pass
    _background_tasks.append(asyncio.create_task(sampler.run()))

    # БД: создать недостающие таблицы/колонки и строки accounts ДО старта аккаунтов.
    # Любая ошибка БД не должна мешать старту бота/HTTP.
    account_numbers = [
        int("".join(ch for ch in label if ch.isdigit()) or "1") for label, _ in sessions
    ]
    try:
        await asyncio.wait_for(db_migrate.ensure_schema(account_numbers), timeout=60)
    except Exception as e:
        logger.error("DB: автонастройка не выполнена: %s", e)
    _background_tasks.append(asyncio.create_task(db_migrate.maintenance_loop()))

    if not sessions:
        logger.warning("⚠️ Telegram sessions не найдены")
        yield
        for task in _background_tasks:
            task.cancel()
        return

    accounts = [
        PatrickAccount(label, session, index)
        for index, (label, session) in enumerate(sessions)
    ]
    for acc in accounts:
        hub.register(acc)

    # HTTP-сервер поднимается сразу (Render ждёт открытый порт), аккаунты
    # стартуют в фоне, каждый в своей задаче и со своим сдвигом.
    for acc in accounts:
        _background_tasks.append(asyncio.create_task(run_account(acc)))
    _background_tasks.append(asyncio.create_task(keep_alive_cycle()))
    _background_tasks.append(asyncio.create_task(memory_guard_cycle()))

    yield

    for task in _background_tasks:
        task.cancel()

    for acc in accounts:
        try:
            await acc.stop()
        except Exception as e:
            logger.warning("Ошибка остановки %s: %s", acc.label, e)


app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "accounts": len(accounts),
        "ready": [acc.label for acc in accounts if acc.ready],
        "rss_mb": round(rss_mb()),
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

    headers = supabase_headers(service_key)

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


KNOWLEDGE_KINDS = ("screen", "task", "ad", "notice", "alert", "transition", "flow")


async def _load_knowledge(kind: str | None = None, limit: int = 2000):
    params = {
        "select": "fingerprint,kind,title,sample_text,data,seen_count,first_seen,last_seen,last_account",
        "order": "last_seen.desc",
        "limit": str(max(1, min(limit, 5000))),
    }
    if kind in KNOWLEDGE_KINDS:
        params["kind"] = f"eq.{kind}"
    return await supabase_get("bot_knowledge", params)


def _knowledge_markdown(rows: list) -> str:
    by = {k: [] for k in KNOWLEDGE_KINDS}
    for r in rows:
        by.setdefault(r.get("kind") or "screen", []).append(r)
    names = {
        "screen": "Экраны (меню, разделы)", "task": "Задания", "ad": "Реклама бота (вставки вместо экрана)", "notice": "Уведомления и плашки (без кнопок)",
        "flow": "Сценарии (автовывод и т.п.)", "alert": "Всплывающие окна (алерты)", "transition": "Что делают кнопки (кнопка → результат)",
    }
    out = [
        "# Знания о @patrickstarsrobot (авто-сбор)",
        f"Сгенерировано: {datetime.now(ADMIN_TIMEZONE):%d.%m.%Y %H:%M}. "
        f"Записей: {len(rows)} (" + ", ".join(f"{k}: {len(v)}" for k, v in by.items()) + ")",
        "Числа в текстах — примеры; одинаковые по смыслу сообщения объединены в одну запись.",
        "",
    ]
    for kind in KNOWLEDGE_KINDS:
        items = by.get(kind) or []
        if not items:
            continue
        out += [f"## {names[kind]}", ""]
        for r in items:
            d = r.get("data") or {}
            out.append(f"### {r.get('title') or '(без заголовка)'}")
            out.append(f"- видели: {r.get('seen_count')} раз, последний: {r.get('last_seen')}, id: `{r.get('fingerprint')}`")
            if kind == "transition":
                b = d.get("button") or {}
                out.append(f"- экран: {d.get('screen_title')}")
                out.append(f"- кнопка: «{b.get('text')}» ({b.get('type')}, data={b.get('data')})")
                out.append(f"- результат: {d.get('outcome')} — {d.get('outcome_title')}")
            if r.get("sample_text"):
                out += ["```", r["sample_text"], "```"]
            btns = d.get("buttons") or []
            if btns:
                out.append("- кнопки:")
                for b in btns:
                    extra = " ".join(x for x in (
                        f"data={b['data']}" if b.get("data") else "",
                        f"url={b['url']}" if b.get("url") else "") if x)
                    out.append(f"  - «{b.get('text')}» [{b.get('type')}] {extra}".rstrip())
            out.append("")
    return "\n".join(out)


@app.get("/api/admin/knowledge")
async def admin_knowledge(
    kind: str | None = None,
    authorization: str | None = Header(default=None),
):
    check_admin_auth(authorization)
    rows = await _load_knowledge(kind)
    counts = {}
    for r in rows:
        counts[r.get("kind")] = counts.get(r.get("kind"), 0) + 1
    return {"items": rows, "counts": counts, "total": len(rows)}


def _crawler_acc():
    for acc in accounts:
        if getattr(acc, "crawler", None):
            return acc
    return None


@app.post("/api/admin/knowledge/crawl")
async def admin_knowledge_crawl(authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    acc = _crawler_acc()
    if not acc or not acc.ready:
        raise HTTPException(status_code=503, detail="Аккаунт ещё не запущен")
    if not acc.crawler.request():
        raise HTTPException(status_code=409, detail="Обход уже идёт")
    return {"ok": True}


@app.get("/api/admin/knowledge/crawl-status")
async def admin_knowledge_crawl_status(authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    acc = _crawler_acc()
    return acc.crawler.status if acc else {"running": False, "progress": "аккаунт не запущен"}


@app.get("/api/admin/knowledge/export")
async def admin_knowledge_export(
    format: str = "json",
    authorization: str | None = Header(default=None),
):
    check_admin_auth(authorization)
    rows = await _load_knowledge()
    stamp = datetime.now(ADMIN_TIMEZONE).strftime("%Y%m%d-%H%M")
    if format == "md":
        return Response(
            _knowledge_markdown(rows),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="patrick-knowledge-{stamp}.md"'},
        )
    payload = {
        "bot": "@patrickstarsrobot",
        "generated_at": datetime.now(ADMIN_TIMEZONE).isoformat(),
        "total": len(rows),
        "items": rows,
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="patrick-knowledge-{stamp}.json"'},
    )


sampler: ResourceSampler | None = None

JOB_TITLES = {"tasks": "Задания", "clicker": "Клик", "daily": "Ежедневка", "crawler": "Обход бота"}


def _acc_number(acc) -> int:
    return int("".join(ch for ch in acc.label if ch.isdigit()) or "1")


@app.get("/api/admin/jobs")
async def admin_jobs(authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    out = []
    for acc in accounts:
        sched = getattr(acc, "sched", None)
        if not sched:
            continue
        snap = sched.snapshot()
        plan = sched.get_data("clicker")
        snap["clicker"]["plan"] = {
            "done": plan.get("done", 0), "total": len(plan.get("times") or []), "date": plan.get("date"),
        }
        if not getattr(acc, "crawler", None):
            snap.pop("crawler", None)
        out.append({"account": _acc_number(acc), "label": acc.label, "ready": bool(acc.ready), "jobs": snap,
                    "warmup": sched.warmup_info(), "dead": acc.dead, "proxy": await _proxy_public(acc)})
    return {"accounts": out, "titles": JOB_TITLES}


@app.post("/api/admin/run")
async def admin_run(account: int, job: str, authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    acc = next((a for a in accounts if _acc_number(a) == account), None)
    if not acc or not getattr(acc, "sched", None):
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.ready:
        raise HTTPException(status_code=503, detail="Аккаунт ещё запускается")
    if job not in JOB_TITLES:
        raise HTTPException(status_code=400, detail="Неизвестная задача")
    if not acc.sched.trigger(job):
        raise HTTPException(status_code=409, detail="Задача уже выполняется (или для неё нет ручного запуска)")
    return {"ok": True}


async def _proxy_public(acc) -> dict:
    """Состояние маршрута аккаунта для админки (без логина/пароля)."""
    mgr = getattr(acc, "proxy", None)
    if not mgr:
        return {"configured": False, "status": "unknown", "connected": bool(acc.client.is_connected())}
    try:
        return await mgr.public()
    except Exception as e:
        logger.debug("proxy public: %r", e)
        return {"configured": False, "status": "unknown", "connected": bool(acc.client.is_connected())}


@app.get("/api/admin/proxies")
async def admin_proxies(authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    return {"accounts": [
        {"account": _acc_number(a), "label": a.label, "proxy": await _proxy_public(a)} for a in accounts
    ]}


@app.post("/api/admin/proxies/check")
async def admin_proxies_check(account: int = 0, authorization: str | None = Header(default=None)):
    """Проверить прокси сейчас: account=N — один аккаунт, 0 — все."""
    check_admin_auth(authorization)
    targets = [a for a in accounts if account in (0, _acc_number(a))]
    if not targets:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    await asyncio.gather(*(a.proxy.refresh() for a in targets), return_exceptions=True)
    return {"accounts": [
        {"account": _acc_number(a), "label": a.label, "proxy": await _proxy_public(a)} for a in targets
    ]}


def _account_or_404(number: int):
    acc = next((a for a in accounts if _acc_number(a) == number), None)
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    return acc


@app.post("/api/admin/proxy/mode")
async def admin_proxy_mode(account: int, mode: str, authorization: str | None = Header(default=None)):
    """Режим маршрута аккаунта: auto (прокси, при аварии — IP Render) | on (только прокси) | off (напрямую, IP Render)."""
    check_admin_auth(authorization)
    acc = _account_or_404(account)
    if acc.dead:
        raise HTTPException(status_code=409, detail=f"Аккаунт остановлен: {acc.dead}")
    try:
        persisted = await acc.proxy.set_mode(mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось переключить: {type(e).__name__}: {str(e)[:150]}")
    return {"ok": True, "saved": persisted, "proxy": await _proxy_public(acc)}


@app.post("/api/admin/proxy/url")
async def admin_proxy_url(request: Request, authorization: str | None = Header(default=None)):
    """Задать адрес прокси аккаунта из админки (пустой url — убрать и вернуться к переменной ACCOUNT_<N>_PROXY)."""
    check_admin_auth(authorization)
    b = await request.json()
    acc = _account_or_404(int(b.get("account") or 0))
    if acc.dead:
        raise HTTPException(status_code=409, detail=f"Аккаунт остановлен: {acc.dead}")
    try:
        persisted = await acc.proxy.set_url(str(b.get("url") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось применить: {type(e).__name__}: {str(e)[:150]}")
    return {"ok": True, "saved": persisted, "proxy": await _proxy_public(acc)}


@app.post("/api/admin/warmup")
async def admin_warmup(request: Request, authorization: str | None = Header(default=None)):
    """Прогрев аккаунта: days > 0 — включить на N дней (отсчёт с этой минуты), 0 — выключить."""
    check_admin_auth(authorization)
    b = await request.json()
    acc = _account_or_404(int(b.get("account") or 0))
    try:
        days = int(b.get("days"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="days: целое число дней (0 — выключить)")
    if not 0 <= days <= 60:
        raise HTTPException(status_code=400, detail="days: от 0 до 60")
    persisted = await acc.sched.start_warmup(days)
    return {"ok": True, "saved": persisted, "warmup": acc.sched.warmup_info()}


@app.post("/api/admin/probe-withdraw")
async def admin_probe_withdraw(account: int, authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    acc = next((a for a in accounts if _acc_number(a) == account), None)
    if not acc or not acc.ready:
        raise HTTPException(status_code=503, detail="Аккаунт не найден или ещё запускается")
    from withdraw_probe import probe_withdraw
    return {"result": await probe_withdraw(acc)}


def _sg_wrap(coro):
    async def run():
        try:
            return await coro
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return run()


@app.post("/api/admin/session/start")
async def admin_session_start(request: Request, authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    import session_gen as sg
    b = await request.json()
    if b.get("mode") == "qr":
        return await _sg_wrap(sg.start_qr(b.get("api_id"), b.get("api_hash")))
    return await _sg_wrap(sg.start_phone(b.get("api_id"), b.get("api_hash"), b.get("phone")))


@app.post("/api/admin/session/code")
async def admin_session_code(request: Request, authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    import session_gen as sg
    b = await request.json()
    return await _sg_wrap(sg.submit_code(b.get("sid"), b.get("code")))


@app.post("/api/admin/session/password")
async def admin_session_password(request: Request, authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    import session_gen as sg
    b = await request.json()
    return await _sg_wrap(sg.submit_password(b.get("sid"), b.get("password")))


@app.get("/api/admin/session/status")
async def admin_session_status(sid: str, authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    import session_gen as sg
    return sg.status(sid)


@app.post("/api/admin/session/cancel")
async def admin_session_cancel(sid: str, authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    import session_gen as sg
    await sg.cancel(sid)
    return {"ok": True}


@app.post("/api/admin/refresh-profile")
async def admin_refresh_profile(account: int, authorization: str | None = Header(default=None)):
    """Открыть Профиль в боте и обновить баланс / друзей / активировавших."""
    check_admin_auth(authorization)
    acc = next((a for a in accounts if _acc_number(a) == account), None)
    if not acc or not acc.ready:
        raise HTTPException(status_code=503, detail="Аккаунт не найден или ещё запускается")
    ok = await acc._refresh_real_balance()
    ps = acc.profile_stats
    return {"ok": bool(ok), "balance": ps.balance, "friends": ps.friends, "activated": ps.activated}


@app.post("/api/admin/withdraw")
async def admin_withdraw(account: int, gift: str, mode: str = "friend", friend_id: str = "",
                         authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    import withdraw as wd
    acc = next((a for a in accounts if _acc_number(a) == account), None)
    if not acc or not acc.ready:
        raise HTTPException(status_code=503, detail="Аккаунт не найден или ещё запускается")
    try:
        job = await wd.manager.start(acc, gift, mode, friend_id or None)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "job": job}


@app.get("/api/admin/withdrawals")
async def admin_withdrawals(limit: int = 200, authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    import withdraw as wd
    try:
        history = await supabase_get("withdrawals", {
            "select": "*", "order": "created_at.desc", "limit": str(max(1, min(limit, 500)))})
    except Exception:
        history = wd.manager.recent
    running = wd.manager.current
    if running:
        history = [h for h in history if h.get("id") != running.get("id")]
    accs = []
    for a in accounts:
        ps = a.profile_stats
        accs.append({
            "account": _acc_number(a), "label": a.label, "ready": bool(a.ready),
            "balance": round(float(ps.balance or 0), 2), "friends": ps.friends, "activated": ps.activated,
            "known": bool(getattr(ps, "balance_updated_at", None)), "min_activated": wd.MIN_ACTIVATED,
        })
    done = [h for h in history if h.get("status") == "success"]
    return {
        "enabled": wd.ENABLED, "friend_id": wd.FRIEND_ID, "gifts": wd.GIFTS,
        "running": running, "history": history, "accounts": accs,
        "summary": {
            "total": len(history), "success": len(done),
            "unverified": sum(1 for h in history if h.get("status") == "unverified"),
            "failed": sum(1 for h in history if h.get("status") == "failed"),
            "stars_out": round(sum(float(h.get("stars") or 0) for h in done), 2),
        },
    }


@app.get("/api/admin/resources")
async def admin_resources(hours: int = 24, authorization: str | None = Header(default=None)):
    check_admin_auth(authorization)
    if not sampler:
        raise HTTPException(status_code=503, detail="Замер ресурсов не запущен")
    return {"analysis": sampler.analyze(), "history": sampler.history(max(1, min(hours, 72)))}


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
    proxy_status = {}
    dead_status = {}

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
            dead_status[number] = acc.dead
            proxy_status[number] = await _proxy_public(acc)

        except Exception:
            continue

    for row in accounts_data:
        number = int(
            row.get("account_number")
            or 0
        )

        row["proxy"] = proxy_status.get(number)
        row["dead"] = dead_status.get(number)

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
