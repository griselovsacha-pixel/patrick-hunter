"""
Данные для карточек аккаунтов в админке.

Подключается одной строкой в main.py (см. инструкцию), ничего в остальном коде не меняет:

  GET /api/admin/accounts-info          — ник, ID, реф-ссылка, время запуска, FloodWait, выведено…
  GET /api/admin/account-avatar?account=N — аватарка аккаунта из Telegram (кэшируется на 6 часов)
"""
import asyncio
import logging
import time

from fastapi import Header, HTTPException
from fastapi.responses import Response

logger = logging.getLogger(__name__)

AVATAR_TTL_SEC = 6 * 3600
_avatar_cache: dict = {}   # номер аккаунта -> (время, bytes | None)


def _number(acc) -> int:
    return int("".join(ch for ch in acc.label if ch.isdigit()) or "1")


def _has_photo(me) -> bool:
    photo = getattr(me, "photo", None)
    return bool(photo) and type(photo).__name__ != "UserProfilePhotoEmpty"


def register(app, get_accounts, check_admin_auth, supabase_get):
    """get_accounts — функция, возвращающая актуальный список аккаунтов (в main.py он пересоздаётся)."""

    def find(number: int):
        return next((a for a in get_accounts() if _number(a) == number), None)

    @app.get("/api/admin/accounts-info")
    async def accounts_info(authorization: str | None = Header(default=None)):
        check_admin_auth(authorization)

        # Сколько звёзд выведено с каждого аккаунта (успешные выводы из таблицы withdrawals)
        withdrawn: dict = {}
        try:
            rows = await supabase_get("withdrawals", {
                "select": "account_number,stars,status", "status": "eq.success", "limit": "5000"})
            for r in rows:
                n = int(r.get("account_number") or 0)
                w = withdrawn.setdefault(n, {"stars": 0.0, "count": 0})
                w["stars"] += float(r.get("stars") or 0)
                w["count"] += 1
        except Exception as e:
            logger.debug("accounts-info: withdrawals недоступны: %r", e)

        now = time.time()
        out = []
        for acc in get_accounts():
            n = _number(acc)
            me = getattr(acc, "me", None)
            ps = getattr(acc, "profile_stats", None)
            prot = getattr(acc, "protection", None)

            last_flood = None
            floods_1h = floods_24h = 0
            state = state_title = None
            if prot is not None:
                try:
                    floods_1h = prot.recent_floods(3600)
                    floods_24h = prot.recent_floods(86400)
                    state = prot.state
                    snap = prot.snapshot()
                    state_title = snap.get("state_title")
                    lf = prot.last_flood
                    if lf and float(lf.get("ts") or 0) > 1e9:
                        last_flood = {"sec": lf.get("sec"), "action": lf.get("action"),
                                      "ago_sec": int(now - float(lf["ts"]))}
                except Exception as e:
                    logger.debug("accounts-info protection: %r", e)

            bio = getattr(acc, "bio_manager", None)
            started = getattr(acc, "started_at", None)
            w = withdrawn.get(n, {"stars": 0.0, "count": 0})
            out.append({
                "account": n,
                "label": acc.label,
                "ready": bool(acc.ready),
                "dead": acc.dead,
                "tg_id": getattr(me, "id", None),
                "username": getattr(me, "username", None),
                "first_name": getattr(me, "first_name", None),
                "last_name": getattr(me, "last_name", None),
                "has_avatar": bool(acc.ready and _has_photo(me)),
                "referral_link": (bio.referral_link if bio else None) or None,
                "started_at": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
                               if started else None),
                "uptime_sec": int(now - started) if started else None,
                "balance": round(float(getattr(ps, "balance", 0) or 0), 2),
                "friends": getattr(ps, "friends", 0),
                "activated": getattr(ps, "activated", 0),
                "floods_1h": floods_1h,
                "floods_24h": floods_24h,
                "last_flood": last_flood,
                "state": state,
                "state_title": state_title,
                "withdrawn_stars": round(w["stars"], 2) if withdrawn or w["count"] else None,
                "withdrawn_count": w["count"] if withdrawn or w["count"] else None,
            })
        return {"accounts": out}

    @app.get("/api/admin/account-avatar")
    async def account_avatar(account: int, authorization: str | None = Header(default=None)):
        check_admin_auth(authorization)
        acc = find(account)
        if not acc or not acc.ready:
            raise HTTPException(status_code=404, detail="Аккаунт не найден или ещё запускается")

        hit = _avatar_cache.get(account)
        if hit and time.time() - hit[0] < AVATAR_TTL_SEC:
            data = hit[1]
        else:
            try:
                data = await asyncio.wait_for(acc.client.download_profile_photo("me", file=bytes), 20)
            except Exception as e:
                logger.debug("avatar %s: %r", account, e)
                data = None
            _avatar_cache[account] = (time.time(), data)

        if not data:
            raise HTTPException(status_code=404, detail="Нет аватарки")
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})
