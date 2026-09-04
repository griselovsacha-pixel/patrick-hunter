"""
Точка входа. Держит FastAPI-приложение (нужно для Render — веб-сервис
должен слушать порт), self-ping против засыпания на бесплатном тарифе,
и запускает PatrickAccount.
"""
import os
import sys
import random
import asyncio
import logging
import urllib.request
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import load_account_sessions
from account import PatrickAccount

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

accounts: list = []
_background_tasks: list = []


async def keep_alive_cycle():
    """Анти-засыпание на бесплатном тарифе Render — периодический self-ping."""
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
    for acc in accounts:
        await acc.start()
        logger.info(f"✅ [{acc.label}] запущен")

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
