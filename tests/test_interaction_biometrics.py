"""Behavioural biometrics of InteractionSimulator: where clicks land, how the cursor gets there, what key events
typing produces. Everything runs on fake pages with a virtual clock and seeded random generators."""

from __future__ import annotations

import math
import random
import statistics
import unittest
from typing import Any
from unittest.mock import patch

from modules.interaction_simulator import InteractionConfig, InteractionError, InteractionSimulator


class _Clock:
    """Virtual time: asyncio.sleep advances it, recorded events are stamped with it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.events: list[tuple[float, str, Any]] = []

    async def sleep(self, seconds: float) -> None:
        self.now += seconds

    def record(self, kind: str, payload: Any = None) -> None:
        self.events.append((self.now, kind, payload))


class _FakeMouse:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    async def move(self, x: float, y: float) -> None:
        self._clock.record("move", (x, y))

    async def down(self) -> None:
        self._clock.record("down")

    async def up(self) -> None:
        self._clock.record("up")

    async def wheel(self, dx: float, dy: float) -> None:
        self._clock.record("wheel", (dx, dy))


class _FakeKeyboard:
    """Records key traffic; fill / insert_text exist only to prove they are never used."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.typed: list[str] = []  # what reached the field, one entry per inserted character
        self.shift_held = False
        self.unshifted_capitals: list[str] = []

    async def down(self, key: str) -> None:
        self._clock.record("keydown", key)
        if key == "Shift":
            self.shift_held = True
            return
        if (key.isupper() or key in '!@#$%^&*()_+{}|:"<>?~') and not self.shift_held:
            self.unshifted_capitals.append(key)
        self.typed.append(key)

    async def up(self, key: str) -> None:
        self._clock.record("keyup", key)
        if key == "Shift":
            self.shift_held = False

    async def press(self, key: str, delay: int | None = None) -> None:
        self._clock.record("press", key)
        if key == "Backspace":
            self.typed.pop()
        elif key == "Enter":
            self.typed.append("\n")

    async def type(self, text: str, delay: int | None = None) -> None:
        self._clock.record("type", text)
        self.typed.extend(text)

    async def insert_text(self, text: str) -> None:
        raise AssertionError("insert_text must never be used for typing")

    async def fill(self, *args: Any) -> None:
        raise AssertionError("fill must never be used for typing")


class _FakeCdpSession:
    def __init__(self, clock: _Clock, field: _FakeKeyboard) -> None:
        self._clock = clock
        self._field = field
        self.sent: list[dict[str, Any]] = []

    async def send(self, method: str, params: dict[str, Any]) -> None:
        assert method == "Input.dispatchKeyEvent"
        self.sent.append(params)
        self._clock.record("cdp-" + params["type"], params["key"])
        if params["type"] == "keyDown":
            self._field.typed.append(params["text"])


class _FakeContext:
    def __init__(self, page: _FakePage, chromium: bool) -> None:
        self._page = page
        self._chromium = chromium
        self.sessions_opened = 0
        self.attempts = 0

    async def new_cdp_session(self, page: Any) -> _FakeCdpSession:
        self.attempts += 1
        if not self._chromium:
            raise RuntimeError("CDP session is only available in Chromium")
        self.sessions_opened += 1
        return self._page.cdp


class _FakePage:
    def __init__(self, clock: _Clock, chromium: bool = False, viewport: tuple[int, int] | None = (1000, 700)) -> None:
        self.clock = clock
        self.mouse = _FakeMouse(clock)
        self.keyboard = _FakeKeyboard(clock)
        self.cdp = _FakeCdpSession(clock, self.keyboard)
        self.context = _FakeContext(self, chromium)
        self.viewport_size = {"width": viewport[0], "height": viewport[1]} if viewport else None

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, expression: str) -> list[int]:
        return [1280, 720]


class _FakeLocator:
    def __init__(self, box: dict[str, float] | None, hit: bool = True) -> None:
        self._box = box
        self._hit = hit

    async def bounding_box(self) -> dict[str, float] | None:
        return self._box

    async def scroll_into_view_if_needed(self, timeout: int | None = None) -> None:
        return None

    async def evaluate(self, script: str, arg: Any = None) -> bool:
        return self._hit

    async def click(self, **kwargs: Any) -> None:
        raise AssertionError("locator.click() would teleport the cursor and bypass the simulator")


def _simulator(seed: int = 1, **config: Any) -> InteractionSimulator:
    return InteractionSimulator(InteractionConfig(**config), rng=random.Random(seed))


def _run_with_clock(clock: _Clock):
    return patch("asyncio.sleep", new=clock.sleep)


class TestClickPoint(unittest.TestCase):
    BOX = {"x": 400.0, "y": 300.0, "width": 200.0, "height": 100.0}

    def test_points_stay_inside_twenty_to_eighty_percent(self) -> None:
        sim = _simulator()
        for _ in range(5000):
            x, y = sim._click_point(self.BOX, (1000, 700))
            self.assertTrue(400 + 0.2 * 200 <= x <= 400 + 0.8 * 200, x)
            self.assertTrue(300 + 0.2 * 100 <= y <= 300 + 0.8 * 100, y)

    def test_points_are_not_the_geometric_centre(self) -> None:
        sim = _simulator()
        points = [sim._click_point(self.BOX, (1000, 700)) for _ in range(5000)]
        centre = (500.0, 350.0)
        exact = sum(1 for p in points if abs(p[0] - centre[0]) < 0.5 and abs(p[1] - centre[1]) < 0.5)
        self.assertLess(exact / len(points), 0.01)  # a ~1x1 px bullseye is hit by ~0.3% of gaussian draws
        self.assertGreater(statistics.pstdev(p[0] for p in points), 0.08 * 200)
        self.assertGreater(statistics.pstdev(p[1] for p in points), 0.08 * 100)

    def test_distribution_is_gaussian_around_the_centre(self) -> None:
        sim = _simulator(5)
        points = [sim._click_point(self.BOX, (1000, 700)) for _ in range(20000)]
        self.assertAlmostEqual(statistics.mean(p[0] for p in points), 500.0, delta=0.01 * 200)
        self.assertAlmostEqual(statistics.mean(p[1] for p in points), 350.0, delta=0.01 * 100)
        # bell shape: the middle third of the allowed band holds well over a third of the draws
        middle = sum(1 for p in points if abs(p[0] - 500) < 0.1 * 200) / len(points)
        self.assertGreater(middle, 0.45)  # uniform over 20%-80% would give 0.33
        # but the spread is real: about 14% of the draws lie in the outer part of the band
        outer = sum(1 for p in points if abs(p[0] - 500) > 0.2 * 200) / len(points)
        self.assertGreater(outer, 0.08)

    def test_same_seed_gives_same_points(self) -> None:
        first = [_simulator(9)._click_point(self.BOX) for _ in range(3)]
        second = [_simulator(9)._click_point(self.BOX) for _ in range(3)]
        self.assertEqual(first, second)

    def test_half_hidden_element_is_clicked_in_its_visible_part(self) -> None:
        sim = _simulator()
        box = {"x": 300.0, "y": 650.0, "width": 120.0, "height": 100.0}  # only y 650..700 is on screen
        for _ in range(500):
            x, y = sim._click_point(box, (1000, 700))
            self.assertTrue(650 + 0.2 * 50 <= y <= 650 + 0.8 * 50, y)
            self.assertTrue(300 + 0.2 * 120 <= x <= 300 + 0.8 * 120, x)

    def test_element_cut_by_the_left_and_top_edges(self) -> None:
        sim = _simulator()
        box = {"x": -80.0, "y": -40.0, "width": 200.0, "height": 100.0}  # visible: x 0..120, y 0..60
        for _ in range(500):
            x, y = sim._click_point(box, (1000, 700))
            self.assertTrue(0.2 * 120 <= x <= 0.8 * 120, x)
            self.assertTrue(0.2 * 60 <= y <= 0.8 * 60, y)

    def test_element_outside_the_viewport_has_no_point(self) -> None:
        sim = _simulator()
        self.assertIsNone(sim._click_point({"x": 100.0, "y": 800.0, "width": 50.0, "height": 20.0}, (1000, 700)))
        self.assertIsNone(sim._click_point({"x": -200.0, "y": 10.0, "width": 50.0, "height": 20.0}, (1000, 700)))


class TestViewportDetection(unittest.IsolatedAsyncioTestCase):
    async def test_uses_the_emulated_viewport(self) -> None:
        page = _FakePage(_Clock(), viewport=(900, 600))
        self.assertEqual(await InteractionSimulator._viewport(page), (900.0, 600.0))

    async def test_falls_back_to_window_inner_size(self) -> None:
        page = _FakePage(_Clock(), viewport=None)
        self.assertEqual(await InteractionSimulator._viewport(page), (1280.0, 720.0))

    async def test_unreadable_viewport_is_none_not_an_error(self) -> None:
        page = _FakePage(_Clock(), viewport=None)

        async def broken(expression: str) -> None:
            raise RuntimeError("page crashed")

        page.evaluate = broken  # type: ignore[method-assign]
        self.assertIsNone(await InteractionSimulator._viewport(page))


class TestCursorPath(unittest.IsolatedAsyncioTestCase):
    async def _click(self, seed: int, box: dict[str, float] | None = None) -> tuple[_Clock, dict[str, float]]:
        clock = _Clock()
        page = _FakePage(clock)
        box = box or {"x": 600.0, "y": 420.0, "width": 140.0, "height": 44.0}
        with _run_with_clock(clock):
            await _simulator(seed).click_locator(page, _FakeLocator(box))
        return clock, box

    @staticmethod
    def _moves(clock: _Clock) -> list[tuple[float, float]]:
        return [payload for _, kind, payload in clock.events if kind == "move"]

    async def test_cursor_travels_in_many_small_steps(self) -> None:
        for seed in range(20):
            clock, _ = await self._click(seed)
            moves = self._moves(clock)
            self.assertGreaterEqual(len(moves), 8, f"seed={seed}")
            total = math.hypot(moves[-1][0] - moves[0][0], moves[-1][1] - moves[0][1])
            biggest = max(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(moves, moves[1:]))
            self.assertLess(biggest, 0.35 * total, f"seed={seed}: one step jumps most of the way")

    async def test_every_step_changes_the_pixel(self) -> None:
        for seed in range(20):
            clock, _ = await self._click(seed)
            pixels = [(round(x), round(y)) for x, y in self._moves(clock)]
            for first, second in zip(pixels, pixels[1:]):
                self.assertNotEqual(first, second, f"seed={seed}: a zero-movement event")

    async def test_path_ends_exactly_at_the_off_centre_click_point(self) -> None:
        offsets = []
        for seed in range(30):
            clock, box = await self._click(seed)
            last_move = self._moves(clock)[-1]
            self.assertTrue(box["x"] + 0.2 * box["width"] <= last_move[0] <= box["x"] + 0.8 * box["width"])
            self.assertTrue(box["y"] + 0.2 * box["height"] <= last_move[1] <= box["y"] + 0.8 * box["height"])
            centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            offsets.append(math.hypot(last_move[0] - centre[0], last_move[1] - centre[1]))
            kinds = [kind for _, kind, _ in clock.events]
            self.assertEqual(kinds[-3:], ["move", "down", "up"], "nothing but the aim pause may sit between move and down")
        self.assertGreater(min(offsets), 0.0)
        self.assertGreater(len(set(round(o, 3) for o in offsets)), 25)  # a different point every time

    async def test_pause_between_arriving_and_pressing_and_between_down_and_up(self) -> None:
        for seed in range(20):
            clock, _ = await self._click(seed)
            stamps = {kind: stamp for stamp, kind, _ in clock.events if kind in ("down", "up")}
            last_move = [stamp for stamp, kind, _ in clock.events if kind == "move"][-1]
            # the stamp of the last move is taken before its own step sleep, which also counts as hesitation
            self.assertGreater(stamps["down"] - last_move, 0.02, f"seed={seed}: press right after arriving")
            self.assertGreater(stamps["up"] - stamps["down"], 0.02, f"seed={seed}: instant mouse up")

    async def test_pointer_is_remembered_across_simulator_instances(self) -> None:
        clock = _Clock()
        page = _FakePage(clock)
        box = {"x": 600.0, "y": 420.0, "width": 140.0, "height": 44.0}
        with _run_with_clock(clock):
            await _simulator(1).click_locator(page, _FakeLocator(box))
            parked = self._moves(clock)[-1]
            count = len(self._moves(clock))
            await _simulator(2).move_mouse(page, parked[0] + 120, parked[1] + 60)  # a brand new simulator
        first_step = self._moves(clock)[count]
        self.assertLess(math.hypot(first_step[0] - parked[0], first_step[1] - parked[1]), 40, "cursor teleported")

    async def test_first_move_of_a_fresh_page_still_has_steps(self) -> None:
        clock, _ = await self._click(3)
        self.assertGreater(len(self._moves(clock)), 8)

    async def test_no_bounding_box_raises_instead_of_a_direct_locator_click(self) -> None:
        clock = _Clock()
        page = _FakePage(clock)
        with _run_with_clock(clock), self.assertRaises(InteractionError):
            await _simulator().click_locator(page, _FakeLocator(None))
        self.assertEqual([kind for _, kind, _ in clock.events if kind != "sleep"], [])

    async def test_verified_click_hits_an_off_centre_point_too(self) -> None:
        clock = _Clock()
        page = _FakePage(clock)
        box = {"x": 600.0, "y": 420.0, "width": 140.0, "height": 44.0}
        with _run_with_clock(clock):
            await _simulator(4).click_locator(page, _FakeLocator(box), verify_hit=True)
        last_move = self._moves(clock)[-1]
        self.assertNotEqual((last_move[0], last_move[1]), (670.0, 442.0))
        self.assertEqual([kind for _, kind, _ in clock.events][-2:], ["down", "up"])

    async def test_idle_wiggle_goes_through_the_stepped_path_not_a_direct_jump(self) -> None:
        clock = _Clock()
        page = _FakePage(clock)
        sim = _simulator(6)
        with _run_with_clock(clock), patch.object(sim, "move_mouse", wraps=sim.move_mouse) as stepped:
            await sim.idle(page, 0.02)
        self.assertGreater(stepped.call_count, 0)
        self.assertGreater(len(self._moves(clock)), 2 * stepped.call_count)  # several mouse.move per wiggle


class TestTypingKeyEvents(unittest.IsolatedAsyncioTestCase):
    async def _type(self, text: str, seed: int = 1, chromium: bool = False, **config: Any) -> _FakePage:
        clock = _Clock()
        page = _FakePage(clock, chromium=chromium)
        allow_typos = config.pop("allow_typos", False)
        with _run_with_clock(clock):
            await _simulator(seed, **config).type_text(page, text, allow_typos=allow_typos)
        return page

    @staticmethod
    def _kinds(page: _FakePage) -> list[tuple[str, Any]]:
        return [(kind, payload) for _, kind, payload in page.clock.events]

    async def test_ascii_text_is_typed_as_keydown_keyup_pairs_one_character_at_a_time(self) -> None:
        page = await self._type("hi there")
        events = self._kinds(page)
        self.assertNotIn("type", [kind for kind, _ in events])
        downs = [payload for kind, payload in events if kind == "keydown"]
        ups = [payload for kind, payload in events if kind == "keyup"]
        self.assertEqual("".join(downs), "hi there")
        self.assertEqual(ups, downs)
        for index in range(0, len(events), 2):  # strictly down, up, down, up: no batch insertion
            self.assertEqual(events[index][0], "keydown")
            self.assertEqual(events[index + 1], ("keyup", events[index][1]))
        self.assertEqual("".join(page.keyboard.typed), "hi there")

    async def test_capitals_and_symbols_are_typed_with_shift_held(self) -> None:
        page = await self._type("Hello, World! (ok)")
        self.assertEqual(page.keyboard.unshifted_capitals, [])
        self.assertFalse(page.keyboard.shift_held)
        events = self._kinds(page)
        position = events.index(("keydown", "H"))
        self.assertEqual(events[position - 1], ("keydown", "Shift"))  # Shift goes down first...
        self.assertEqual(events[position + 1], ("keyup", "H"))
        self.assertEqual(events[position + 2], ("keyup", "Shift"))  # ...and comes up after the letter
        self.assertEqual("".join(page.keyboard.typed), "Hello, World! (ok)")

    async def test_shift_is_released_when_the_character_key_is_rejected(self) -> None:
        clock = _Clock()
        page = _FakePage(clock)
        original = page.keyboard.down

        async def reject_capital(key: str) -> None:
            if key == "Q":
                raise RuntimeError("unknown key")
            await original(key)

        page.keyboard.down = reject_capital  # type: ignore[method-assign]
        with _run_with_clock(clock):
            await _simulator()._press_character(page, "Q")
        self.assertFalse(page.keyboard.shift_held)
        self.assertEqual([p for k, p in self._kinds(page) if k == "type"], ["Q"])  # typed once, by the fallback

    async def test_no_fill_and_no_insert_text_for_typed_comments(self) -> None:
        # the fake keyboard raises AssertionError if fill / insert_text is called
        for text in ("Thanks for the video", "Спасибо за видео, очень полезно", "a1 b2\nc3"):
            page = await self._type(text, chromium=True, typo_probability=0.3, allow_typos=True)
            self.assertEqual("".join(page.keyboard.typed), text)

    async def test_backspace_corrections_are_key_presses(self) -> None:
        text = "the quick brown fox jumps over the lazy dog again and again"
        page = await self._type(text, seed=3, typo_probability=0.4, allow_typos=True)
        backspaces = [payload for kind, payload in self._kinds(page) if kind == "press" and payload == "Backspace"]
        self.assertGreater(len(backspaces), 3)  # typos really happened, and each one was undone by a key press
        self.assertEqual("".join(page.keyboard.typed), text)

    async def test_cyrillic_without_cdp_falls_back_to_keyboard_type_once_per_letter(self) -> None:
        page = await self._type("мир", chromium=False)
        self.assertEqual([p for k, p in self._kinds(page) if k == "type"], ["м", "и", "р"])
        self.assertEqual(page.context.sessions_opened, 0)
        self.assertEqual(page.context.attempts, 1)  # Firefox: the failed attempt is made once and remembered
        self.assertEqual("".join(page.keyboard.typed), "мир")

    async def test_cyrillic_in_chromium_is_sent_as_real_russian_layout_keys(self) -> None:
        page = await self._type("Привет", chromium=True)
        self.assertEqual(page.context.sessions_opened, 1)  # one session, reused for every letter
        self.assertNotIn("type", [kind for kind, _ in self._kinds(page)])
        downs = [p for p in page.cdp.sent if p["type"] == "keyDown"]
        ups = [p for p in page.cdp.sent if p["type"] == "keyUp"]
        self.assertEqual("".join(p["key"] for p in downs), "Привет")
        self.assertEqual(len(ups), len(downs))
        self.assertEqual([p["code"] for p in downs], ["KeyG", "KeyH", "KeyB", "KeyD", "KeyT", "KeyN"])
        self.assertEqual(downs[0]["modifiers"], 8)  # capital П: Shift modifier
        self.assertEqual(downs[0]["unmodifiedText"], "п")
        self.assertTrue(all(p["modifiers"] == 0 for p in downs[1:]))
        events = self._kinds(page)
        self.assertEqual(events[0], ("keydown", "Shift"))  # Shift pressed through the keyboard for the capital
        self.assertEqual(events[1], ("cdp-keyDown", "П"))
        self.assertEqual(events[2], ("cdp-keyUp", "П"))
        self.assertEqual(events[3], ("keyup", "Shift"))
        self.assertFalse(page.keyboard.shift_held)
        self.assertEqual("".join(page.keyboard.typed[1:]), "ривет")

    async def test_letters_without_a_russian_key_use_insert_not_a_wrong_key(self) -> None:
        page = await self._type("їé😂", chromium=True)
        self.assertEqual([p for k, p in self._kinds(page) if k == "type"], ["ї", "é", "😂"])
        self.assertEqual(page.cdp.sent, [])

    async def test_cdp_failure_does_not_type_a_letter_twice(self) -> None:
        clock = _Clock()
        page = _FakePage(clock, chromium=True)

        async def failing_release(method: str, params: dict[str, Any]) -> None:
            if params["type"] == "keyDown":
                page.keyboard.typed.append(params["text"])
            else:
                raise RuntimeError("release failed")

        page.cdp.send = failing_release  # type: ignore[method-assign]
        with _run_with_clock(clock):
            await _simulator()._press_character(page, "ж")
        self.assertEqual(page.keyboard.typed, ["ж"])


class TestTypingRhythmFromEvents(unittest.IsolatedAsyncioTestCase):
    TEXT = " ".join(["thanks for sharing this really useful and clear explanation of the whole topic"] * 8)

    async def _page(self, seed: int = 11) -> _FakePage:
        clock = _Clock()
        page = _FakePage(clock)
        with _run_with_clock(clock):
            await _simulator(seed).type_text(page, self.TEXT, allow_typos=False)
        return page

    @staticmethod
    def _gaps(page: _FakePage) -> list[tuple[str, float]]:
        downs = [(stamp, payload) for stamp, kind, payload in page.clock.events if kind == "keydown"]
        return [(first[1], second[0] - first[0]) for first, second in zip(downs, downs[1:])]

    async def test_a_beat_passes_between_focus_and_the_first_key(self) -> None:
        page = await self._page()
        first = next(stamp for stamp, kind, _ in page.clock.events if kind == "keydown")
        self.assertGreaterEqual(first, 0.18 * 0.6)

    async def test_key_to_key_intervals_vary_and_have_no_constant_rhythm(self) -> None:
        letters = [gap for key, gap in self._gaps(await self._page()) if key != " "]
        self.assertGreater(len(set(round(gap, 6) for gap in letters)), 0.99 * len(letters))
        self.assertGreater(statistics.pstdev(letters) / statistics.mean(letters), 0.2)

    async def test_words_end_with_a_longer_gap_and_sometimes_a_hesitation(self) -> None:
        gaps = self._gaps(await self._page())
        after_space = [gap for key, gap in gaps if key == " "]
        after_letter = [gap for key, gap in gaps if key != " "]
        self.assertGreater(statistics.mean(after_space), 1.25 * statistics.mean(after_letter))
        self.assertTrue(any(gap > 0.45 for gap in after_space), "no hesitation between any two words")

    async def test_each_text_gets_its_own_pace(self) -> None:
        durations = []
        for seed in range(12):
            page = await self._page(seed)
            events = [stamp for stamp, kind, _ in page.clock.events if kind == "keydown"]
            durations.append(events[-1] - events[0])
        self.assertGreater(max(durations) / min(durations), 1.15)  # not the same speed every time


class TestInteractionConfig(unittest.TestCase):
    def test_new_ranges_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            InteractionConfig(click_aim_pause_ms_min=200, click_aim_pause_ms_max=100)
        with self.assertRaises(ValueError):
            InteractionConfig(first_key_delay_ms_min=900, first_key_delay_ms_max=100)


if __name__ == "__main__":
    unittest.main()
