"""
Автонастройка БД и ежемесячная очистка (Supabase = обычный Postgres).

REST-API Supabase (PostgREST) не умеет менять схему, поэтому для создания
таблиц/колонок нужно прямое подключение к Postgres — переменная DATABASE_URL
(Supabase -> Project Settings -> Database -> Connection string -> «Session pooler»
или «Transaction pooler»; на Render бесплатный direct-хост часто недоступен из-за IPv6).

Что делает:
  ensure_schema()      — при старте: создаёт недостающие таблицы/колонки/индексы/функцию
                         record_patrick_event (из db_schema.py), включает RLS, заводит строки
                         accounts для настроенных сессий, просит PostgREST перечитать схему.
  maintenance_loop()   — фоновая задача: раз в CLEANUP_INTERVAL_DAYS (30) чистит мусорные логи.
                         Дата последней очистки хранится в БД (maintenance_state), поэтому
                         рестарты Render её не сбрасывают.

Если DATABASE_URL не задан или asyncpg не установлен — всё это просто пропускается,
приложение работает как раньше.
"""
import asyncio
import logging
import os

import db_schema

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# --- Сколько хранить (дней), всё настраивается через env ---
LOG_KEEP_INFO_DAYS = int(os.getenv("LOG_KEEP_INFO_DAYS", "14"))        # INFO/DEBUG в app_logs
LOG_KEEP_ERROR_DAYS = int(os.getenv("LOG_KEEP_ERROR_DAYS", "60"))      # любые логи (ERROR/WARN) старше — удаляются
SNAPSHOT_THIN_AFTER_DAYS = int(os.getenv("SNAPSHOT_THIN_AFTER_DAYS", "60"))  # старше — оставляем 1 снимок баланса в день
EVENTS_KEEP_DAYS = int(os.getenv("EVENTS_KEEP_DAYS", "365"))           # promo/task/click/daily_bonus events
CLEANUP_INTERVAL_DAYS = int(os.getenv("CLEANUP_INTERVAL_DAYS", "30"))
CLEANUP_CHECK_EVERY_SEC = 6 * 3600
BATCH = 5000

_LOCK_KEY = 727274  # произвольное число для advisory-lock миграции


async def _connect():
    import asyncpg  # ленивый импорт: без пакета модуль просто отключается
    # statement_cache_size=0 — обязательно для Supabase transaction pooler (порт 6543)
    return await asyncpg.connect(DATABASE_URL, statement_cache_size=0, timeout=20)


def enabled() -> bool:
    if not DATABASE_URL:
        return False
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        logger.warning("DB: asyncpg не установлен — миграции/очистка отключены")
        return False
    return True


# --------------------------------------------------------------------------
# Миграция
# --------------------------------------------------------------------------
async def ensure_schema(account_numbers=()) -> bool:
    """Идемпотентно приводит БД к схеме из db_schema.py. Возвращает True при успехе.
    Не бросает исключений наружу: БД-проблемы не должны ронять бота."""
    if not enabled():
        if not DATABASE_URL:
            logger.warning("DB: DATABASE_URL не задан — автосоздание таблиц/колонок пропущено "
                           "(схему можно применить вручную: python db_schema.py > schema.sql)")
        return False
    try:
        conn = await _connect()
    except Exception as e:
        logger.error("DB: не удалось подключиться по DATABASE_URL: %s", e)
        return False

    failed = 0
    try:
        async with conn.transaction():
            # xact-lock работает и через pooler, и при двух одновременных инстансах
            await conn.execute("select pg_advisory_xact_lock($1)", _LOCK_KEY)
            for sql in db_schema.statements():
                try:
                    async with conn.transaction():  # savepoint: сбой одного оператора не рвёт остальные
                        await conn.execute(sql)
                except Exception as e:
                    failed += 1
                    logger.error("DB: оператор не выполнился (%s): %s", str(e)[:160], " ".join(sql.split())[:120])

            for n in sorted({int(x) for x in account_numbers}):
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "insert into accounts (account_number, name) values ($1, $2) "
                            "on conflict (account_number) do nothing",
                            n, f"Аккаунт {n}",
                        )
                except Exception as e:
                    failed += 1
                    logger.error("DB: не удалось создать accounts #%s: %s", n, e)
    except Exception as e:
        logger.error("DB: миграция прервана: %s", e)
        return False
    finally:
        try:
            await conn.close()
        except Exception:
            pass

    if failed:
        logger.warning("DB: схема применена с ошибками (%d) — см. выше", failed)
    else:
        logger.info("DB: схема актуальна (%d таблиц)", len(db_schema.TABLES))
    return failed == 0


# --------------------------------------------------------------------------
# Очистка
# --------------------------------------------------------------------------
async def _delete_batched(conn, table: str, where: str, *args) -> int:
    """DELETE пачками по BATCH строк, чтобы не держать долгие блокировки."""
    total = 0
    while True:
        res = await conn.execute(
            f'delete from "{table}" where id in (select id from "{table}" where {where} limit {BATCH})',
            *args,
        )
        n = int(res.split()[-1])
        total += n
        if n < BATCH:
            return total
        await asyncio.sleep(0.2)


async def cleanup(conn) -> dict:
    """Одна очистка. Возвращает {что: сколько удалено}."""
    out = {}
    out["app_logs_info"] = await _delete_batched(
        conn, "app_logs",
        "upper(level) in ('INFO','DEBUG') and created_at < now() - make_interval(days => $1::int)",
        LOG_KEEP_INFO_DAYS,
    )
    out["app_logs_old"] = await _delete_batched(
        conn, "app_logs", "created_at < now() - make_interval(days => $1::int)", LOG_KEEP_ERROR_DAYS,
    )
    # Старые снимки баланса: оставляем последний за каждый день на аккаунт
    out["balance_snapshots_thinned"] = await _delete_batched(
        conn, "balance_snapshots",
        """captured_at < now() - make_interval(days => $1::int) and id not in (
               select distinct on (account_id, (captured_at at time zone 'Europe/Kyiv')::date) id
                 from balance_snapshots
                order by account_id, (captured_at at time zone 'Europe/Kyiv')::date, captured_at desc)""",
        SNAPSHOT_THIN_AFTER_DAYS,
    )
    # Сырые события: итоги уже лежат в daily_stats, их не трогаем
    for table, col in (("promo_events", "found_at"), ("task_events", "completed_at"),
                       ("click_events", "clicked_at"), ("daily_bonus_events", "claimed_at")):
        out[table] = await _delete_batched(
            conn, table, f"{col} < now() - make_interval(days => $1::int)", EVENTS_KEEP_DAYS,
        )
    return out


async def run_cleanup_if_due() -> bool:
    """Выполняет очистку, если с прошлой прошло >= CLEANUP_INTERVAL_DAYS. True — выполнена."""
    conn = await _connect()
    try:
        await conn.execute(
            "insert into maintenance_state (key) values ('last_cleanup') on conflict (key) do nothing"
        )
        # Атомарный «захват» запуска: если два инстанса — очищает только один.
        prev = await conn.fetchrow(
            """update maintenance_state m set value = now()
                where m.key = 'last_cleanup'
                  and (m.value is null or m.value < now() - make_interval(days => $1::int))
            returning (select value from maintenance_state where key = 'last_cleanup') as prev""",
            CLEANUP_INTERVAL_DAYS,
        )
        if prev is None:
            return False
        try:
            stats = await cleanup(conn)
        except Exception:
            # не удалось — откатываем дату, чтобы повторить на следующей проверке
            await conn.execute("update maintenance_state set value = $1 where key = 'last_cleanup'", prev["prev"])
            raise
        summary = ", ".join(f"{k}={v}" for k, v in stats.items())
        await conn.execute("update maintenance_state set note = $1 where key = 'last_cleanup'", summary[:500])
        logger.info("DB: очистка выполнена: %s", summary)
        for t in ("app_logs", "balance_snapshots"):
            try:
                await conn.execute(f'vacuum (analyze) "{t}"')
            except Exception as e:
                logger.debug("DB: vacuum %s: %s", t, e)
        return True
    finally:
        await conn.close()


async def maintenance_loop():
    """Фоновая задача для main.py: проверяет «пора ли чистить» при старте и каждые 6 часов."""
    if not enabled():
        return
    await asyncio.sleep(120)  # не мешаем старту аккаунтов
    while True:
        try:
            await run_cleanup_if_due()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("DB: ошибка очистки: %s", e)
        await asyncio.sleep(CLEANUP_CHECK_EVERY_SEC)
