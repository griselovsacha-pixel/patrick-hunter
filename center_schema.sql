-- Центр аккаунтов: таблицы center_*. ЗАПУСКАТЬ НЕ ОБЯЗАТЕЛЬНО — модуль создаёт их сам при старте.
-- Основная таблица проекта `accounts` и файл schema.sql НЕ меняются.
-- Если раньше ты запускал старый schema.sql центра и создал таблицы accounts/account_logs/warmup_tasks/settings —
-- они не нужны (а `accounts` мог конфликтовать с таблицей бота): проверь и удали лишнее вручную.

create table if not exists center_settings(key text primary key, value jsonb not null default '{}'::jsonb);
create table if not exists center_accounts(
  id serial primary key, label text, phone text, tg_id bigint unique, username text, first_name text, last_name text,
  session_enc text not null,
  status text not null default 'new',            -- new | warming | ready | working | banned | dead
  lang text default 'ru', country text, proxy text, proxy_on boolean default false,
  proxy_status text default 'unknown', proxy_ms int, has_2fa boolean,
  spam_status text default 'unknown', spam_text text, spam_checked_at timestamptz,
  avatar text, registered_at date, notes text,
  farm_enabled boolean default false, warmup_enabled boolean default false, warmup_started_at timestamptz,
  channels jsonb default '[]'::jsonb, profile_plan jsonb default '{}'::jsonb, wstate jsonb default '{}'::jsonb,
  api_id int, api_hash text, device jsonb default '{}'::jsonb,
  funnel_warmup_until timestamptz, moved_at timestamptz,
  last_error text, last_seen_ok timestamptz, created_at timestamptz default now());
create table if not exists center_logs(
  id bigserial primary key, account_id int references center_accounts(id) on delete cascade,
  ts timestamptz default now(), kind text, text text);
create index if not exists center_logs_acc_ts on center_logs(account_id, ts desc);
create table if not exists center_tasks(
  id bigserial primary key, account_id int references center_accounts(id) on delete cascade,
  run_at timestamptz not null, kind text not null, payload jsonb default '{}'::jsonb,
  status text default 'pending', result text, done_at timestamptz);
create index if not exists center_tasks_due on center_tasks(status, run_at);
create index if not exists center_tasks_acc on center_tasks(account_id, run_at desc);
alter table center_accounts enable row level security;
alter table center_logs enable row level security;
alter table center_tasks enable row level security;
alter table center_settings enable row level security;;
