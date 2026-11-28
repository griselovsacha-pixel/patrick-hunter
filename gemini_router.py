"""Выбор модели Gemini для центра аккаунтов — логика из рабочего модуля ИИ-ассистента (ai.py), без кэша и каталога.

  * модели ранжируются по ожидаемому времени ответа (свежие 10 мин: задержки и доля успехов), lite — первыми на холодном старте;
  * хеджинг: если модель молчит пару секунд, параллельно стартует следующая (до GEMINI_PARALLEL), побеждает первая;
  * «остывание» упавшей пары модель+ключ растёт вдвое (20→40→80… до 10 мин), 404 — модель убирается на час;
  * несколько ключей по кругу: GEMINI_API_KEYS (через запятую) и/или GEMINI_API_KEY.
ENV: GEMINI_API_KEYS, GEMINI_API_KEY, GEMINI_MODELS, GEMINI_CALL_TIMEOUT, GEMINI_DEADLINE, GEMINI_MAX_ATTEMPTS,
     GEMINI_PARALLEL, GEMINI_HEDGE_MIN_MS, GEMINI_HEDGE_MAX_MS, GEMINI_RETRY_PAUSE_MS, GEMINI_API_BASE
"""
import asyncio, collections, logging, os, random, re, time

log = logging.getLogger("accounts_center.gemini")

DEFAULT_MODELS = ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
API_BASE = os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

def _split(raw): return [x for x in re.split(r"[,\s;]+", raw or "") if x]
def _int_env(name, default, lo, hi):
    try: v = int(os.environ.get(name, default))
    except ValueError: v = default
    return max(lo, min(hi, v))

CALL_TIMEOUT = _int_env("GEMINI_CALL_TIMEOUT", 12, 3, 60)
TOTAL_DEADLINE = _int_env("GEMINI_DEADLINE", 30, 8, 110)
MAX_ATTEMPTS = _int_env("GEMINI_MAX_ATTEMPTS", 8, 1, 30)
MAX_PARALLEL = _int_env("GEMINI_PARALLEL", 2, 1, 4)
HEDGE_MIN = _int_env("GEMINI_HEDGE_MIN_MS", 2500, 500, 20000) / 1000
HEDGE_MAX = _int_env("GEMINI_HEDGE_MAX_MS", 6000, 1000, 30000) / 1000
RETRY_PAUSE = _int_env("GEMINI_RETRY_PAUSE_MS", 150, 0, 5000) / 1000
HEALTH_WINDOW = 600
COOLDOWN_MAX = 600


class CallResult:
    def __init__(self, text=None, kind="", cooldown=0.0, scope="pair"):
        self.text, self.kind, self.cooldown, self.scope = text, kind, cooldown, scope

def parse_response(status, data, retry_after=None):
    if status == 200 and isinstance(data, dict):
        if (data.get("promptFeedback") or {}).get("blockReason"): return CallResult(kind="blocked")
        cands = data.get("candidates") or []
        if cands:
            parts = ((cands[0].get("content") or {}).get("parts")) or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought")).strip()
            if text: return CallResult(text=text)
        return CallResult(kind="empty", cooldown=5)
    if status == 429: return CallResult(kind="rate", cooldown=min(max(retry_after or 60, 10), 300))
    if status in (500, 502, 503, 504, 408): return CallResult(kind="overload", cooldown=20)
    if status == 404: return CallResult(kind="no-model", cooldown=3600, scope="model")
    if status in (400, 401, 403): return CallResult(kind=f"http-{status}", cooldown=600)
    return CallResult(kind=f"http-{status}", cooldown=30)


class GeminiRouter:
    def __init__(self):
        keys = _split(os.environ.get("GEMINI_API_KEYS", "") + " " + os.environ.get("GEMINI_API_KEY", ""))
        self.keys = list(dict.fromkeys(keys))
        self.models = _split(os.environ.get("GEMINI_MODELS", "")) or list(DEFAULT_MODELS)
        self._cool, self._strikes, self._hist, self._rr, self._session = {}, {}, {}, 0, None
        self.last_error = ""

    def configured(self): return bool(self.keys and self.models)

    def _cooling(self, model, ki):
        now = time.monotonic()
        return self._cool.get(("pair", model, ki), 0) > now or self._cool.get(("model", model), 0) > now

    @staticmethod
    def _prior(model, idx): return (2.0 if "lite" in model.lower() else 4.0) + 0.05 * idx
    def _recent(self, m):
        now = time.monotonic(); return [x for x in self._hist.get(m, ()) if now - x[0] < HEALTH_WINDOW]
    def _typical(self, m, idx=0):
        oks = [x[2] for x in self._recent(m) if x[1]]
        return (sum(oks) + self._prior(m, idx)) / (len(oks) + 1)
    def _expected(self, m, idx):
        h = self._recent(m); p = (sum(1 for x in h if x[1]) + 0.8) / (len(h) + 1)
        return self._typical(m, idx) / max(p, 0.05)
    def _record(self, m, ok, secs):
        self._hist.setdefault(m, collections.deque(maxlen=12)).append((time.monotonic(), ok, secs))

    def ranking(self, models):
        return sorted((round(self._expected(m, i), 2), m) for i, m in enumerate(models))

    def candidates(self, models):
        n = len(self.keys)
        if not n: return []
        self._rr = (self._rr + 1) % n
        order = [(self._expected(m, mi), i, mi, m, (self._rr + i) % n) for mi, m in enumerate(models) for i in range(n)]
        order.sort(key=lambda t: t[:3])
        pairs = [(m, ki) for _, _, _, m, ki in order]
        return ([c for c in pairs if not self._cooling(*c)] + [c for c in pairs if self._cooling(*c)])[:MAX_ATTEMPTS]

    def _penalize(self, model, ki, res):
        if res.cooldown > 0:
            if res.scope == "model": self._cool[("model", model)] = time.monotonic() + res.cooldown
            else:
                n = self._strikes.get((model, ki), 0) + 1; self._strikes[(model, ki)] = n
                self._cool[("pair", model, ki)] = time.monotonic() + min(res.cooldown * 2 ** (n - 1), COOLDOWN_MAX)

    @staticmethod
    def _pause(fails):
        if RETRY_PAUSE <= 0: return 0.0
        return min(RETRY_PAUSE * (2 ** max(fails - 1, 0)), 1.5) * random.uniform(0.7, 1.3)

    async def aclose(self):
        if self._session is not None and not self._session.closed: await self._session.close()

    async def _call(self, model, key, payload, timeout):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=16, ttl_dns_cache=300))
        try:
            async with self._session.post(f"{API_BASE}/models/{model}:generateContent", json=payload,
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                try: data = await r.json(content_type=None)
                except Exception: data = None
                try: ra = float(r.headers.get("Retry-After", ""))
                except ValueError: ra = None
                return parse_response(r.status, data, ra)
        except asyncio.TimeoutError: return CallResult(kind="timeout", cooldown=15)
        except aiohttp.ClientError: return CallResult(kind="network", cooldown=10)

    async def _guarded(self, model, key, payload, timeout):
        try: return await self._call(model, key, payload, timeout)
        except Exception as e:
            log.warning("gemini call crashed on %s: %s", model, type(e).__name__)
            return CallResult(kind="crash", cooldown=15)

    async def _race(self, cands, payload, deadline, models):
        pending, queue, next_launch, fails = {}, list(cands), 0.0, 0
        last = ""
        try:
            while True:
                now = time.monotonic(); left = deadline - now
                if left <= 0: break
                can = bool(queue) and len(pending) < MAX_PARALLEL and left >= 2
                if can and now >= next_launch:
                    model, ki = queue.pop(0)
                    t = asyncio.create_task(self._guarded(model, self.keys[ki], payload, min(CALL_TIMEOUT, left)))
                    pending[t] = (model, ki, now)
                    idx = models.index(model) if model in models else 0
                    next_launch = now + max(HEDGE_MIN, min(HEDGE_MAX, self._typical(model, idx) * 1.6))
                    continue
                if not pending and not can: break
                tmo = min(max(next_launch - now, 0.0) if can else left, left)
                if not pending: await asyncio.sleep(tmo); continue
                done, _ = await asyncio.wait(set(pending), timeout=tmo, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    model, ki, t0 = pending.pop(t); res, dt = t.result(), time.monotonic() - t0
                    if res.text:
                        self._record(model, True, dt); self._strikes.pop((model, ki), None); return res.text, model
                    if res.kind != "blocked": self._record(model, False, dt)
                    last = f"{model}: {res.kind}"
                    log.warning("gemini: %s (ключ #%d) %s за %.1f с", model, ki + 1, res.kind, dt)
                    self._penalize(model, ki, res); fails += 1
                    next_launch = min(next_launch, time.monotonic() + self._pause(fails))
            self.last_error = last
            return None, None
        finally:
            for t in pending: t.cancel()
            if pending: await asyncio.gather(*pending, return_exceptions=True)

    async def generate(self, system, user_text, temperature=0.9, max_tokens=1024, prefer=None):
        """Возвращает (текст, модель). prefer — модель из настроек: попадает в список, дальше ранжирование по скорости."""
        if not self.configured(): raise RuntimeError("не заданы GEMINI_API_KEY / GEMINI_API_KEYS")
        models = list(dict.fromkeys(([prefer] if prefer else []) + self.models))
        payload = {"systemInstruction": {"parts": [{"text": system}]},
                   "contents": [{"role": "user", "parts": [{"text": user_text}]}],
                   "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
        saved, self.models = self.models, models          # candidates()/_race работают по актуальному списку
        try:
            text, model = await self._race(self.candidates(models), payload, time.monotonic() + TOTAL_DEADLINE, models)
        finally:
            self.models = saved
        if text is None: raise RuntimeError(f"Gemini не ответил ({self.last_error or 'все модели недоступны'})")
        return text, model

router = GeminiRouter()
