"""
Центр аккаунтов v4 — подготовка купленных аккаунтов к основной воронке (FastAPI-модуль Patrick Hunter).

Подключение (уже сделано в main.py):
    import accounts_center
    accounts_center.setup(app, check_admin_auth)      # роуты /accounts и /api/accounts/*
    await accounts_center.start()                      # в lifespan: пул БД, таблицы, планировщик
    await accounts_center.funnel_accounts()            # аккаунты, перенесённые в воронку

ENV: DATABASE_URL (уже есть), FERNET_KEY (новый, обязателен), GEMINI_API_KEY (для комментариев),
     TELEGRAM_API_ID / TELEGRAM_API_HASH (берутся из config.py).
pip: telethon asyncpg aiohttp cryptography    (+ opentele — только если нужен импорт tdata)

Таблицы центра имеют префикс center_ — основная таблица accounts проекта НЕ затрагивается.
"""
import asyncio, base64, io, json, logging, math, os, random, re, secrets, shutil, tempfile, time, zipfile
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from telethon import TelegramClient, functions, types, errors
from telethon.sessions import StringSession

try:
    from cryptography.fernet import Fernet
except Exception:                                   # pragma: no cover
    Fernet = None

logger = logging.getLogger("accounts_center")

API_ID = int(os.getenv("TELEGRAM_API_ID", "31206776"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "7ca69453a014d6a87d67afe4d9df8284")
DSN = (os.getenv("DATABASE_URL") or "").strip()
import gemini_router
GEMINI_KEY = gemini_router.router.configured()   # есть ли ключи (GEMINI_API_KEY / GEMINI_API_KEYS)
_FKEY = (os.getenv("FERNET_KEY") or "").strip()
fernet = Fernet(_FKEY.encode()) if (Fernet and _FKEY) else None

CLIENT_PROVIDER = None    # fn(row) -> уже подключённый клиент воронки или None (не открываем вторую сессию)
FUNNEL_STATUS = None      # fn() -> {center_id: {...}} состояние аккаунтов в основной воронке (ставит main.py)
FUNNEL_BASE = 100         # номер аккаунта в основном боте = FUNNEL_BASE + id в центре
KYIV = ZoneInfo("Europe/Kyiv")
HERE = Path(__file__).parent
pool: asyncpg.Pool = None
pending: dict = {}
RECENT: list = []
_runner = None
_check_auth = None
FLOOD_WORDS = ("no limits", "good news", "свободен от", "вільний від", "не обмежен", "на вашем аккаунте нет ограничений")
DEAD_ERRORS = tuple(getattr(errors, n) for n in (
    "AuthKeyUnregisteredError", "AuthKeyDuplicatedError", "SessionRevokedError", "SessionExpiredError",
    "UserDeactivatedError", "UserDeactivatedBanError") if hasattr(errors, n))
DEVICES = [("Samsung Galaxy S23", "SDK 34", "10.14.5"), ("Xiaomi 13", "SDK 33", "10.14.4"),
           ("Google Pixel 7", "SDK 34", "10.14.5"), ("Samsung Galaxy A54", "SDK 34", "10.14.3")]

DEFAULT_PLAN = {
    "days": 7,
    "zones": {"morning": [7, 10], "day": [14, 17], "evening": [21, 23]},          # киевское время
    "day_plan": {   # zones — сколько сессий в этот день; posts — [мин, макс]; max_react — потолок реакций
        "1": {"zones": 2, "posts": [2, 3], "react": False, "comment": False, "max_react": 0},
        "2": {"zones": 2, "posts": [2, 4], "react": True, "comment": True, "max_react": 2},
        "default": {"zones": 3, "posts": [2, 7], "react": True, "comment": True, "max_react": 4}},
    "channels_per_session": [1, 3],
    "react_ratio": 0.55,          # 2 поста→1, 4→2, 7→4
    "skip_chance": 0.1,           # с 3 дня иногда пропускаем одну из трёх сессий (люди не идеальны)
    "profile_days": {"name": 4, "username": 4, "avatar": 4, "bio": 4},
    "profile_zones": {"name": "morning", "username": "morning", "avatar": "evening", "bio": "evening"},
    "profile_gap_min": 25,        # минимум минут между любыми двумя действиями одного аккаунта
    "funnel_days": 3,
    "channel_pool": [],
    "reactions": ["👍", "👍", "👍", "❤", "❤", "🔥", "😁"],
}
DEFAULT_GEMINI = {"enabled": True, "model": "", "temperature": 0.9,
    "prompt": ("Ты обычный читатель телеграм-канала и пишешь комментарий под постом. "
               "Пиши на том же языке, что и пост. Коротко (3-15 слов), живо, как реальный человек в чате: "
               "с маленькой буквы, без хэштегов, ссылок и упоминаний, эмодзи почти никогда. "
               "Реагируй на суть поста: мнение, короткий вопрос или реплика, не пересказывай пост и не хвали шаблонно. "
               "Верни только текст комментария. Если пост - реклама, розыгрыш, подборка ссылок или сказать нечего - верни ровно SKIP.")}

TABLES = ("center_accounts", "center_logs", "center_tasks", "center_settings")
DDL = """
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
alter table center_settings enable row level security;
"""
COUNTRY = {"1": "US/CA", "7": "RU/KZ", "380": "UA", "886": "TW", "86": "CN", "44": "GB", "91": "IN",
           "62": "ID", "55": "BR", "90": "TR", "84": "VN", "49": "DE", "33": "FR", "48": "PL", "375": "BY"}


# ───────────── утилиты ─────────────
def deep(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out

def _clamp(v, lo, hi, default):
    try: return max(lo, min(hi, type(default)(v)))
    except (TypeError, ValueError): return default

def sanitize_plan(p):
    """Защита от кривых значений из формы: сломанный план остановил бы планировщик."""
    d = DEFAULT_PLAN
    p["days"] = _clamp(p.get("days"), 1, 30, d["days"])
    for z, (a, b) in list(p["zones"].items()):
        a, b = _clamp(a, 0, 23, 7), _clamp(b, 1, 24, 10)
        p["zones"][z] = [a, b] if b > a else [a, min(24, a + 1)]
    for k, v in p["day_plan"].items():
        lo, hi = _clamp(v["posts"][0], 1, 30, 2), _clamp(v["posts"][1], 1, 30, 7)
        v["posts"] = [min(lo, hi), max(lo, hi)]
        v["zones"] = _clamp(v.get("zones"), 1, 3, 3); v["max_react"] = _clamp(v.get("max_react"), 0, 10, 0)
        v["react"], v["comment"] = bool(v.get("react")), bool(v.get("comment"))
    c0, c1 = _clamp(p["channels_per_session"][0], 1, 5, 1), _clamp(p["channels_per_session"][1], 1, 5, 3)
    p["channels_per_session"] = [min(c0, c1), max(c0, c1)]
    p["react_ratio"] = _clamp(p.get("react_ratio"), 0.0, 1.0, 0.55)
    p["skip_chance"] = _clamp(p.get("skip_chance"), 0.0, 0.5, 0.1)
    p["profile_gap_min"] = _clamp(p.get("profile_gap_min"), 5, 240, 25)
    p["funnel_days"] = _clamp(p.get("funnel_days"), 0, 30, 3)
    for f in d["profile_days"]:
        p["profile_days"][f] = _clamp(p["profile_days"].get(f), 1, 30, 4)
        if p["profile_zones"].get(f) not in p["zones"]: p["profile_zones"][f] = d["profile_zones"][f]
    p["reactions"] = [str(x).strip() for x in p.get("reactions") or [] if str(x).strip()] or d["reactions"]
    p["channel_pool"] = norm_channels(p.get("channel_pool"), 60)
    return p

async def plan_cfg(): return deep(DEFAULT_PLAN, await pool.fetchval("select value from center_settings where key='plan'"))
async def gem_cfg(): return {**DEFAULT_GEMINI, **(await pool.fetchval("select value from center_settings where key='gemini'") or {})}

def guess_country(phone):
    for n in (3, 2, 1):
        if phone and phone[:n] in COUNTRY: return COUNTRY[phone[:n]]

def parse_proxy(s):
    if not s: return None
    u = urlparse(s if "://" in s else "socks5://" + s)
    return (u.scheme.replace("socks5h", "socks5"), u.hostname, u.port, True, u.username, u.password)

def make_client(sess, proxy, lang, api_id=None, api_hash=None, device=None):
    lang = lang or "ru"; kw = {}
    if device: kw = {k: device[k] for k in ("device_model", "system_version", "app_version") if device.get(k)}
    return TelegramClient(StringSession(sess), api_id or API_ID, api_hash or API_HASH, proxy=parse_proxy(proxy),
                          lang_code=lang, system_lang_code=lang, connection_retries=2, request_retries=2, **kw)

def client_of(acc, sess):
    return make_client(sess, acc["proxy"] if acc["proxy_on"] else None, acc["lang"], acc["api_id"], acc["api_hash"], acc["device"])

def dec(acc): return fernet.decrypt(acc["session_enc"].encode()).decode()

def kyiv_now(): return datetime.now(KYIV)

def day_index(acc):
    if not acc["warmup_started_at"]: return None
    return (kyiv_now().date() - acc["warmup_started_at"].astimezone(KYIV).date()).days + 1

def pub(r):
    d = dict(r)
    for k in ("session_enc", "api_hash", "device"): d.pop(k, None)
    d["day"] = day_index(r)
    return d

def norm_channels(lst, limit=12):
    out = []
    for x in lst or []:
        x = str(x).strip().replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "").lstrip("@").split("/")[0].split("?")[0]
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", x) and "@" + x not in out: out.append("@" + x)
    return out[:limit]

async def log(aid, kind, text):
    try: await pool.execute("insert into center_logs(account_id,kind,text) values($1,$2,$3)", aid, kind, str(text)[:500])
    except Exception as e: logger.warning("log: %r", e)

async def get_acc(aid):
    r = await pool.fetchrow("select * from center_accounts where id=$1", aid)
    if not r: raise HTTPException(status_code=404, detail="нет такого аккаунта")
    return r

@asynccontextmanager
async def session_of(acc):
    shared = CLIENT_PROVIDER(acc) if CLIENT_PROVIDER else None
    if shared is not None and shared.is_connected():
        yield shared; return
    if acc["status"] == "working":
        raise RuntimeError("аккаунт работает в воронке и ещё не поднят там — подожди минуту (две сессии одновременно запрещены)")
    c = client_of(acc, dec(acc)); await c.connect()
    try:
        if not await c.is_user_authorized(): raise RuntimeError("сессия мертва")
        yield c
    finally:
        await c.disconnect()

async def funnel_accounts():
    """Для основного бота: аккаунты, перенесённые в воронку (сессия уже расшифрована)."""
    if pool is None or fernet is None: return []
    rows = await pool.fetch("select * from center_accounts where status='working' and farm_enabled order by id")
    out = []
    for r in rows:
        try: sess = dec(r)
        except Exception as e:
            logger.error("center #%s: не расшифровать сессию (%r) — проверь FERNET_KEY", r["id"], e); continue
        out.append({**pub(r), "session": sess, "api_id": r["api_id"] or API_ID, "api_hash": r["api_hash"] or API_HASH,
                    "proxy": r["proxy"] if r["proxy_on"] else None, "device": r["device"] or {},
                    "number": FUNNEL_BASE + r["id"]})
    return out


# ───────────── данные аккаунта ─────────────
async def check_spam(c):
    async with c.conversation("SpamBot", timeout=30) as conv:
        await conv.send_message("/start"); r = await conv.get_response()
    return ("free" if any(k in r.raw_text.lower() for k in FLOOD_WORDS) else "limited"), r.raw_text[:400]

async def ensure_spam(c, acc):
    last = acc["spam_checked_at"]
    if acc["spam_status"] in ("free", "limited") and last and datetime.now(timezone.utc) - last < timedelta(hours=12):
        return acc["spam_status"]
    st, txt = await check_spam(c)
    await pool.execute("update center_accounts set spam_status=$2, spam_text=$3, spam_checked_at=now() where id=$1", acc["id"], st, txt)
    return st

async def collect_info(aid, c, spam=False):
    me = await c.get_me(); av = None
    try:
        b = await c.download_profile_photo("me", file=bytes, download_big=False)
        if b: av = "data:image/jpeg;base64," + base64.b64encode(b).decode()
    except Exception: pass
    pw = await c(functions.account.GetPasswordRequest())
    q = dict(tg_id=me.id, username=me.username, first_name=me.first_name, last_name=me.last_name,
             phone=me.phone, avatar=av, has_2fa=bool(pw.has_password))
    if spam:
        st, txt = await check_spam(c)
        q.update(spam_status=st, spam_text=txt, spam_checked_at=datetime.now(timezone.utc))
    sets = ",".join(f"{k}=${i+2}" for i, k in enumerate(q))
    await pool.execute(f"update center_accounts set {sets}, last_seen_ok=now(), last_error=null where id=$1", aid, *q.values())

async def refresh(aid, spam=True):
    acc = await get_acc(aid); t0 = time.time()
    try:
        async with session_of(acc) as c:
            ms = int((time.time() - t0) * 1000); await collect_info(aid, c, spam)
        await pool.execute("update center_accounts set proxy_status=$2, proxy_ms=$3 where id=$1", aid,
                           "ok" if acc["proxy_on"] and acc["proxy"] else "off", ms)
        await log(aid, "info", "данные обновлены")
    except Exception as e:
        dead = isinstance(e, DEAD_ERRORS) or "мертва" in str(e)
        await pool.execute("""update center_accounts set last_error=$2, status=case when $3 then 'dead' else status end,
            proxy_status=case when proxy_on and not $3 then 'dead' else proxy_status end where id=$1""", aid, repr(e)[:300], dead)
        await log(aid, "error", f"обновление: {e!r}"); raise

async def save_new(c, proxy, lang, label=None):
    me = await c.get_me()
    sess = fernet.encrypt(StringSession.save(c.session).encode()).decode()
    ir = getattr(c, "_init_request", None)
    dev = {"device_model": ir.device_model, "system_version": ir.system_version, "app_version": ir.app_version} if ir else {}
    aid = await pool.fetchval("""
        insert into center_accounts(label,phone,tg_id,session_enc,lang,country,proxy,proxy_on,api_id,api_hash,device)
        values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        on conflict (tg_id) do update set session_enc=excluded.session_enc, last_error=null,
            status=case when center_accounts.status in ('dead','banned') then 'new' else center_accounts.status end
        returning id""", label or me.first_name or str(me.id), me.phone, me.id, sess, lang or "ru",
        guess_country(me.phone or ""), proxy or None, bool(proxy), c.api_id, c.api_hash, dev)
    await collect_info(aid, c); await log(aid, "info", "аккаунт добавлен"); await c.disconnect()
    return aid


# ───────────── Gemini ─────────────
def clean_comment(t):
    t = re.sub(r"^(комментарий|коментар|comment)\s*[:\-]\s*", "", t.strip(), flags=re.I)
    t = re.sub(r"https?://\S+|t\.me/\S+|@\w+|#\w+", "", t)
    t = re.sub(r"[*_`>~]+", "", t); t = re.sub(r"\s+", " ", t).strip().strip("\"'«»“”").strip()
    if len(t) > 140:
        m = re.match(r"(.{8,140}?[.!?…])(\s|$)", t)
        t = m.group(1) if m else t[:140].rsplit(" ", 1)[0]
    if t.endswith(".") and random.random() < 0.7: t = t[:-1]
    return t

async def gemini_comment(post_text, g):
    """Комментарий через GeminiRouter (gemini_router.py): ранжирование моделей по скорости, хеджинг, ротация ключей."""
    avoid = ("\nНе повторяй эти комментарии:\n- " + "\n- ".join(RECENT[-8:])) if RECENT else ""
    raw, model = await gemini_router.router.generate(
        g["prompt"], f"{avoid}\n\nПОСТ:\n{post_text[:1800]}",
        temperature=float(g["temperature"]), max_tokens=1024, prefer=(g.get("model") or "").strip() or None)
    if not raw or raw.upper().startswith("SKIP"): return "SKIP"
    t = clean_comment(raw)
    if len(t) < 3 or t in RECENT: return "SKIP"
    RECENT.append(t); del RECENT[:-20]
    return t


# ───────────── планировщик ─────────────
def zone_bounds(plan, z, d):
    h0, h1 = plan["zones"][z]
    end = datetime(d.year, d.month, d.day, tzinfo=KYIV) + timedelta(hours=h1)
    return (datetime(d.year, d.month, d.day, tzinfo=KYIV) + timedelta(hours=h0), end)

def pick_time(a, b, taken, gap_min, prev_clock=None):
    b = b - timedelta(minutes=10)
    if b <= a: return None
    for _ in range(40):
        t = a + timedelta(seconds=random.uniform(0, (b - a).total_seconds()))
        if any(abs((t - x).total_seconds()) < gap_min * 60 for x in taken): continue
        if prev_clock is not None and abs(t.hour * 60 + t.minute - prev_clock) < 25: continue   # не в то же время, что вчера
        return t
    return None

def has_profile_data(pp, f):
    return bool({"name": pp.get("first_name"), "username": pp.get("username"), "avatar": pp.get("avatar"), "bio": pp.get("bio")}[f])

async def plan_day(acc, plan):
    ws = dict(acc["wstate"] or {}); now = kyiv_now(); today = now.date()
    if ws.get("plan_date") == str(today): return
    d = day_index(acc)
    if d > plan["days"]:
        await pool.execute("update center_accounts set status='ready', warmup_enabled=false where id=$1", acc["id"])
        await pool.execute("delete from center_tasks where account_id=$1 and status='pending'", acc["id"])
        await log(acc["id"], "info", f"прогрев {plan['days']} дн. завершён → «готов», можно переносить в воронку"); return
    dc = plan["day_plan"].get(str(d)) or plan["day_plan"]["default"]
    earliest = now + timedelta(minutes=3)
    if not ws.get("planned_once"):                       # свежую сессию не дёргаем сразу после входа
        earliest = now + timedelta(minutes=random.randint(12, 35)); ws["planned_once"] = True
    ahead = [z for z in plan["zones"] if zone_bounds(plan, z, today)[1] - earliest > timedelta(minutes=25)]
    clocks, taken, tasks = dict(ws.get("clock") or {}), [], []
    if ahead:
        chosen = random.sample(ahead, min(dc["zones"], len(ahead)))
        if d >= 3 and len(chosen) >= 3 and random.random() < plan["skip_chance"]: chosen.remove(random.choice(chosen))
        cz = random.choice(chosen) if dc["comment"] else None
        for z in chosen:
            a, b = zone_bounds(plan, z, today)
            t = pick_time(max(a, earliest), b, taken, plan["profile_gap_min"], clocks.get(z))
            if t: taken.append(t); clocks[z] = t.hour * 60 + t.minute; tasks.append((t, "session", {"zone": z, "comment": z == cz}))
    pp, done = acc["profile_plan"] or {}, set(ws.get("profile_done") or [])
    for f in ("name", "username", "avatar", "bio"):
        if f in done or d < plan["profile_days"][f] or not has_profile_data(pp, f): continue
        z = plan["profile_zones"][f]
        if z not in ahead: continue
        a, b = zone_bounds(plan, z, today)
        t = pick_time(max(a, earliest), b, taken, plan["profile_gap_min"])
        if t: taken.append(t); tasks.append((t, "p_" + f, {"zone": z}))
    for t, kind, payload in tasks:
        await pool.execute("insert into center_tasks(account_id,run_at,kind,payload) values($1,$2,$3,$4)", acc["id"], t, kind, payload)
    ws.update(plan_date=str(today), clock=clocks)
    await pool.execute("update center_accounts set wstate=$2 where id=$1", acc["id"], ws)
    await log(acc["id"], "plan", f"день {d}: " + (", ".join(f"{k} {t.strftime('%H:%M')}" for t, k, _ in sorted(tasks, key=lambda x: x[0])) or "задач на сегодня нет"))


# ───────────── выполнение ─────────────
async def do_session(c, acc, ws, plan, p):
    d = day_index(acc); dc = plan["day_plan"].get(str(d)) or plan["day_plan"]["default"]
    chans = acc["channels"] or []
    if not chans: raise RuntimeError("не заданы каналы для чтения")
    lo, hi = plan["channels_per_session"]
    total = random.randint(*dc["posts"]); k = max(1, min(random.randint(lo, hi), len(chans), total))
    picked = random.sample(chans, k); counts = [1] * k
    for _ in range(total - k): counts[random.randrange(k)] += 1
    n_react = 0 if not dc["react"] else max(1, min(round(total * plan["react_ratio"]), dc["max_react"] or 99))
    read_log, seen = [], ws.setdefault("read", {})
    for ch, cnt in zip(picked, counts):
        try: ent = await c.get_entity(ch)
        except errors.FloodWaitError: raise
        except Exception as e: await log(acc["id"], "error", f"{ch}: {e!r}"); continue
        if ch not in ws.setdefault("joined", []):            # вступаем лениво, по одному каналу за раз
            try: await c(functions.channels.JoinChannelRequest(ent))
            except errors.FloodWaitError: raise
            except Exception: pass
            ws["joined"].append(ch); await asyncio.sleep(random.uniform(3, 9))
        old = set(seen.get(ch, []))
        cand = [m for m in await c.get_messages(ent, limit=40) if not m.action and m.id not in old]
        take = random.sample(cand, min(cnt, len(cand))); read_ids = []
        for m in sorted(take, key=lambda m: -m.id):
            await c(functions.messages.GetMessagesViewsRequest(peer=ent, id=[m.id], increment=True))
            read_ids.append(m.id); read_log.append((ent, ch, m))
            await asyncio.sleep(random.uniform(2, 6) + min(len(m.raw_text or ""), 700) / random.uniform(60, 110))
        if read_ids:
            await c.send_read_acknowledge(ent, max_id=max(read_ids))
            seen[ch] = (list(old) + read_ids)[-300:]
    if not read_log: return "нет новых постов"
    reacted = []
    if n_react:
        pool_ = read_log[:]; random.shuffle(pool_)
        for ent, ch, m in pool_:
            if len(reacted) >= n_react: break
            try:
                await c(functions.messages.SendReactionRequest(peer=ent, msg_id=m.id,
                        reaction=[types.ReactionEmoji(emoticon=random.choice(plan["reactions"]))]))
                reacted.append((ent, ch, m)); await asyncio.sleep(random.uniform(3, 10))
            except errors.FloodWaitError: raise
            except Exception: continue
    today = str(kyiv_now().date()); note = ""
    want = dc["comment"] and ws.get("comment_date") != today and (p.get("comment") or ws.get("comment_retry") == today)
    if want and reacted:
        note = await try_comment(c, acc, ws, reacted, today)
    elif want:
        ws["comment_retry"] = today
    return f"каналов {len(picked)}, постов {len(read_log)}, реакций {len(reacted)}" + (f", {note}" if note else "")

async def try_comment(c, acc, ws, reacted, today):
    if await ensure_spam(c, acc) == "limited":
        ws["comment_retry"] = None; return "комментарий пропущен: спамблок"
    g = await gem_cfg()
    if not (g["enabled"] and GEMINI_KEY): return "комментарий пропущен: Gemini выключен"
    hist = ws.setdefault("commented", {}); cutoff = str(kyiv_now().date() - timedelta(days=3))
    for k in [k for k, v in hist.items() if v < cutoff]: hist.pop(k)
    # свежие посты, каналы без недавних комментариев — вперёд
    order = sorted(reacted, key=lambda x: (x[1] in hist, random.random()))
    now_utc = datetime.now(timezone.utc)
    for ent, ch, m in order:
        if not (getattr(m.replies, "comments", False) and (m.raw_text or "").strip()): continue
        if m.date and now_utc - m.date > timedelta(hours=72): continue       # под старыми постами не пишем
        try:
            text = await gemini_comment(m.raw_text, g)
            if text == "SKIP": continue
            try:
                r = await c(functions.messages.GetDiscussionMessageRequest(peer=ent, msg_id=m.id))
                disc = next((x for x in r.chats if getattr(x, "megagroup", False)), None)
                if disc and disc.id not in ws.setdefault("joined_disc", []):
                    try: await c(functions.channels.JoinChannelRequest(disc))
                    except errors.FloodWaitError: raise
                    except Exception: pass
                    ws["joined_disc"].append(disc.id); await asyncio.sleep(random.uniform(4, 10))
            except errors.FloodWaitError: raise
            except Exception: pass
            await asyncio.sleep(random.uniform(15, 60))                      # «читаем» пост и ветку
            async with c.action(ent, "typing"): await asyncio.sleep(min(9, 1.5 + len(text) * random.uniform(0.08, 0.16)))
            await c.send_message(ent, text, comment_to=m.id)
            ws["comment_date"] = today; ws["comment_retry"] = None; hist[ch] = today
            await log(acc["id"], "comment", f"{ch}: {text}")
            return "комментарий оставлен"
        except errors.FloodWaitError: raise
        except Exception as e:
            await log(acc["id"], "error", f"комментарий {ch}: {e!r}")
    ws["comment_retry"] = today
    return "комментарий: не нашлось подходящего поста, попробую в следующей сессии"

async def do_profile(c, acc, ws, kind):
    f, pp = kind[2:], acc["profile_plan"] or {}
    if f == "name":
        await c(functions.account.UpdateProfileRequest(first_name=pp["first_name"], last_name=pp.get("last_name") or ""))
    elif f == "username":
        try: await c(functions.account.UpdateUsernameRequest(pp["username"].lstrip("@")))
        except errors.UsernameOccupiedError: raise RuntimeError(f"юзернейм @{pp['username']} занят — задай другой")
    elif f == "bio":
        await c(functions.account.UpdateProfileRequest(about=pp["bio"][:70]))
    elif f == "avatar":
        raw = base64.b64decode(pp["avatar"].split(",", 1)[1])
        up = await c.upload_file(io.BytesIO(raw), file_name="photo.jpg")
        await c(functions.photos.UploadProfilePhotoRequest(file=up))
    ws.setdefault("profile_done", []).append(f)
    await collect_info(acc["id"], c)
    return f"профиль: {f} установлен"

async def run_task(t, plan):
    acc = await get_acc(t["account_id"]); ws = dict(acc["wstate"] or {}); status, res = "done", "ok"
    await pool.execute("update center_tasks set status='running' where id=$1", t["id"])
    pause = False
    try:
        if (ws.get("wait_until") or 0) > time.time(): status, res = "missed", "пропущено: flood-wait"
        else:
            async def job():
                async with session_of(acc) as c:
                    return await (do_session(c, acc, ws, plan, t["payload"]) if t["kind"] == "session" else do_profile(c, acc, ws, t["kind"]))
            res = await asyncio.wait_for(job(), 900)
            ws["fails"] = 0
            await pool.execute("update center_accounts set last_seen_ok=now(), last_error=null where id=$1", acc["id"])
    except errors.FloodWaitError as e:
        ws["wait_until"] = time.time() + e.seconds + 60; status, res = "failed", f"FloodWait {e.seconds}с"
    except Exception as e:
        status, res = "failed", (repr(e) if str(e) == "" else f"{type(e).__name__}: {e}")[:300]
        dead = isinstance(e, DEAD_ERRORS) or "мертва" in str(e)
        if t["kind"] == "session" and not dead:
            ws["fails"] = ws.get("fails", 0) + 1
            pause = ws["fails"] >= 3                                  # 3 сбоя подряд — не долбим аккаунт дальше
        await pool.execute("update center_accounts set last_error=$2, status=case when $3 then 'dead' else status end where id=$1", acc["id"], res, dead)
    await pool.execute("update center_accounts set wstate=$2 where id=$1", acc["id"], ws)
    await pool.execute("update center_tasks set status=$2, result=$3, done_at=now() where id=$1", t["id"], status, res)
    await log(acc["id"], "task" if status == "done" else "error", f"{t['kind']}: {res}")
    if pause:
        await pool.execute("update center_accounts set warmup_enabled=false where id=$1", acc["id"])
        await pool.execute("delete from center_tasks where account_id=$1 and status='pending'", acc["id"])
        await log(acc["id"], "error", "прогрев поставлен на паузу: 3 неудачные сессии подряд. Проверь прокси/аккаунт и запусти снова")

async def runner_loop():
    await asyncio.sleep(15); last_clean = 0
    while True:
        try:
            plan = await plan_cfg()
            for acc in await pool.fetch("select * from center_accounts where status='warming' and warmup_enabled and warmup_started_at is not null"):
                try: await plan_day(acc, plan)
                except Exception as e: logger.warning("plan_day %s: %r", acc["id"], e)
            await pool.execute("update center_tasks set status='missed', result='пропущено: время вышло (сервер спал?)' where status='pending' and run_at < now()-interval '45 minutes'")
            await pool.execute("update center_tasks set status='failed', result='завис' where status='running' and run_at < now()-interval '30 minutes'")
            t = await pool.fetchrow("""select t.* from center_tasks t join center_accounts a on a.id=t.account_id
                where t.status='pending' and t.run_at<=now() and a.status='warming' and a.warmup_enabled order by t.run_at limit 1""")
            if t:
                await run_task(t, plan); await asyncio.sleep(random.uniform(20, 90)); continue   # аккаунты строго по очереди
            if time.time() - last_clean > 120:
                last_clean = time.time()
                for k in [k for k, v in pending.items() if time.time() - v["t"] > 600]:
                    try: await pending.pop(k)["c"].disconnect()
                    except Exception: pass
                await pool.execute("delete from center_logs where ts < now() - interval '30 days'")
                await pool.execute("delete from center_tasks where run_at < now() - interval '14 days'")
        except asyncio.CancelledError: raise
        except Exception as e:
            logger.error("runner_loop: %r", e)
        await asyncio.sleep(30)


# ───────────── жизненный цикл ─────────────
def ready() -> bool: return pool is not None and fernet is not None

async def start():
    global pool, _runner
    if not DSN: logger.warning("Центр аккаунтов отключён: нет DATABASE_URL"); return False
    if fernet is None: logger.warning("Центр аккаунтов отключён: нет FERNET_KEY (или не установлен cryptography)"); return False
    async def init(conn): await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, statement_cache_size=0, init=init, command_timeout=60)
    async with pool.acquire() as conn:
        for stmt in [s.strip() for s in DDL.split(";") if s.strip()]:
            try: await conn.execute(stmt)
            except Exception as e: logger.error("center DDL: %s | %s", str(e)[:150], " ".join(stmt.split())[:90])
    _runner = asyncio.create_task(runner_loop())
    logger.info("Центр аккаунтов запущен")
    return True

async def stop():
    global pool, _runner
    if _runner: _runner.cancel()
    for v in list(pending.values()):
        try: await v["c"].disconnect()
        except Exception: pass
    pending.clear()
    try: await gemini_router.router.aclose()
    except Exception: pass
    if pool: await pool.close(); pool = None


# ───────────── HTTP ─────────────
async def _auth(authorization: str | None = Header(default=None)):
    if _check_auth: _check_auth(authorization)
    if not ready():
        raise HTTPException(status_code=503, detail="Центр аккаунтов выключен: задай FERNET_KEY и DATABASE_URL в Render и перезапусти сервис")

router = APIRouter(prefix="/api/accounts", dependencies=[Depends(_auth)])

def J(o, status=200):
    return Response(json.dumps(o, default=str, ensure_ascii=False), status_code=status, media_type="application/json")

def bad(e, status=400):
    msg = e.detail if isinstance(e, HTTPException) else (str(e) or repr(e))
    return J({"error": msg}, status)

def handler(fn):
    import functools
    @functools.wraps(fn)
    async def w(*a, **kw):
        try: return await fn(*a, **kw)
        except HTTPException as e: return bad(e, e.status_code)
        except errors.PhoneCodeInvalidError: return bad("неверный код")
        except errors.PhoneCodeExpiredError: return bad("код устарел — запроси новый")
        except errors.PasswordHashInvalidError: return bad("неверный пароль 2FA")
        except errors.FloodWaitError as e: return bad(f"Telegram просит подождать {e.seconds} с")
        except Exception as e: logger.warning("%s: %r", fn.__name__, e); return bad(e)
    return w

async def h_list():
    rows = await pool.fetch("""select a.*,
        (select min(run_at) from center_tasks t where t.account_id=a.id and t.status='pending' and t.run_at>now()) as next_at,
        (select count(*) from center_tasks t where t.account_id=a.id and t.kind='session' and t.status='done'
            and (t.run_at at time zone 'Europe/Kyiv')::date=(now() at time zone 'Europe/Kyiv')::date) as today_done,
        (select count(*) from center_tasks t where t.account_id=a.id and t.kind='session'
            and (t.run_at at time zone 'Europe/Kyiv')::date=(now() at time zone 'Europe/Kyiv')::date) as today_total,
        (select count(*) from center_logs l where l.account_id=a.id and l.kind='comment') as comments_total
        from center_accounts a order by a.id""")
    farm = {}
    try: farm = FUNNEL_STATUS() if FUNNEL_STATUS else {}
    except Exception: pass
    return J([{**pub(r), "farm": farm.get(r["id"])} for r in rows])

async def h_logs(aid: int):
    return J([dict(r) for r in await pool.fetch("select ts,kind,text from center_logs where account_id=$1 order by ts desc limit 150", aid)])

async def h_tasks(aid: int):
    return J([dict(r) for r in await pool.fetch("""select id,kind,run_at,status,result,payload from center_tasks
        where account_id=$1 and run_at > now() - interval '3 days' order by run_at desc limit 80""", aid)])

async def h_patch(aid: int, req: Request):
    d = await req.json(); acc = await get_acc(aid); q = {}
    for k in ("label", "lang", "notes", "proxy_on"):
        if k in d: q[k] = d[k]
    if "status" in d and d["status"] in ("new", "warming", "ready", "working", "banned", "dead"):
        if d["status"] == "working" and acc["status"] != "working": raise HTTPException(400, "в воронку переносят кнопкой «В основную воронку»")
        q["status"] = d["status"]
    for k in ("country", "proxy"):
        if k in d: q[k] = (d[k] or "").strip() or None
    if "registered_at" in d: q["registered_at"] = datetime.strptime(d["registered_at"], "%Y-%m-%d").date() if d["registered_at"] else None
    if "channels" in d: q["channels"] = norm_channels(d["channels"])
    if "profile_plan" in d:
        pp = d["profile_plan"]; pp["username"] = (pp.get("username") or "").lstrip("@").strip()
        if pp["username"] and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", pp["username"]): raise HTTPException(400, "юзернейм: 5–32 символа, латиница/цифры/_, начинается с буквы")
        pp["bio"] = (pp.get("bio") or "")[:70]
        q["profile_plan"] = pp
    if q:
        sets = ",".join(f"{k}=${i+2}" for i, k in enumerate(q))
        await pool.execute(f"update center_accounts set {sets} where id=$1", aid, *q.values())
    return J(pub(await get_acc(aid)))

async def h_refresh(aid: int, spam: int = 0):
    await refresh(aid, spam=bool(spam)); return J(pub(await get_acc(aid)))

async def h_check_username(aid: int, req: Request):
    acc = await get_acc(aid); name = (await req.json())["username"].lstrip("@")
    async with session_of(acc) as c:
        ok = await c(functions.account.CheckUsernameRequest(name))
    return J({"free": bool(ok)})

async def start_warm(aid):
    acc = await get_acc(aid)
    if acc["status"] in ("working", "dead", "banned"): raise HTTPException(400, f"нельзя запускать прогрев: статус «{acc['status']}»")
    if not acc["channels"]: await auto_channels(aid, quiet=True)
    await pool.execute("""update center_accounts set status='warming', warmup_enabled=true,
        warmup_started_at=coalesce(warmup_started_at, now()) where id=$1""", aid)

async def h_warm(aid: int, act: str):
    acc = await get_acc(aid)
    if act == "start":
        await start_warm(aid)
    elif act == "stop":
        await pool.execute("update center_accounts set warmup_enabled=false where id=$1", aid)
    elif act == "replan":
        await pool.execute("delete from center_tasks where account_id=$1 and status='pending'", aid)
        await pool.execute("update center_accounts set wstate = wstate - 'plan_date' where id=$1", aid)
    elif act == "reset":
        if acc["status"] == "working": raise HTTPException(400, "аккаунт уже в воронке")
        await pool.execute("delete from center_tasks where account_id=$1", aid)
        await pool.execute("""update center_accounts set warmup_started_at=now(), wstate='{}'::jsonb, status='warming',
            warmup_enabled=true where id=$1""", aid)
    elif act == "next":
        await pool.execute("""update center_tasks set run_at=now() where id=(select id from center_tasks
            where account_id=$1 and status='pending' order by run_at limit 1)""", aid)
    else: raise HTTPException(400, "неизвестное действие")
    await log(aid, "info", f"прогрев: {act}")
    return J(pub(await get_acc(aid)))

async def h_bulk_start():
    ids = [r["id"] for r in await pool.fetch("select id from center_accounts where status='new' order by id")]
    n = 0
    for i in ids:
        try: await start_warm(i); await log(i, "info", "прогрев: start (массово)"); n += 1
        except Exception as e: logger.warning("bulk start %s: %r", i, e)
    return J({"started": n})

async def auto_channels(aid, quiet=False):
    poolc = norm_channels((await plan_cfg())["channel_pool"], 60)
    if len(poolc) < 5:
        if quiet: return
        raise HTTPException(400, "в общем пуле меньше 5 каналов (Настройки плана → пул каналов)")
    use = Counter()
    for r in await pool.fetch("select channels from center_accounts where id<>$1", aid):
        use.update(r["channels"] or [])
    pick = sorted(poolc, key=lambda x: (use[x], random.random()))[:5]       # реже всего занятые каналы — наборы не совпадают
    await pool.execute("update center_accounts set channels=$2 where id=$1", aid, pick)

async def h_channels_auto(aid: int):
    await auto_channels(aid); return J(pub(await get_acc(aid)))

async def h_channels_check(aid: int):
    acc = await get_acc(aid); res = []
    async with session_of(acc) as c:
        for ch in acc["channels"] or []:
            try:
                ent = await c.get_entity(ch)
                if not (isinstance(ent, types.Channel) and ent.broadcast):
                    res.append({"ch": ch, "ok": False, "err": "это не канал"}); continue
                full = await c(functions.channels.GetFullChannelRequest(ent))
                res.append({"ch": ch, "ok": True, "title": ent.title, "members": getattr(full.full_chat, "participants_count", None),
                            "comments": bool(getattr(full.full_chat, "linked_chat_id", None))})
            except errors.FloodWaitError: raise
            except Exception as e: res.append({"ch": ch, "ok": False, "err": type(e).__name__})
            await asyncio.sleep(random.uniform(1.5, 4))
    return J(res)

async def h_funnel(aid: int):
    acc = await get_acc(aid)
    if acc["status"] in ("dead", "banned"): raise HTTPException(400, "аккаунт мёртв — в воронку нельзя")
    if acc["status"] == "working": raise HTTPException(400, "аккаунт уже в воронке")
    days = (await plan_cfg())["funnel_days"]
    await pool.execute("""update center_accounts set status='working', farm_enabled=true, warmup_enabled=false, moved_at=now(),
        funnel_warmup_until=now() + make_interval(days => $2) where id=$1""", aid, days)
    await pool.execute("delete from center_tasks where account_id=$1 and status='pending'", aid)
    await log(aid, "info", f"перенесён в основную воронку (в боте №{FUNNEL_BASE + aid}); обычный прогрев в воронке {days} дн.")
    return J(pub(await get_acc(aid)))

async def h_delete(aid: int):
    acc = await get_acc(aid)
    if acc["status"] == "working": raise HTTPException(400, "аккаунт работает в воронке — сначала смени статус (напр. «пауза») и подожди минуту")
    await pool.execute("delete from center_accounts where id=$1", aid); return J({"ok": True})

async def h_settings_get():
    return J({"plan": await plan_cfg(), "gemini": await gem_cfg(), "gemini_key_set": bool(GEMINI_KEY), "funnel_base": FUNNEL_BASE})

async def h_settings_put(req: Request):
    d = await req.json()
    if "plan" in d: d["plan"] = sanitize_plan(deep(DEFAULT_PLAN, d["plan"]))
    if "gemini" in d:
        g = {**DEFAULT_GEMINI, **d["gemini"]}; g["temperature"] = _clamp(g.get("temperature"), 0.0, 2.0, 0.9)
        g["model"] = (g.get("model") or DEFAULT_GEMINI["model"]).strip(); d["gemini"] = g
    for k in ("plan", "gemini"):
        if k in d: await pool.execute("insert into center_settings(key,value) values($1,$2) on conflict(key) do update set value=excluded.value", k, d[k])
    return await h_settings_get()

async def h_gem_test(req: Request):
    d = await req.json(); g = {**await gem_cfg(), **(d.get("gemini") or {})}
    return J({"text": await gemini_comment(d["text"], g)})

# — вход —
async def h_phone(req: Request):
    d = await req.json(); phone = re.sub(r"\D", "", d["phone"]); dm, sv, av = random.choice(DEVICES)
    c = make_client("", d.get("proxy"), d.get("lang"), device={"device_model": dm, "system_version": sv, "app_version": av})
    await c.connect(); sent = await c.send_code_request("+" + phone); tok = secrets.token_urlsafe(8)
    pending[tok] = dict(c=c, phone=phone, hash=sent.phone_code_hash, proxy=d.get("proxy"), lang=d.get("lang"), t=time.time())
    return J({"token": tok})

async def h_qr(req: Request):
    d = await req.json(); dm, sv, av = random.choice(DEVICES)
    c = make_client("", d.get("proxy"), d.get("lang"), device={"device_model": dm, "system_version": sv, "app_version": av})
    await c.connect(); qr = await c.qr_login(); tok = secrets.token_urlsafe(8)
    pending[tok] = dict(c=c, qr=qr, proxy=d.get("proxy"), lang=d.get("lang"), t=time.time())
    return J({"token": tok, "url": qr.url})

async def h_qr_poll(token: str):
    p = pending.get(token)
    if not p: raise HTTPException(404, "вход устарел — начни заново")
    qr = p["qr"]
    try: await asyncio.wait_for(qr.wait(), timeout=2)
    except asyncio.TimeoutError:
        try:
            if qr.expires < datetime.now(timezone.utc): await qr.recreate()
        except Exception: pass
        return J({"waiting": True, "url": qr.url})
    except errors.SessionPasswordNeededError: return J({"need_password": True})
    pending.pop(token); return J({"id": await save_new(p["c"], p["proxy"], p["lang"])})

async def h_code(req: Request):
    d = await req.json(); p = pending.get(d.get("token"))
    if not p: raise HTTPException(404, "вход устарел — начни заново")
    c = p["c"]
    try:
        if d.get("code"): await c.sign_in(phone="+" + p["phone"], code=d["code"], phone_code_hash=p["hash"])
        else: await c.sign_in(password=d["password"])
    except errors.SessionPasswordNeededError:
        if not d.get("password"): return J({"need_password": True})
        await c.sign_in(password=d["password"])
    pending.pop(d["token"]); return J({"id": await save_new(c, p["proxy"], p["lang"])})

async def _import(blob, fname, f):
    proxy, lang, tmp = f.get("proxy") or None, f.get("lang") or "ru", tempfile.mkdtemp()
    aid_ = int(f["api_id"]) if str(f.get("api_id") or "").isdigit() else None
    ah_ = (f.get("api_hash") or "").strip() or None
    c = None
    try:
        if blob is None:
            if not (f.get("session") or "").strip(): raise RuntimeError("пустая строка сессии")
            c = make_client(f["session"].strip(), proxy, lang, aid_, ah_)
        elif fname.lower().endswith(".zip"):
            try:
                from opentele.td import TDesktop
                from opentele.api import UseCurrentSession
            except ImportError: raise RuntimeError("для tdata нужен пакет opentele (pip install opentele) — либо импортируй .session / строку")
            with zipfile.ZipFile(io.BytesIO(blob)) as z: z.extractall(tmp)
            td = next((p.parent for p in Path(tmp).rglob("key_data*")), None) or next(Path(tmp).rglob("tdata"))
            c = await TDesktop(str(td)).ToTelethon(session=f"{tmp}/s.session", flag=UseCurrentSession, proxy=parse_proxy(proxy))
        else:
            Path(f"{tmp}/s.session").write_bytes(blob)
            c = TelegramClient(f"{tmp}/s", aid_ or API_ID, ah_ or API_HASH, proxy=parse_proxy(proxy))
        await c.connect()
        if not await c.is_user_authorized(): raise RuntimeError("сессия не авторизована (мертва или не тот формат/api_id)")
        return J({"id": await save_new(c, proxy, lang, f.get("label"))})
    except Exception:
        if c is not None:
            try: await c.disconnect()
            except Exception: pass
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

async def h_import_str(req: Request):
    return await _import(None, "", await req.json())

async def h_import_file(req: Request):
    q = dict(req.query_params); blob = await req.body()
    if not blob: raise HTTPException(400, "пустой файл")
    if len(blob) > 40_000_000: raise HTTPException(400, "файл больше 40 МБ")
    return await _import(blob, req.headers.get("x-filename", ""), q)

async def h_page(): return FileResponse(HERE / "accounts.html")

def setup(app, check_auth):
    """check_auth(authorization_header) — функция проверки админа из main.py (бросает HTTPException)."""
    global _check_auth
    _check_auth = check_auth
    R = router; add = lambda path, fn, m: R.add_api_route(path, handler(fn), methods=[m])
    add("", h_list, "GET")
    add("/settings", h_settings_get, "GET"); add("/settings", h_settings_put, "PUT")
    add("/gemini/test", h_gem_test, "POST"); add("/bulk/start", h_bulk_start, "POST")
    add("/login/phone", h_phone, "POST"); add("/login/code", h_code, "POST")
    add("/login/qr", h_qr, "POST"); add("/login/qr/poll", h_qr_poll, "GET")
    add("/import", h_import_str, "POST"); add("/import/file", h_import_file, "POST")
    add("/{aid:int}", h_patch, "PATCH"); add("/{aid:int}", h_delete, "DELETE")
    add("/{aid:int}/refresh", h_refresh, "POST"); add("/{aid:int}/logs", h_logs, "GET")
    add("/{aid:int}/tasks", h_tasks, "GET"); add("/{aid:int}/funnel", h_funnel, "POST")
    add("/{aid:int}/channels/auto", h_channels_auto, "POST"); add("/{aid:int}/channels/check", h_channels_check, "POST")
    add("/{aid:int}/check-username", h_check_username, "POST"); add("/{aid:int}/warmup/{act}", h_warm, "POST")
    app.include_router(R)
    app.add_api_route("/accounts", h_page, methods=["GET"], include_in_schema=False)
