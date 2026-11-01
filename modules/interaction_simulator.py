from __future__ import annotations

import asyncio
import inspect
import math
import random
import re
import unicodedata
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

_NEIGHBORS: dict[str, str] = {
    "q": "wa",
    "w": "qeas",
    "e": "wrsd",
    "r": "etdf",
    "t": "ryfg",
    "y": "tugh",
    "u": "yihj",
    "i": "uojk",
    "o": "ipkl",
    "p": "ol",
    "a": "qwsz",
    "s": "awedxz",
    "d": "serfcx",
    "f": "drtgvc",
    "g": "ftyhbv",
    "h": "gyujnb",
    "j": "huikmn",
    "k": "jiolm",
    "l": "kop",
    "z": "asx",
    "x": "zsdc",
    "c": "xdfv",
    "v": "cfgb",
    "b": "vghn",
    "n": "bhjm",
    "m": "njk",
    # Russian ЙЦУКЕН keyboard adjacency mapping
    "й": "цуф",
    "ц": "йуыф",
    "у": "цкыв",
    "к": "уева",
    "е": "кнпа",
    "н": "егрп",
    "г": "ншро",
    "ш": "гщло",
    "щ": "шздл",
    "з": "щхжд",
    "х": "зъэж",
    "ъ": "хэ",
    "ф": "йцыя",
    "ы": "фувчя",
    "в": "ыукасч",
    "а": "вкепмс",
    "п": "аенрми",
    "р": "пнготи",
    "о": "ргшльт",
    "л": "ошщдьб",
    "д": "лщзжюб",
    "ж": "дзхэю",
    "э": "жхъ",
    "я": "фыч",
    "ч": "яывс",
    "с": "чвам",
    "м": "сапи",
    "и": "мпрот",
    "т": "ироль",
    "ь": "толб",
    "б": "ьлдю",
    "ю": "бдж",
    "ё": "й",
}


_SHIFTED = frozenset('!@#$%^&*()_+{}|:"<>?~')
_WORD_PAUSE_PROBABILITY = 0.05  # chance of a short hesitation between two words
_WORD_PAUSE_MS = (250.0, 700.0)
_PROTECTED_SPAN = re.compile(r"@[\w.\-]+|(?:https?://|www\.)\S+")  # mentions and links never get typos
_LATE_NOTICE_CHARS = (1, 2)  # a transposition is noticed after this many more characters

_HIT_TEST_JS = """(el, p) => {
    const top = document.elementFromPoint(p.x, p.y);
    return !!top && (top === el || el.contains(top));
}"""


class InteractionError(RuntimeError):
    """Raised when a UI interaction cannot be performed."""


class InteractionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    typo_probability: float = Field(default=0.04, ge=0, le=0.5)  # per letter; the share below is a subset of it
    transposition_share: float = Field(default=0.35, ge=0, le=1)  # of those typos: swapped neighbours ("teh")
    key_delay_ms_min: int = Field(default=45, ge=5)
    key_delay_ms_max: int = Field(default=130, ge=5)
    key_hold_ms_min: int = Field(default=60, ge=10)
    key_hold_ms_max: int = Field(default=100, ge=10)
    thinking_pause_min: float = Field(default=1.0, ge=0)
    thinking_pause_max: float = Field(default=2.5, ge=0)
    backspace_delay_ms_min: int = Field(default=60, ge=10)
    backspace_delay_ms_max: int = Field(default=180, ge=10)
    mouse_step_delay_ms_min: int = Field(default=4, ge=0)
    mouse_step_delay_ms_max: int = Field(default=9, ge=0)
    click_hold_ms_min: int = Field(default=40, ge=10)
    click_hold_ms_max: int = Field(default=120, ge=10)
    scroll_step_min: int = Field(default=120, ge=20)
    scroll_step_max: int = Field(default=420, ge=20)
    scroll_pause_min: float = Field(default=0.25, ge=0)
    scroll_pause_max: float = Field(default=0.9, ge=0)

    @model_validator(mode="after")
    def _validate_ranges(self) -> "InteractionConfig":
        pairs = (
            ("key_delay_ms_min", "key_delay_ms_max"),
            ("key_hold_ms_min", "key_hold_ms_max"),
            ("thinking_pause_min", "thinking_pause_max"),
            ("backspace_delay_ms_min", "backspace_delay_ms_max"),
            ("mouse_step_delay_ms_min", "mouse_step_delay_ms_max"),
            ("click_hold_ms_min", "click_hold_ms_max"),
            ("scroll_step_min", "scroll_step_max"),
            ("scroll_pause_min", "scroll_pause_max"),
        )
        for minimum, maximum in pairs:
            if getattr(self, minimum) > getattr(self, maximum):
                raise ValueError(f"{minimum} must not exceed {maximum}")
        return self


class InteractionSimulator:
    """Human-like mouse, wheel and keyboard primitives for Playwright pages."""

    def __init__(self, config: InteractionConfig | None = None, rng: random.Random | None = None) -> None:
        self._cfg = config or InteractionConfig()
        self._rng = rng or random.Random()
        self._positions: dict[int, tuple[float, float]] = {}

    @staticmethod
    def _ensure_open(page: Any) -> None:
        if page.is_closed():
            raise InteractionError("cannot interact with a closed page")

    def _delay(self, minimum_ms: float, maximum_ms: float) -> float:
        return self._rng.uniform(minimum_ms, maximum_ms) / 1000

    def _lognormal_ms(self, minimum_ms: float, maximum_ms: float) -> float:
        """Right-skewed keystroke timing: [min, max] holds ~90% of the draws, a rare long tail runs past max."""
        if minimum_ms <= 0 or maximum_ms <= minimum_ms:
            return float(minimum_ms)
        sigma = math.log(maximum_ms / minimum_ms) / (2 * 1.645)
        value = self._rng.lognormvariate(math.log(math.sqrt(minimum_ms * maximum_ms)), sigma)
        return min(max(value, minimum_ms * 0.6), maximum_ms * 2.5)

    async def human_pause(self, minimum_seconds: float, maximum_seconds: float) -> None:
        low, high = sorted((max(0.0, minimum_seconds), max(0.0, maximum_seconds)))
        await asyncio.sleep(self._rng.uniform(low, high))

    async def thinking_pause(self, minimum_seconds: float | None = None, maximum_seconds: float | None = None) -> None:
        """Simulate cognitive hesitation / reading pause before beginning input."""
        low = self._cfg.thinking_pause_min if minimum_seconds is None else minimum_seconds
        high = self._cfg.thinking_pause_max if maximum_seconds is None else maximum_seconds
        await self.human_pause(low, high)

    def _bezier_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        steps: int,
    ) -> list[tuple[float, float]]:
        dx, dy = end[0] - start[0], end[1] - start[1]
        distance = math.hypot(dx, dy)
        if distance < 1e-6:
            return [end]
        normal_x, normal_y = -dy / distance, dx / distance
        bend_one = self._rng.uniform(-0.25, 0.25) * distance
        bend_two = self._rng.uniform(-0.25, 0.25) * distance
        control_one = (start[0] + dx / 3 + normal_x * bend_one, start[1] + dy / 3 + normal_y * bend_one)
        control_two = (start[0] + 2 * dx / 3 + normal_x * bend_two, start[1] + 2 * dy / 3 + normal_y * bend_two)

        points: list[tuple[float, float]] = []
        for index in range(1, steps + 1):
            t = index / steps
            t = t * t * (3 - 2 * t)
            inverse = 1 - t
            x = (
                inverse**3 * start[0]
                + 3 * inverse**2 * t * control_one[0]
                + 3 * inverse * t**2 * control_two[0]
                + t**3 * end[0]
            )
            y = (
                inverse**3 * start[1]
                + 3 * inverse**2 * t * control_one[1]
                + 3 * inverse * t**2 * control_two[1]
                + t**3 * end[1]
            )
            points.append((x + self._rng.uniform(-0.8, 0.8), y + self._rng.uniform(-0.8, 0.8)))
        points[-1] = end
        return points

    async def move_mouse(self, page: Any, x: float, y: float) -> None:
        self._ensure_open(page)
        start = self._positions.get(id(page), (self._rng.uniform(40, 420), self._rng.uniform(40, 320)))
        end = (float(x), float(y))
        distance = math.hypot(end[0] - start[0], end[1] - start[1])
        steps = max(10, min(50, int(distance / 12) + 8))
        for point in self._bezier_path(start, end, steps):
            await page.mouse.move(point[0], point[1])
            await asyncio.sleep(
                self._delay(self._cfg.mouse_step_delay_ms_min, self._cfg.mouse_step_delay_ms_max)
            )
        self._positions[id(page)] = end

    async def _press_mouse(self, page: Any) -> None:
        await page.mouse.down()
        await asyncio.sleep(self._delay(self._cfg.click_hold_ms_min, self._cfg.click_hold_ms_max))
        await page.mouse.up()

    async def click(self, page: Any, x: float, y: float) -> None:
        self._ensure_open(page)
        await self.move_mouse(page, x, y)
        await asyncio.sleep(self._delay(40, 120))
        await self._press_mouse(page)

    async def _click_verified(self, page: Any, locator: Any, attempts: int = 3) -> None:
        """Click only while the element is really under the cursor: layout shifts during the mouse path would
        otherwise land the click on a neighbour (e.g. 'Cancel' next to 'Reply')."""
        for _ in range(attempts):
            box = await locator.bounding_box()
            if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
                break
            x = box["x"] + box["width"] * self._rng.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * self._rng.uniform(0.3, 0.7)
            await self.move_mouse(page, x, y)
            await asyncio.sleep(self._delay(40, 120))
            if await locator.evaluate(_HIT_TEST_JS, {"x": x, "y": y}):
                await self._press_mouse(page)
                return
            logger.debug("Под курсором другой элемент (раскладка сдвинулась), повторное наведение")
        raise InteractionError("click target is not under the cursor (layout kept shifting)")

    async def click_locator(
        self, page: Any, locator: Any, timeout_ms: int | None = None, verify_hit: bool = False
    ) -> None:
        self._ensure_open(page)
        try:
            await locator.scroll_into_view_if_needed(timeout=timeout_ms)
        except Exception as exc:
            raise InteractionError(f"element is not scrollable into view: {exc}") from exc
        if verify_hit:
            await self._click_verified(page, locator)
            return
        box = await locator.bounding_box()
        if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
            # Fallback for background / minimized execution where layout coordinates may be unavailable
            try:
                await locator.click(timeout=timeout_ms)
                return
            except Exception as exc:
                raise InteractionError(f"element has no clickable bounding box and direct click failed: {exc}") from exc
        x = box["x"] + box["width"] * self._rng.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * self._rng.uniform(0.3, 0.7)
        await self.click(page, x, y)

    async def scroll(self, page: Any, total_pixels: int) -> None:
        self._ensure_open(page)
        if total_pixels == 0:
            return
        direction = 1 if total_pixels > 0 else -1
        remaining = abs(total_pixels)
        while remaining > 0:
            step = min(remaining, self._rng.randint(self._cfg.scroll_step_min, self._cfg.scroll_step_max))
            try:
                await page.mouse.wheel(0, step * direction)
            except Exception:
                pass
            # Also dispatch window.scrollBy so that background/minimized windows scroll reliably
            try:
                await page.evaluate(f"window.scrollBy(0, {step * direction})")
            except Exception:
                pass
            remaining -= step
            await asyncio.sleep(self._rng.uniform(self._cfg.scroll_pause_min, self._cfg.scroll_pause_max))
            if self._rng.random() < 0.15:
                rev = -direction * self._rng.randint(10, 60)
                try:
                    await page.mouse.wheel(0, rev)
                except Exception:
                    pass
                try:
                    await page.evaluate(f"window.scrollBy(0, {rev})")
                except Exception:
                    pass
                await asyncio.sleep(self._rng.uniform(0.1, 0.35))

    async def _press_character(self, page: Any, character: str) -> None:
        hold_time = self._lognormal_ms(self._cfg.key_hold_ms_min, self._cfg.key_hold_ms_max) / 1000
        down_fn = getattr(page.keyboard, "down", None)
        up_fn = getattr(page.keyboard, "up", None)
        typed = False
        # Playwright keyboard.down/up natively accepts US ASCII characters and named keys.
        # Non-ASCII (Cyrillic, emojis) should directly use keyboard.type to avoid IPC Unknown key errors.
        if ord(character) <= 127 and inspect.iscoroutinefunction(down_fn) and inspect.iscoroutinefunction(up_fn):
            try:
                await down_fn(character)
            except Exception:
                pass
            else:
                typed = True  # the key went down: a failed release must not type the character a second time
                await asyncio.sleep(hold_time)
                try:
                    await up_fn(character)
                except Exception:
                    pass
        if not typed:
            type_fn = getattr(page.keyboard, "type", None)
            if callable(type_fn):
                res_type = type_fn(character)
                if asyncio.iscoroutine(res_type):
                    await res_type
            await asyncio.sleep(hold_time)

    async def type_text(self, page: Any, text: str, allow_typos: bool = True) -> None:
        self._ensure_open(page)
        if not text:
            return
        protected = [match.span() for match in _PROTECTED_SPAN.finditer(text)]
        skip_until = 0
        for index, character in enumerate(text):
            if index < skip_until:  # already typed by a transposition correction
                continue
            if character == "\n":
                press_fn = getattr(page.keyboard, "press", None)
                hold_ms = int(self._lognormal_ms(self._cfg.key_hold_ms_min, self._cfg.key_hold_ms_max))
                if callable(press_fn):
                    try:
                        res = press_fn("Enter", delay=hold_ms)
                        if asyncio.iscoroutine(res):
                            await res
                    except TypeError:
                        res = press_fn("Enter")
                        if asyncio.iscoroutine(res):
                            await res
                await asyncio.sleep(self._delay(self._cfg.key_delay_ms_min, self._cfg.key_delay_ms_max) * 1.8)
                continue
            if (
                allow_typos is True
                and character.isalpha()
                and not self._in_spans(index, protected)
                and not self._marks_adjacent(text, index, index + 1)
                and self._rng.random() < self._cfg.typo_probability
            ):
                if self._can_transpose(text, index, protected) and self._rng.random() < self._cfg.transposition_share:
                    lag = self._late_notice_chars(text, index, protected)
                    await self._type_with_transposition(page, text, index, lag)
                    skip_until = index + 2 + lag
                    continue
                await self._type_with_typo(page, character)
            else:
                await self._press_character(page, character)
            await asyncio.sleep(self._character_delay(character, text[index + 1 : index + 2]))

    async def _type_with_typo(self, page: Any, character: str) -> None:
        neighbors = _NEIGHBORS.get(character.lower())
        if not neighbors:
            await self._press_character(page, character)
            return
        wrong = self._rng.choice(neighbors)
        wrong = wrong.upper() if character.isupper() else wrong
        logger.trace("Injecting typo {!r} for {!r}", wrong, character)
        await self._press_character(page, wrong)
        await asyncio.sleep(
            self._delay(self._cfg.backspace_delay_ms_min, self._cfg.backspace_delay_ms_max)
        )
        await self._press_backspace(page)
        await asyncio.sleep(
            self._delay(self._cfg.backspace_delay_ms_min, self._cfg.backspace_delay_ms_max)
        )
        await self._press_character(page, character)

    async def _press_backspace(self, page: Any) -> None:
        press_fn = getattr(page.keyboard, "press", None)
        if not callable(press_fn):  # a skipped Backspace would leave the typo in the posted text
            raise InteractionError("keyboard.press is unavailable: cannot correct a typo")
        hold_ms = int(self._lognormal_ms(self._cfg.key_hold_ms_min, self._cfg.key_hold_ms_max))
        try:
            res = press_fn("Backspace", delay=hold_ms)
            if asyncio.iscoroutine(res):
                await res
        except TypeError:
            res = press_fn("Backspace")
            if asyncio.iscoroutine(res):
                await res

    @staticmethod
    def _in_spans(index: int, spans: list[tuple[int, int]]) -> bool:
        return any(start <= index < end for start, end in spans)

    @staticmethod
    def _marks_adjacent(text: str, first: int, last: int) -> bool:
        """True when a combining mark sits right before `first` or right after `last` (or inside): no wrong keystroke there,
        whatever a Backspace removes (one code point or a whole cluster)."""
        return any(
            0 <= i < len(text) and unicodedata.category(text[i]).startswith("M")
            for i in range(first - 1, last + 2)
        )

    @classmethod
    def _can_transpose(cls, text: str, index: int, spans: list[tuple[int, int]] | None = None) -> bool:
        """True when the letters at `index` and `index + 1` are two different lowercase letters outside mentions/links."""
        pair = text[index : index + 2]
        if len(pair) < 2 or not all(c.isalpha() and not c.isupper() for c in pair) or pair[0] == pair[1]:
            return False
        if cls._marks_adjacent(text, index, index + 1):
            return False
        return not any(cls._in_spans(i, spans or []) for i in (index, index + 1))

    def _late_notice_chars(self, text: str, index: int, spans: list[tuple[int, int]]) -> int:
        """How many characters after the swapped pair get typed before the slip is noticed (fewer at a line or link end).

        Only characters typed with one keystroke and removed by one Backspace qualify: no newline, no emoji or
        code points above U+2000, nothing inside a mention or link, and no character that is, or sits next to,
        a combining mark.
        """
        wanted = self._rng.choice(_LATE_NOTICE_CHARS)
        lag = 0
        while lag < wanted:
            position = index + 2 + lag
            if position >= len(text) or text[position] == "\n" or ord(text[position]) >= 0x2000:
                break
            if self._marks_adjacent(text, position, position):
                break
            if self._in_spans(position, spans):
                break
            lag += 1
        return lag

    async def _type_with_transposition(self, page: Any, text: str, index: int, lag: int) -> None:
        """Type `text[index + 1]` before `text[index]` plus `lag` more characters, notice, delete them all, retype."""
        end = index + 2 + lag
        wrong = text[index + 1] + text[index] + text[index + 2 : end]
        logger.trace("Injecting transposition {!r} for {!r}", wrong, text[index:end])
        for position, character in enumerate(wrong):
            await self._press_character(page, character)
            await asyncio.sleep(self._character_delay(character, wrong[position + 1 : position + 2]))
        await asyncio.sleep(self._delay(self._cfg.backspace_delay_ms_min, self._cfg.backspace_delay_ms_max) * 2)
        for _ in wrong:  # exactly one Backspace per typed character
            await self._press_backspace(page)
            await asyncio.sleep(self._lognormal_ms(self._cfg.key_delay_ms_min, self._cfg.key_delay_ms_max) / 1000)
        await asyncio.sleep(self._delay(self._cfg.backspace_delay_ms_min, self._cfg.backspace_delay_ms_max))
        for position in range(index, end):
            await self._press_character(page, text[position])
            await asyncio.sleep(self._character_delay(text[position], text[position + 1 : position + 2]))

    def _character_delay(self, character: str, following: str = "") -> float:
        """Seconds to wait after `character`: lognormal base, longer at word and sentence breaks and on Shift keys.

        Punctuation pauses only when it ends a word ("end. Next"), not inside "name@mail.com" or "a.b".
        """
        delay = self._lognormal_ms(self._cfg.key_delay_ms_min, self._cfg.key_delay_ms_max)
        at_word_end = following in ("", " ", "\n")
        if character == " ":
            delay *= self._rng.uniform(1.3, 2.0)
            if self._rng.random() < _WORD_PAUSE_PROBABILITY:
                delay += self._rng.uniform(*_WORD_PAUSE_MS)
        elif character in ".!?" and at_word_end:
            delay *= self._rng.uniform(1.8, 3.0)
        elif character in ",;:" and at_word_end:
            delay *= self._rng.uniform(1.4, 2.2)
        elif character.isupper() or character in _SHIFTED:
            delay *= self._rng.uniform(1.1, 1.5)
        return delay / 1000

    async def idle(self, page: Any, max_seconds: float) -> None:
        self._ensure_open(page)
        deadline = asyncio.get_running_loop().time() + max(0.0, max_seconds)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, self._rng.uniform(0.2, 0.7)))
            current = self._positions.get(id(page), (self._rng.uniform(80, 400), self._rng.uniform(80, 300)))
            target = (
                min(1200.0, max(0.0, current[0] + self._rng.uniform(-40, 40))),
                min(800.0, max(0.0, current[1] + self._rng.uniform(-25, 25))),
            )
            await page.mouse.move(target[0], target[1])
            self._positions[id(page)] = target


__all__ = ["InteractionConfig", "InteractionError", "InteractionSimulator"]
