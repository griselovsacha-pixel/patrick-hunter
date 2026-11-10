"""
TaskRunner — цикл выполнения заданий бота (подписки, запуск подботов,
мини-аппы). Логика перенесена почти 1:1 из старой версии, только разбита
по методам и переведена на core.wait_fresh / реальный баланс из профиля.
"""
import re
import asyncio
import logging

from config import ALL_DONE_PHRASES, CONFIRM_WORDS
from utils import is_webapp_button, human_delay, random_task_interval
from antiban import safe_call
from core import utcnow

logger = logging.getLogger(__name__)


class TaskRunner:
    def __init__(self, client, core, tg_logger, profile_stats, label: str, db=None):
        self.client = client
        self.core = core
        self.tg_logger = tg_logger
        self.profile_stats = profile_stats
        self.label = label
        self.db = db
        self.start_offset = 0  # выставляется в account.py (индекс аккаунта * TASK_START_STAGGER_SEC)

    async def process_one_task(self, task_msg) -> tuple:
        text = task_msg.text or ""

        if any(phrase in text.lower() for phrase in ALL_DONE_PHRASES):
            return 'all_done', text

        has_webapp = bool(task_msg.buttons) and any(
            is_webapp_button(btn) for row in task_msg.buttons for btn in row
        )

        if has_webapp:
            return await self._handle_webapp_task(task_msg, text)

        return await self._handle_regular_task(task_msg, text)

    async def _handle_webapp_task(self, task_msg, text):
        startapp_url = None
        for row in task_msg.buttons:
            for btn in row:
                url = getattr(btn, 'url', None)
                if url and 'startapp' in url.lower():
                    startapp_url = url
                    break
            if startapp_url:
                break

        # Кнопка web_view прямо в сообщении (не ссылка t.me) — открываем через WebView
        webview_btn = None
        if not startapp_url:
            for row in task_msg.buttons:
                for btn in row:
                    raw = getattr(btn, 'button', None)
                    if type(raw).__name__ in ('KeyboardButtonWebView', 'KeyboardButtonSimpleWebView'):
                        webview_btn = btn
                        break
                if webview_btn:
                    break

        opened = False
        if startapp_url:
            opened = await self.core.handle_startapp(startapp_url)
        elif webview_btn:
            opened = await self.core.handle_webview_button(task_msg, webview_btn)

        if startapp_url or webview_btn:
            if opened:
                since = utcnow()
                fresh_msg = await self.core.wait_fresh(since, timeout=8)
                if fresh_msg and "проверка" in (fresh_msg.text or "").lower():
                    if await self.core.captcha.solve_if_needed(fresh_msg):
                        since = utcnow()
                        fresh_msg = await self.core.wait_fresh(since, timeout=6)
                confirmed = await self._click_confirm_if_present(fresh_msg)
                confirm_note = " и подтверждено" if confirmed else ""
                await self.tg_logger.log(f"🚀 Приложение запущено{confirm_note}!\n📄 {text[:80]}")
                if self.db:
                    await self.db.event("mini_app", stars=0.0, task_type="mini_app")
                return 'done', f'startapp ({(startapp_url or "webview")[:50]})'

        for row in task_msg.buttons:
            for btn in row:
                if "пропустить" in btn.text.lower():
                    try:
                        await btn.click()
                    except Exception as e:
                        logger.warning(f"⚠️ [{self.label}] Клик Пропустить: {e}")
                    await human_delay(2, 3)
                    return 'skip', 'мини-апп (пропущен)'

        await self.tg_logger.log(f"⚠️ Мини-апп без кнопки Пропустить\n📄 {text[:150]}", error="ручное вмешательство")
        return 'all_done', text

    async def _click_confirm_if_present(self, msg) -> bool:
        if not msg or not msg.buttons:
            return False
        for row in msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in CONFIRM_WORDS):
                    try:
                        await btn.click()
                        await human_delay(2, 3)
                        return True
                    except Exception as e:
                        logger.warning(f"⚠️ [{self.label}] Клик подтверждения: {e}")
                        return False
        return False

    async def _handle_regular_task(self, task_msg, text):
        action_done = False
        action_parts = []
        same_msg_confirm_btn = None

        if task_msg.buttons:
            for row in task_msg.buttons:
                for btn in row:
                    url = getattr(btn, 'url', None)
                    btn_text = btn.text.lower()

                    if any(w in btn_text for w in CONFIRM_WORDS):
                        same_msg_confirm_btn = btn
                        continue
                    if any(w in btn_text for w in ["пропустить", "меню", "назад", "главное"]):
                        continue

                    if url:
                        tme_match = re.search(r't\.me/(\+?[a-zA-Z0-9_]+)', url)
                        try:
                            if tme_match:
                                target = tme_match.group(1)
                                if target.lower().endswith('bot'):
                                    await safe_call(lambda t=target: self.client.send_message(t, '/start'), action="task_start_bot")
                                    await self.core.archive_chat(target)
                                    action_parts.append(f"запустил бота @{target}")
                                else:
                                    await self.core.safe_join(target)
                                    action_parts.append(f"подписался на {target}")
                            else:
                                action_parts.append("перешёл по ссылке")
                            action_done = True
                        except Exception as e:
                            logger.warning(f"⚠️ [{self.label}] Ошибка действия по кнопке: {e}")

        if not action_done:
            for target in re.findall(r't\.me/(\+?[a-zA-Z0-9_]+)', text):
                if target.lower().endswith('bot'):
                    await safe_call(lambda t=target: self.client.send_message(t, '/start'), action="task_start_bot")
                    await self.core.archive_chat(target)
                    action_parts.append(f"запустил бота @{target}")
                else:
                    await self.core.safe_join(target)
                    action_parts.append(f"подписался на {target}")
                action_done = True

            for username in re.findall(r'@([a-zA-Z0-9_]{5,})', text):
                if username.lower().endswith('bot'):
                    await safe_call(lambda u=username: self.client.send_message(u, '/start'), action="task_start_bot")
                    await self.core.archive_chat(username)
                    action_parts.append(f"запустил бота @{username}")
                else:
                    await self.core.safe_join(username)
                    action_parts.append(f"подписался на @{username}")
                action_done = True

        if not action_done and same_msg_confirm_btn:
            action_done = True
            action_parts.append("ознакомился с заданием")

        action_desc = ", ".join(action_parts) if action_parts else "выполнил задание"

        if not action_done:
            await self.tg_logger.log(f"⚠️ Не смог определить тип задания\n📄 {text[:150]}", error="неизвестный тип")
            if task_msg.buttons:
                for row in task_msg.buttons:
                    for btn in row:
                        if "пропустить" in btn.text.lower():
                            try:
                                await btn.click()
                            except Exception:
                                pass
                            await human_delay(2, 3)
                            break
            return 'skip', text[:100]

        await self.core.yield_sleep(6, 12)  # отдаём лок: промокод не ждёт задание

        confirmed = False
        if same_msg_confirm_btn:
            try:
                since = utcnow()
                await same_msg_confirm_btn.click()
                confirmed = True
                await human_delay(1, 2)
            except Exception as e:
                logger.warning(f"⚠️ [{self.label}] Не удалось нажать подтверждение: {e}")
                since = utcnow()
        else:
            since = utcnow()

        fresh_msg = await self.core.wait_fresh(since, timeout=8)
        if fresh_msg and "проверка" in (fresh_msg.text or "").lower():
            if await self.core.captcha.solve_if_needed(fresh_msg):
                since = utcnow()
                fresh_msg = await self.core.wait_fresh(since, timeout=6)

        if not confirmed and fresh_msg:
            confirmed = await self._click_confirm_if_present(fresh_msg)

        suffix = "" if confirmed else " (без подтверждения)"
        return 'done', action_desc + suffix

    # --- Основной цикл заданий ---
    async def run_forever(self):
        await asyncio.sleep(30 + self.start_offset)  # даём аккаунту "отлежаться"; сдвиг разводит аккаунты по времени
        while True:
            try:
                await self.tg_logger.log("📋 Начинаю выполнение заданий...")

                async with self.core.bot_lock.acquire():
                    msg = await self.core._open_unlocked()
                    if not msg or not msg.buttons:
                        await self.tg_logger.log("Бот не ответил на /start", error="нет ответа")
                        await asyncio.sleep(10800)
                        continue

                    task_opened = False
                    since = utcnow()
                    for row in msg.buttons:
                        for btn in row:
                            if "задани" in btn.text.lower():
                                await btn.click()
                                task_opened = True
                                break
                        if task_opened:
                            break

                    if not task_opened:
                        await self.tg_logger.log("Кнопка заданий не найдена", error="нет кнопки")
                        await asyncio.sleep(10800)
                        continue

                    first_task_msg = await self.core.wait_fresh(since, timeout=8)

                completed = 0
                skipped = 0
                last_task_msg_id = None
                cycle_broken = False

                for i in range(30):
                    try:
                        async with self.core.bot_lock.acquire():
                            if i == 0 and first_task_msg is not None:
                                task_msg = first_task_msg
                            else:
                                task_msg = await self.core.poll_response()
                            if not task_msg:
                                await self.tg_logger.log("Не получил сообщение от бота", error="нет ответа")
                                cycle_broken = True
                                break

                            if last_task_msg_id and task_msg.id == last_task_msg_id:
                                skipped_by_btn = False
                                if task_msg.buttons:
                                    for row in task_msg.buttons:
                                        for btn in row:
                                            if "пропустить" in btn.text.lower():
                                                await btn.click()
                                                skipped_by_btn = True
                                                await human_delay(3, 5)
                                                break
                                        if skipped_by_btn:
                                            break
                                if skipped_by_btn:
                                    skipped += 1
                                    continue
                                await self.tg_logger.log("Задание зациклилось без кнопки Пропустить", error="цикл")
                                cycle_broken = True
                                break

                            last_task_msg_id = task_msg.id
                            result, desc = await self.process_one_task(task_msg)
                    except Exception as e:
                        logger.error(f"[{self.label}] Ошибка на задании #{i+1}: {e}")
                        self.profile_stats.add_error(f"tasks#{i+1}: {str(e)[:40]}")
                        await human_delay(3, 6)
                        continue

                    if result == 'all_done':
                        interval = random_task_interval()
                        h, m = interval // 3600, (interval % 3600) // 60
                        self.profile_stats.tasks_completed_today += completed
                        self.profile_stats.tasks_skipped_today += skipped
                        await self._refresh_balance()
                        await self.tg_logger.log(
                            f"🎉 Все задания выполнены! ✅{completed} ⏭️{skipped}\n⏳ Следующая проверка через {h}ч {m}м"
                        )
                        await asyncio.sleep(interval)
                        cycle_broken = True
                        break
                    elif result == 'done':
                        completed += 1
                        await self.tg_logger.log(f"✅ Задание #{completed} выполнено!\n📌 {desc}")
                        if self.db:
                            await self.db.event("task", stars=0.4, task_type="task")
                        await human_delay(4, 9)
                    elif result == 'skip':
                        skipped += 1
                        await human_delay(2, 4)

                if not cycle_broken:
                    interval = random_task_interval()
                    h, m = interval // 3600, (interval % 3600) // 60
                    self.profile_stats.tasks_completed_today += completed
                    await self._refresh_balance()
                    await self.tg_logger.log(f"⚠️ Лимит заданий (30). ✅{completed} ⏭️{skipped}. Через {h}ч {m}м")
                    await asyncio.sleep(interval)

            except Exception as e:
                logger.error(f"[{self.label}] Ошибка заданий: {e}")
                self.profile_stats.add_error(f"tasks: {str(e)[:50]}")
                await self.tg_logger.log("Ошибка цикла заданий", error=str(e))
                await asyncio.sleep(600)

    async def _refresh_balance(self):
        """После полного цикла заданий заходим в Профиль и берём реальный баланс."""
        msg = await self.core.open_bot()
        if not msg or not msg.buttons:
            return
        for row in msg.buttons:
            for btn in row:
                if any(w in btn.text.lower() for w in ["профил", "profile"]):
                    since = utcnow()
                    await btn.click()
                    resp = await self.core.wait_fresh(since, timeout=6)
                    if resp:
                        await self.profile_stats.update_from_profile_text(resp.text or "")
                    return
