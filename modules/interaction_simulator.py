from __future__ import annotations

import asyncio
import inspect
import math
import random
import re
import unicodedata
import weakref
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

# Click point inside an element: gaussian around the centre, truncated to this share of the width / height.
_CLICK_SPREAD = 0.15  # sigma as a share of the size: +-0.3 (the truncation edge) is two sigmas
_CLICK_BOUNDS = (0.2, 0.8)
_SHIFT_LEAD_MS = (20.0, 70.0)  # Shift goes down this long before the character key
_SHIFT_TRAIL_MS = (15.0, 60.0)  # and is released this long after it
_TEMPO_RANGE = (0.8, 1.45)  # per-text typing speed multiplier (>1 = slower): every comment gets its own pace

# Physical keys of the Russian (ЙЦУКЕН) layout: lowercase letter -> (KeyboardEvent.code, Windows virtual-key code).
# Playwright's keyboard.down() only knows the US layout, so a Cyrillic letter typed through it would be an
# insertText without keydown/keyup; with these codes the letter is sent as a real key of the Russian layout.
_CYRILLIC_KEYS: dict[str, tuple[str, int]] = {
    "й": ("KeyQ", 81), "ц": ("KeyW", 87), "у": ("KeyE", 69), "к": ("KeyR", 82), "е": ("KeyT", 84),
    "н": ("KeyY", 89), "г": ("KeyU", 85), "ш": ("KeyI", 73), "щ": ("KeyO", 79), "з": ("KeyP", 80),
    "х": ("BracketLeft", 219), "ъ": ("BracketRight", 221),
    "ф": ("KeyA", 65), "ы": ("KeyS", 83), "в": ("KeyD", 68), "а": ("KeyF", 70), "п": ("KeyG", 71),
    "р": ("KeyH", 72), "о": ("KeyJ", 74), "л": ("KeyK", 75), "д": ("KeyL", 76),
    "ж": ("Semicolon", 186), "э": ("Quote", 222),
    "я": ("KeyZ", 90), "ч": ("KeyX", 88), "с": ("KeyC", 67), "м": ("KeyV", 86), "и": ("KeyB", 66),
    "т": ("KeyN", 78), "ь": ("KeyM", 77), "б": ("Comma", 188), "ю": ("Period", 190),
    "ё": ("Backquote", 192),
}
_SHIFT_MODIFIER = 8  # Input.dispatchKeyEvent `modifiers` bit for Shift

# The last known pointer position per page, shared by every simulator instance: several of them work on one
# page (runner, authenticator, warmer), and a fresh instance must continue from where the cursor really is.
_POINTERS: weakref.WeakKeyDictionary[Any, tuple[float, float]] = weakref.WeakKeyDictionary()
_POINTERS_BY_ID: dict[int, tuple[float, float]] = {}  # pages that cannot be weakly referenced
_CDP_SESSIONS: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()  # page -> CDP session or None

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
    click_aim_pause_ms_min: int = Field(default=40, ge=0)  # cursor has arrived -> button goes down
    click_aim_pause_ms_max: int = Field(default=120, ge=0)
    first_key_delay_ms_min: int = Field(default=180, ge=0)  # field is focused -> first key goes down
    first_key_delay_ms_max: int = Field(default=520, ge=0)
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
            ("click_aim_pause_ms_min", "click_aim_pause_ms_max"),
            ("first_key_delay_ms_min", "first_key_delay_ms_max"),
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

    @staticmethod
    def _pointer(page: Any) -> tuple[float, float] | None:
        """Where the cursor last was on `page` (None before the first simulated move)."""
        try:
            return _POINTERS.get(page)
        except TypeError:
            return _POINTERS_BY_ID.get(id(page))

    @staticmethod
    def _remember_pointer(page: Any, position: tuple[float, float]) -> None:
        try:
            _POINTERS[page] = position
        except TypeError:
            _POINTERS_BY_ID[id(page)] = position

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

    @staticmethod
    def _dedupe_pixels(
        points: list[tuple[float, float]], previous: tuple[float, float]
    ) -> list[tuple[float, float]]:
        """Drop path points that land on the same screen pixel as the one before (they would be events with
        movementX = movementY = 0); the last point is always kept exactly, it is the click target."""
        kept: list[tuple[float, float]] = []
        last_pixel = (round(previous[0]), round(previous[1]))
        for point in points[:-1]:
            pixel = (round(point[0]), round(point[1]))
            if pixel != last_pixel:
                kept.append(point)
                last_pixel = pixel
        if kept and (round(kept[-1][0]), round(kept[-1][1])) == (round(points[-1][0]), round(points[-1][1])):
            kept.pop()  # the target pixel is reached by the exact last point instead
        kept.append(points[-1])
        return kept

    async def move_mouse(self, page: Any, x: float, y: float) -> None:
        """Move the cursor to (x, y) along a curved path of several `mouse.move` steps ending exactly there."""
        self._ensure_open(page)
        known = self._pointer(page)
        start = known or (self._rng.uniform(40, 420), self._rng.uniform(40, 320))
        end = (float(x), float(y))
        distance = math.hypot(end[0] - start[0], end[1] - start[1])
        if known is not None and distance < 0.5:
            return  # already there: a zero-length move would be a movementX = movementY = 0 event
        steps = max(10, min(50, int(distance / 12) + 8))
        for point in self._dedupe_pixels(self._bezier_path(start, end, steps), start):
            await page.mouse.move(point[0], point[1])
            self._remember_pointer(page, point)
            await asyncio.sleep(
                self._delay(self._cfg.mouse_step_delay_ms_min, self._cfg.mouse_step_delay_ms_max)
            )

    async def _aim_pause(self) -> None:
        """The short beat between the cursor arriving and the button going down."""
        await asyncio.sleep(
            self._lognormal_ms(self._cfg.click_aim_pause_ms_min, self._cfg.click_aim_pause_ms_max) / 1000
        )

    async def _press_mouse(self, page: Any) -> None:
        await page.mouse.down()
        await asyncio.sleep(self._lognormal_ms(self._cfg.click_hold_ms_min, self._cfg.click_hold_ms_max) / 1000)
        await page.mouse.up()

    async def click(self, page: Any, x: float, y: float) -> None:
        """Click exactly at (x, y); choosing an off-centre point is the caller's job (see click_locator)."""
        self._ensure_open(page)
        await self.move_mouse(page, x, y)
        await self._aim_pause()
        await self._press_mouse(page)

    def _truncated_gauss(self, low: float, high: float, mean: float = 0.5, sigma: float = _CLICK_SPREAD) -> float:
        for _ in range(12):
            value = self._rng.gauss(mean, sigma)
            if low <= value <= high:
                return value
        return min(max(mean, low), high)

    def _click_point(
        self, box: dict[str, float], viewport: tuple[float, float] | None = None
    ) -> tuple[float, float] | None:
        """A human click point: gaussian around the centre, kept inside 20%-80% of the width and the height.

        Only the part of the element that is inside the viewport counts, so a half-hidden element is clicked in its
        visible half. None when nothing of it is visible.
        """
        left, top = float(box["x"]), float(box["y"])
        right, bottom = left + float(box["width"]), top + float(box["height"])
        if viewport is not None:
            left, top = max(left, 0.0), max(top, 0.0)
            right, bottom = min(right, viewport[0]), min(bottom, viewport[1])
        if right - left <= 0 or bottom - top <= 0:
            return None
        low, high = _CLICK_BOUNDS
        return (
            left + (right - left) * self._truncated_gauss(low, high),
            top + (bottom - top) * self._truncated_gauss(low, high),
        )

    @staticmethod
    async def _viewport(page: Any) -> tuple[float, float] | None:
        """Visible area size in CSS pixels (None when it cannot be read)."""
        size = getattr(page, "viewport_size", None)
        if isinstance(size, dict) and size.get("width") and size.get("height"):
            return float(size["width"]), float(size["height"])
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            return None
        try:
            result = evaluate("[window.innerWidth, window.innerHeight]")
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            return None
        if (
            isinstance(result, (list, tuple))
            and len(result) == 2
            and all(isinstance(value, (int, float)) and value > 0 for value in result)
        ):
            return float(result[0]), float(result[1])
        return None

    async def _aim_at(self, page: Any, locator: Any) -> tuple[float, float] | None:
        """Pick the click point inside the element's visible box (None when it has no usable box)."""
        box = await locator.bounding_box()
        if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
            return None
        return self._click_point(box, await self._viewport(page))

    async def _click_verified(self, page: Any, locator: Any, attempts: int = 3) -> None:
        """Click only while the element is really under the cursor: layout shifts during the mouse path would
        otherwise land the click on a neighbour (e.g. 'Cancel' next to 'Reply')."""
        for _ in range(attempts):
            point = await self._aim_at(page, locator)
            if point is None:
                break
            x, y = point
            await self.move_mouse(page, x, y)
            await self._aim_pause()
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
        point = await self._aim_at(page, locator)
        for _ in range(2):  # the layout may still be settling: look again before giving up
            if point is not None:
                break
            await asyncio.sleep(self._delay(150, 350))
            point = await self._aim_at(page, locator)
        if point is None:
            # No locator.click() here: it would teleport the cursor and bypass the simulated mouse path.
            raise InteractionError("element has no clickable bounding box")
        await self.click(page, point[0], point[1])

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

    @staticmethod
    def _needs_shift(character: str) -> bool:
        return character.isupper() or character in _SHIFTED

    async def _release_shift(self, up_fn: Any) -> None:
        await asyncio.sleep(self._lognormal_ms(*_SHIFT_TRAIL_MS) / 1000)
        try:
            await up_fn("Shift")
        except Exception:
            pass

    async def _press_ascii(self, page: Any, character: str, hold_time: float) -> bool:
        """keydown -> keypress -> beforeinput -> input -> keyup for one US-layout character.

        Playwright's keyboard.down("A") would report shiftKey=false for a capital letter or "!", so the Shift key is
        held around those characters like on a real keyboard. False when no key event was sent (caller falls back).
        """
        down_fn = getattr(page.keyboard, "down", None)
        up_fn = getattr(page.keyboard, "up", None)
        if not (inspect.iscoroutinefunction(down_fn) and inspect.iscoroutinefunction(up_fn)):
            return False
        shift_held = False
        if self._needs_shift(character):
            try:
                await down_fn("Shift")
            except Exception:
                pass
            else:
                shift_held = True
                await asyncio.sleep(self._lognormal_ms(*_SHIFT_LEAD_MS) / 1000)
        try:
            try:
                await down_fn(character)
            except Exception:
                return False  # unknown key for Playwright: nothing was typed
            # the key went down: a failed release must not type the character a second time
            await asyncio.sleep(hold_time)
            try:
                await up_fn(character)
            except Exception:
                pass
            return True
        finally:
            if shift_held:
                await self._release_shift(up_fn)

    @staticmethod
    async def _cdp_session(page: Any) -> Any | None:
        """A raw CDP session of a Chromium page (cached; None for Firefox or when it cannot be opened)."""
        try:
            if page in _CDP_SESSIONS:
                return _CDP_SESSIONS[page]
        except TypeError:
            pass
        session: Any | None = None
        factory = getattr(getattr(page, "context", None), "new_cdp_session", None)
        if callable(factory):
            try:
                candidate = factory(page)
                if inspect.isawaitable(candidate):
                    session = await candidate
            except Exception as exc:
                logger.debug("CDP-сессия недоступна, кириллица пойдёт через insertText: {}", exc)
                session = None
        if session is not None and not inspect.iscoroutinefunction(getattr(session, "send", None)):
            session = None
        try:
            _CDP_SESSIONS[page] = session
        except TypeError:
            pass
        return session

    async def _press_cyrillic(self, page: Any, character: str, hold_time: float) -> bool:
        """A Russian-layout letter as a real key (key=letter, code=physical key) instead of a bare insertText.

        Playwright's keyboard.type("й") is only beforeinput + input, with no keydown / keyup at all. Chromium lets us
        send the key through Input.dispatchKeyEvent with the ЙЦУКЕН physical code, which yields the full
        keydown, keypress, beforeinput, input, keyup chain. False when that is impossible (Firefox, other letters).
        """
        spec = _CYRILLIC_KEYS.get(character.lower()) if len(character) == 1 else None
        if spec is None or not (character.islower() or character.isupper()):
            return False
        session = await self._cdp_session(page)
        if session is None:
            return False
        code, virtual_key = spec
        shifted = character.isupper()
        shift_up = getattr(page.keyboard, "up", None)
        shift_held = False
        if shifted:
            shift_down = getattr(page.keyboard, "down", None)
            if not (inspect.iscoroutinefunction(shift_down) and inspect.iscoroutinefunction(shift_up)):
                return False
            try:
                await shift_down("Shift")
            except Exception:
                return False
            shift_held = True
            await asyncio.sleep(self._lognormal_ms(*_SHIFT_LEAD_MS) / 1000)
        event = {
            "key": character,
            "code": code,
            "windowsVirtualKeyCode": virtual_key,
            "modifiers": _SHIFT_MODIFIER if shifted else 0,
        }
        try:
            try:
                await session.send(
                    "Input.dispatchKeyEvent",
                    {"type": "keyDown", **event, "text": character, "unmodifiedText": character.lower()},
                )
            except Exception as exc:
                logger.debug("Не удалось отправить клавишу {!r} через CDP: {}", character, exc)
                return False
            await asyncio.sleep(hold_time)
            try:
                await session.send("Input.dispatchKeyEvent", {"type": "keyUp", **event})
            except Exception:
                pass
            return True
        finally:
            if shift_held:
                await self._release_shift(shift_up)

    async def _press_character(self, page: Any, character: str) -> None:
        """Type one character as real key events when a key exists for it.

        US-layout ASCII goes through keyboard.down/up, Russian letters through a CDP key event of the ЙЦУКЕН layout.
        Everything else (emoji, typographic quotes, other alphabets) has no key in either: it is inserted with
        keyboard.type, i.e. beforeinput + input only, like text entered through an emoji panel or an IME.
        """
        hold_time = self._lognormal_ms(self._cfg.key_hold_ms_min, self._cfg.key_hold_ms_max) / 1000
        if ord(character) <= 127:
            typed = await self._press_ascii(page, character, hold_time)
        else:
            typed = await self._press_cyrillic(page, character, hold_time)
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
        # the hand has to find the keys after the click: a beat before the first keydown
        await asyncio.sleep(
            self._lognormal_ms(self._cfg.first_key_delay_ms_min, self._cfg.first_key_delay_ms_max) / 1000
        )
        tempo = self._rng.uniform(*_TEMPO_RANGE)  # this text's overall pace; drifts a little from key to key
        protected = [match.span() for match in _PROTECTED_SPAN.finditer(text)]
        skip_until = 0
        for index, character in enumerate(text):
            if index < skip_until:  # already typed by a transposition correction
                continue
            tempo = min(max(tempo * self._rng.uniform(0.95, 1.05), 0.75), 1.6)
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
                await asyncio.sleep(
                    self._delay(self._cfg.key_delay_ms_min, self._cfg.key_delay_ms_max) * 1.8 * tempo
                )
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
                    await self._type_with_transposition(page, text, index, lag, tempo)
                    skip_until = index + 2 + lag
                    continue
                await self._type_with_typo(page, character)
            else:
                await self._press_character(page, character)
            await asyncio.sleep(self._character_delay(character, text[index + 1 : index + 2]) * tempo)

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

    async def _type_with_transposition(
        self, page: Any, text: str, index: int, lag: int, tempo: float = 1.0
    ) -> None:
        """Type `text[index + 1]` before `text[index]` plus `lag` more characters, notice, delete them all, retype."""
        end = index + 2 + lag
        wrong = text[index + 1] + text[index] + text[index + 2 : end]
        logger.trace("Injecting transposition {!r} for {!r}", wrong, text[index:end])
        for position, character in enumerate(wrong):
            await self._press_character(page, character)
            await asyncio.sleep(self._character_delay(character, wrong[position + 1 : position + 2]) * tempo)
        await asyncio.sleep(self._delay(self._cfg.backspace_delay_ms_min, self._cfg.backspace_delay_ms_max) * 2)
        for _ in wrong:  # exactly one Backspace per typed character
            await self._press_backspace(page)
            await asyncio.sleep(self._lognormal_ms(self._cfg.key_delay_ms_min, self._cfg.key_delay_ms_max) / 1000)
        await asyncio.sleep(self._delay(self._cfg.backspace_delay_ms_min, self._cfg.backspace_delay_ms_max))
        for position in range(index, end):
            await self._press_character(page, text[position])
            await asyncio.sleep(self._character_delay(text[position], text[position + 1 : position + 2]) * tempo)

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
            current = self._pointer(page) or (self._rng.uniform(80, 400), self._rng.uniform(80, 300))
            target = (
                min(1200.0, max(0.0, current[0] + self._rng.uniform(-40, 40))),
                min(800.0, max(0.0, current[1] + self._rng.uniform(-25, 25))),
            )
            await self.move_mouse(page, target[0], target[1])  # a short multi-step path, not a one-event jump


__all__ = ["InteractionConfig", "InteractionError", "InteractionSimulator"]
