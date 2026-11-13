
"""
Схема БД проекта в одном месте.

Из неё db_migrate.py при старте приложения САМ создаёт недостающие таблицы
и колонки (CREATE TABLE IF NOT EXISTS + ALTER TABLE ... ADD COLUMN IF NOT EXISTS).
Нужна новая колонка — добавь строку в COLUMNS нужной таблицы, при следующем
запуске она появится. Ничего не удаляется и не переименовывается.

Тот же SQL можно получить файлом: `python db_schema.py > schema.sql` и вставить
в Supabase -> SQL Editor (запасной путь, если DATABASE_URL не задан).

Таблицы/колонки восстановлены по тому, что читает и пишет код
(supabase_db.py и /api/admin/overview в main.py).
"""

# Порядок важен: accounts первой (на неё ссылаются остальные).
# Формат колонки: (имя, определение SQL).
TABLES = {
    "accounts": [
        ("account_number", "integer not null"),
        ("name", "text"),
        ("enabled", "boolean not null default true"),
        ("connected", "boolean not null default false"),
        ("last_connected_at", "timestamptz"),
        ("created_at", "timestamptz not null default now()"),
        ("updated_at", "timestamptz not null default now()"),
    ],
    "app_logs": [
        ("account_id", "bigint references accounts(id) on delete cascade"),
        ("level", "text not null default 'INFO'"),
        ("event_type", "text"),
        ("message", "text"),
        ("error", "text"),
        ("metadata", "jsonb not null default '{}'::jsonb"),
        ("created_at", "timestamptz not null default now()"),
    ],
    "balance_snapshots": [
        ("account_id", "bigint references accounts(id) on delete cascade"),
        ("balance", "numeric(12,2) not null default 0"),
        ("captured_at", "timestamptz not null default now()"),
    ],
    "daily_stats": [
        ("stat_date", "date not null default current_date"),
        ("account_id", "bigint references accounts(id) on delete cascade"),
        ("clicks", "integer not null default 0"),
        ("tasks_completed", "integer not null default 0"),
        ("mini_apps_completed", "integer not null default 0"),
        ("daily_bonuses", "integer not null default 0"),
        ("promo_codes_found", "integer not null default 0"),
        ("promo_codes_activated", "integer not null default 0"),
        ("promo_codes_failed", "integer not null default 0"),
        ("stars_from_clicks", "numeric(12,2) not null default 0"),
        ("stars_from_tasks", "numeric(12,2) not null default 0"),
        ("stars_from_promos", "numeric(12,2) not null default 0"),
        ("stars_from_daily", "numeric(12,2) not null default 0"),
        ("total_stars", "numeric(12,2) not null default 0"),
        ("updated_at", "timestamptz not null default now()"),
    ],
    "promo_events": [
        ("account_id", "bigint references accounts(id) on delete cascade"),
        ("promo_code", "text"),
        ("found_at", "timestamptz not null default now()"),
        ("activated_at", "timestamptz"),
        ("status", "text not null default 'found'"),
        ("error_reason", "text"),
        ("stars", "numeric(12,2) not null default 0"),
    ],
    "task_events": [
        ("account_id", "bigint references accounts(id) on delete cascade"),
        ("task_type", "text"),
        ("completed_at", "timestamptz not null default now()"),
        ("stars", "numeric(12,2) not null default 0"),
    ],
    "job_schedule": [
        ("account_number", "integer not null"),
        ("job", "text not null"),
        ("next_run_at", "timestamptz"),
        ("last_run_at", "timestamptz"),
        ("last_status", "text"),
        ("last_message", "text"),
        ("data", "jsonb not null default '{}'::jsonb"),
        ("updated_at", "timestamptz not null default now()"),
    ],
    "resource_samples": [
        ("ts", "timestamptz not null default now()"),
        ("rss_mb", "numeric(8,1)"),
        ("cpu_pct", "numeric(7,2)"),
        ("accounts", "integer"),
        ("tasks", "integer"),
        ("boot", "bigint"),
    ],
    "withdrawals": [
        ("account_number", "integer not null"),
        ("gift_code", "text"),
        ("gift_emoji", "text"),
        ("gift_name", "text"),
        ("stars", "numeric(12,2) not null default 0"),
        ("friend_id", "text"),
        ("status", "text not null default 'running'"),
        ("error", "text"),
        ("balance_before", "numeric(12,2)"),
        ("balance_after", "numeric(12,2)"),
        ("steps", "jsonb not null default '[]'::jsonb"),
        ("bot_response", "text"),
        ("created_at", "timestamptz not null default now()"),
        ("finished_at", "timestamptz"),
    ],
    "bot_knowledge": [
        ("fingerprint", "text"),
        ("kind", "text not null default 'screen'"),
        ("title", "text"),
        ("sample_text", "text"),
        ("data", "jsonb not null default '{}'::jsonb"),
        ("seen_count", "integer not null default 1"),
        ("first_seen", "timestamptz not null default now()"),
        ("last_seen", "timestamptz not null default now()"),
        ("last_account", "text"),
    ],
    "click_events": [
        ("account_id", "bigint references accounts(id) on delete cascade"),
        ("clicked_at", "timestamptz not null default now()"),
        ("stars", "numeric(12,2) not null default 0"),
    ],
    "daily_bonus_events": [
        ("account_id", "bigint references accounts(id) on delete cascade"),
        ("claimed_at", "timestamptz not null default now()"),
        ("stars", "numeric(12,2) not null default 0"),
    ],
    # Служебная таблица: когда в последний раз выполнялась очистка и т.п.
    "maintenance_state": [
        ("value", "timestamptz"),
        ("note", "text"),
    ],
}

# Таблицы, у которых первичный ключ — не bigint identity "id", а текстовый "key".
TEXT_KEY_TABLES = {"maintenance_state"}

# Индексы (idempotent). Уникальные нужны для ON CONFLICT в record_patrick_event
# и для сидирования accounts.
INDEXES = [
    "create unique index if not exists accounts_account_number_key on accounts (account_number)",
    "create unique index if not exists daily_stats_date_account_key on daily_stats (stat_date, account_id)",
    "create unique index if not exists job_schedule_acc_job_key on job_schedule (account_number, job)",
    "create index if not exists withdrawals_created_idx on withdrawals (created_at desc)",
    "create index if not exists resource_samples_ts_idx on resource_samples (ts desc)",
    "create unique index if not exists bot_knowledge_fp_key on bot_knowledge (fingerprint)",
    "create index if not exists bot_knowledge_kind_seen_idx on bot_knowledge (kind, last_seen desc)",
    "create index if not exists app_logs_created_at_idx on app_logs (created_at desc)",
    "create index if not exists app_logs_level_created_idx on app_logs (level, created_at)",
    "create index if not exists balance_snapshots_acc_time_idx on balance_snapshots (account_id, captured_at desc)",
    "create index if not exists promo_events_found_at_idx on promo_events (found_at desc)",
    "create index if not exists promo_events_acc_code_idx on promo_events (account_id, promo_code)",
    "create index if not exists task_events_completed_at_idx on task_events (completed_at desc)",
    "create index if not exists click_events_clicked_at_idx on click_events (clicked_at desc)",
    "create index if not exists daily_bonus_events_claimed_at_idx on daily_bonus_events (claimed_at desc)",
]

# Сервис-роль Supabase обходит RLS; включаем RLS без политик, чтобы данные
# нельзя было прочитать публичным (anon) ключом через REST.
RLS_TABLES = list(TABLES)

TRIGGER_SQL = [
    """
    create or replace function patrick_touch_updated_at() returns trigger
    language plpgsql as $$
    begin
        new.updated_at = now();
        return new;
    end $$
    """,
    "drop trigger if exists accounts_touch_updated_at on accounts",
    """create trigger accounts_touch_updated_at before update on accounts
       for each row execute function patrick_touch_updated_at()""",
]

# RPC, которую вызывает supabase_db.SupabaseStore.event().
# Типы событий: click, task, mini_app, daily_bonus, promo_found,
# promo_activated, promo_failed. День статистики — по Киеву (как в админке).
RECORD_EVENT_SQL = """
create or replace function record_patrick_event(
    p_account_id bigint,
    p_event_type text,
    p_stars numeric default 0,
    p_task_type text default null,
    p_promo_code text default null,
    p_status text default null,
    p_error_reason text default null
) returns void
language plpgsql
as $$
declare
    v_day date := (now() at time zone 'Europe/Kyiv')::date;
    v_stars numeric := coalesce(p_stars, 0);
    v_promo_id bigint;
begin
    insert into daily_stats (stat_date, account_id)
    values (v_day, p_account_id)
    on conflict (stat_date, account_id) do nothing;

    if p_event_type = 'click' then
        update daily_stats set clicks = clicks + 1,
               stars_from_clicks = stars_from_clicks + v_stars
         where stat_date = v_day and account_id = p_account_id;
        insert into click_events (account_id, stars) values (p_account_id, v_stars);

    elsif p_event_type = 'task' then
        update daily_stats set tasks_completed = tasks_completed + 1,
               stars_from_tasks = stars_from_tasks + v_stars
         where stat_date = v_day and account_id = p_account_id;
        insert into task_events (account_id, task_type, stars)
        values (p_account_id, coalesce(p_task_type, 'task'), v_stars);

    elsif p_event_type = 'mini_app' then
        update daily_stats set mini_apps_completed = mini_apps_completed + 1,
               stars_from_tasks = stars_from_tasks + v_stars
         where stat_date = v_day and account_id = p_account_id;
        insert into task_events (account_id, task_type, stars)
        values (p_account_id, coalesce(p_task_type, 'mini_app'), v_stars);

    elsif p_event_type = 'daily_bonus' then
        update daily_stats set daily_bonuses = daily_bonuses + 1,
               stars_from_daily = stars_from_daily + v_stars
         where stat_date = v_day and account_id = p_account_id;
        insert into daily_bonus_events (account_id, stars) values (p_account_id, v_stars);

    elsif p_event_type = 'promo_found' then
        update daily_stats set promo_codes_found = promo_codes_found + 1
         where stat_date = v_day and account_id = p_account_id;
        insert into promo_events (account_id, promo_code, status)
        values (p_account_id, p_promo_code, coalesce(p_status, 'found'));

    elsif p_event_type in ('promo_activated', 'promo_failed') then
        if p_event_type = 'promo_activated' then
            update daily_stats set promo_codes_activated = promo_codes_activated + 1,
                   stars_from_promos = stars_from_promos + v_stars
             where stat_date = v_day and account_id = p_account_id;
        else
            update daily_stats set promo_codes_failed = promo_codes_failed + 1
             where stat_date = v_day and account_id = p_account_id;
        end if;

        -- закрываем самую свежую «found»-запись этого кода, иначе создаём новую
        select id into v_promo_id from promo_events
         where account_id = p_account_id and promo_code = p_promo_code and status = 'found'
         order by found_at desc limit 1;

        if v_promo_id is not null then
            update promo_events
               set status = case p_event_type when 'promo_activated' then 'activated' else 'failed' end,
                   activated_at = case p_event_type when 'promo_activated' then now() else null end,
                   error_reason = p_error_reason,
                   stars = v_stars
             where id = v_promo_id;
        else
            insert into promo_events (account_id, promo_code, status, activated_at, error_reason, stars)
            values (p_account_id, p_promo_code,
                    case p_event_type when 'promo_activated' then 'activated' else 'failed' end,
                    case p_event_type when 'promo_activated' then now() else null end,
                    p_error_reason, v_stars);
        end if;
    else
        return;  -- неизвестный тип: ничего не пишем и не падаем
    end if;

    update daily_stats
       set total_stars = stars_from_clicks + stars_from_tasks + stars_from_promos + stars_from_daily,
           updated_at = now()
     where stat_date = v_day and account_id = p_account_id;
end $$
"""


# «Обучение»: upsert записи о том, что отдал бот (см. knowledge.py).
KNOWLEDGE_SQL = """
create or replace function patrick_knowledge_upsert(
    p_fingerprint text, p_kind text, p_title text, p_text text, p_data jsonb, p_account text
) returns void
language plpgsql
as $$
begin
    insert into bot_knowledge (fingerprint, kind, title, sample_text, data, last_account)
    values (p_fingerprint, p_kind, p_title, p_text, coalesce(p_data, '{}'::jsonb), p_account)
    on conflict (fingerprint) do update
       set seen_count = bot_knowledge.seen_count + 1,
           last_seen = now(),
           title = excluded.title,
           sample_text = excluded.sample_text,
           data = excluded.data,
           last_account = excluded.last_account;
end $$
"""

KNOWLEDGE_GRANTS_SQL = """
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function patrick_knowledge_upsert(text, text, text, text, jsonb, text)
            from public, anon, authenticated;
        grant execute on function patrick_knowledge_upsert(text, text, text, text, jsonb, text)
            to service_role;
    end if;
end $$
"""


# Чтобы публичный (anon) ключ не мог дёргать RPC и портить статистику. На обычном
# Postgres без ролей Supabase блок ничего не делает.
RECORD_EVENT_GRANTS_SQL = """
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function record_patrick_event(bigint, text, numeric, text, text, text, text)
            from public, anon, authenticated;
        grant execute on function record_patrick_event(bigint, text, numeric, text, text, text, text)
            to service_role;
    end if;
end $$
"""


# В новых проектах Supabase таблицы, созданные SQL-ом, могут не получать права
# автоматически — даём service_role (ключ бота) явно. На обычном Postgres — no-op.
SERVICE_ROLE_GRANTS_SQL = """
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant usage on schema public to service_role;
        grant all on all tables in schema public to service_role;
        grant all on all sequences in schema public to service_role;
    end if;
end $$
"""


def create_table_sql(table: str) -> str:
    if table in TEXT_KEY_TABLES:
        return f'create table if not exists "{table}" ("key" text primary key)'
    return f'create table if not exists "{table}" ("id" bigint generated by default as identity primary key)'


def statements() -> list:
    """Все DDL-операторы по порядку (каждый идемпотентен)."""
    out = [create_table_sql(t) for t in TABLES]
    for table, cols in TABLES.items():
        for name, ddl in cols:
            out.append(f'alter table "{table}" add column if not exists "{name}" {ddl}')
    out += INDEXES
    out += [f'alter table "{t}" enable row level security' for t in RLS_TABLES]
    out.append(SERVICE_ROLE_GRANTS_SQL.strip())
    out += [s.strip() for s in TRIGGER_SQL]
    out.append(RECORD_EVENT_SQL.strip())
    out.append(RECORD_EVENT_GRANTS_SQL.strip())
    out.append(KNOWLEDGE_SQL.strip())
    out.append(KNOWLEDGE_GRANTS_SQL.strip())
    out.append("notify pgrst, 'reload schema'")  # PostgREST сразу увидит новые колонки/функцию
    return out


def render_sql() -> str:
    return ";\n\n".join(statements()) + ";\n"


if __name__ == "__main__":
    print(render_sql())
