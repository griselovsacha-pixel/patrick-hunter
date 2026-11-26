"""
CaptchaSolver — вся логика распознавания и решения капчи бота
(математический пример или "нажми на нужный фрукт/овощ").
"""
import re
import logging

from config import FRUIT_MAP
from utils import solve_math, human_delay

logger = logging.getLogger(__name__)


class CaptchaSolver:
    def __init__(self, client, tg_logger, label: str):
        self.client = client
        self.tg_logger = tg_logger
        self.label = label
        self.core = None   # BotCore — ставится из account.py: клики идут через защиту аккаунта

    async def _delay(self, lo, hi):
        if self.core is not None:
            await self.core.delay(lo, hi)
        else:
            await human_delay(lo, hi)

    async def _click(self, btn):
        if self.core is not None:
            return await self.core.click_raw(btn)
        return await btn.click()

    async def solve_if_needed(self, msg) -> bool:
        """Возвращает True, если капчи не было ИЛИ она успешно решена."""
        if not msg:
            return False
        text = msg.text or ""
        if "проверка на робота" not in text.lower():
            return True

        logger.info(f"🤖 [{self.label}] Капча: {text[:100]}")
        if not msg.buttons:
            await self.tg_logger.log("🤖 Капча без кнопок", error="нет кнопок")
            return False

        all_btns = [b.text for row in msg.buttons for b in row]

        # Математика
        res = solve_math(text)
        if res is not None:
            for row in msg.buttons:
                for btn in row:
                    if btn.text.strip() == str(res):
                        try:
                            await self._click(btn)
                        except Exception as e:
                            logger.warning(f"⚠️ [{self.label}] Клик капчи не удался: {e}")
                            return False
                        await self._delay(2, 3)
                        await self.tg_logger.log(f"🤖 Капча (математика) решена: {res}")
                        return True
            await self.tg_logger.log(f"🤖 Капча: ответ {res} не найден среди кнопок", error=str(all_btns))
            return False

        # Фрукты/овощи
        clean_text = re.sub(r"\*+", "", text).lower()
        word_match = re.search(r"[«\"'“”‘’](.+?)[»\"'“”‘’]", clean_text)
        if word_match:
            target_word = word_match.group(1).lower().strip()
            target_emoji = self._find_emoji_for(target_word)

            for row in msg.buttons:
                for btn in row:
                    btn_text = btn.text.strip()
                    if (target_emoji and target_emoji in btn_text) or target_word in btn_text.lower():
                        try:
                            await self._click(btn)
                        except Exception as e:
                            logger.warning(f"⚠️ [{self.label}] Клик капчи не удался: {e}")
                            return False
                        await self._delay(2, 3)
                        await self.tg_logger.log(f"🤖 Капча (фрукт) решена: '{target_word}' → {btn_text}")
                        return True

            await self.tg_logger.log(f"🤖 Капча: не нашёл '{target_word}' среди кнопок", error=str(all_btns))
            return False

        # Фолбэк: слово не в кавычках — ищем известное слово прямо в тексте капчи
        target_emoji, matched_kw = self._find_emoji_in_text(clean_text)
        if target_emoji:
            for row in msg.buttons:
                for btn in row:
                    if target_emoji in btn.text.strip():
                        try:
                            await self._click(btn)
                        except Exception as e:
                            logger.warning(f"⚠️ [{self.label}] Клик капчи не удался: {e}")
                            return False
                        await self._delay(2, 3)
                        await self.tg_logger.log(f"🤖 Капча (фрукт, фолбэк) решена: '{matched_kw}'")
                        return True

        await self.tg_logger.log("🤖 Капча неизвестного типа", error=f"{text[:150]} | {all_btns}")
        return False

    @staticmethod
    def _find_emoji_for(word: str):
        for emoji, keywords in FRUIT_MAP.items():
            for kw in keywords:
                if kw in word or word in kw:
                    return emoji
        return None

    @staticmethod
    def _find_emoji_in_text(text: str):
        for emoji, keywords in FRUIT_MAP.items():
            for kw in keywords:
                if kw in text:
                    return emoji, kw
        return None, None
