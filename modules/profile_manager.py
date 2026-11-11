"""Channel profile of an account: avatar files and the YouTube Studio editor (nickname, description, avatar).

The operator edits a draft in the wizard (stored in the DB); `ProfileEditor.apply` publishes it through the account's
own persistent Camoufox profile (same proxy and fingerprint as every other session of the account), touching only
what differs from the live channel, and checks the result after reloading the page.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from config import BrowserSettings, ProxyUnavailableError, TargetSettings, resolve_account_proxy
from database import AccountRecord, Database
from modules.account_menu_logic import applied_snapshot, name_changes_left, profile_pending_fields
from modules.google_authenticator import GoogleAuthenticator
from modules.interaction_simulator import InteractionConfig, InteractionSimulator
from modules.page_helpers import first_visible
from modules.youtube_runner import YouTubeRunner

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AVATARS_DIR = PROJECT_ROOT / "profiles" / "avatars"
AVATAR_MAX_BYTES = 4 * 1024 * 1024  # YouTube: channel picture up to 4 MB
_AVATAR_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
}
_AVATAR_EXTENSION_ALIASES = {".jpeg": ".jpg"}

HOME_URL = "https://www.youtube.com/?hl=en"
STUDIO_PROFILE_URL = "https://studio.youtube.com/channel/{channel_id}/editing/profile"

# Studio's DOM is undocumented: structural candidates (ids / custom elements, never button texts), the first one
# that matches wins. The post-publish check below catches a selector that silently stopped working.
NAME_INPUT = ("#name-input input", "ytcp-form-input-container#name-input input")
DESCRIPTION_INPUT = ("#description-textarea textarea", "ytcp-form-textarea#description-textarea textarea")
AVATAR_FILE_INPUT = ("ytcp-profile-image-upload input[type='file']", "input[type='file'][accept*='image']")
AVATAR_PREVIEW = ("ytcp-profile-image-upload img", "#profile-image img")
CROP_DONE_BUTTON = ("ytcp-cropping-dialog #done-button", "ytcp-cropping-dialog ytcp-button#done-button")
PUBLISH_BUTTON = ("ytcp-button#publish-button", "#publish-button")

FIELD_TIMEOUT_SECONDS = 20.0
PUBLISH_TIMEOUT_SECONDS = 30.0


class AvatarError(ValueError):
    """The picked file cannot be the channel picture; the message is shown to the operator."""


def validate_avatar(path: str | Path) -> tuple[Path, str]:
    """(file, normalized extension) of a usable channel picture; AvatarError otherwise (type, size, real content)."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise AvatarError("Файл не найден")
    extension = file.suffix.lower()
    extension = _AVATAR_EXTENSION_ALIASES.get(extension, extension)
    if extension not in _AVATAR_SIGNATURES:
        raise AvatarError("Нужен рисунок: png, jpg, gif или bmp")
    size = file.stat().st_size
    if size == 0:
        raise AvatarError("Файл пустой")
    if size > AVATAR_MAX_BYTES:
        raise AvatarError(f"Файл слишком большой: {size / 1024 / 1024:.1f} МБ из {AVATAR_MAX_BYTES // 1024 // 1024} МБ")
    with file.open("rb") as handle:
        head = handle.read(8)
    if not any(head.startswith(signature) for signature in _AVATAR_SIGNATURES[extension]):
        raise AvatarError("Содержимое файла не похоже на рисунок этого формата")
    return file, extension


def import_avatar(source: str | Path, account_name: str, avatars_dir: Path | None = None) -> str:
    """Copy the picture into `profiles/avatars/<account>-<account hash>-<hash>.<ext>`; returns the path relative to the project.

    The name carries the content hash: a different picture is a different path (so "has unapplied edits" is a plain
    comparison), and deleting the original file cannot break a later apply. The account hash keeps accounts whose
    sanitized names coincide (`a b`, `a_b`) from sharing one file.
    """
    file, extension = validate_avatar(source)
    digest = hashlib.sha256(file.read_bytes()).hexdigest()[:10]
    owner = hashlib.sha256(account_name.encode("utf-8")).hexdigest()[:6]
    safe_name = re.sub(r"[^\w.-]", "_", account_name)
    store = avatars_dir or AVATARS_DIR
    target = store / f"{safe_name}-{owner}-{digest}{extension}"
    store.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        partial = target.with_suffix(target.suffix + ".part")  # an interrupted copy never leaves a half file under the real name
        shutil.copyfile(file, partial)
        partial.replace(target)
    return target.relative_to(PROJECT_ROOT).as_posix() if target.is_relative_to(PROJECT_ROOT) else str(target)


def resolve_avatar(path: str) -> Path:
    """Absolute file of a stored avatar path (relative paths are relative to the project, not the working directory)."""
    file = Path(path)
    return file if file.is_absolute() else PROJECT_ROOT / file


def forget_avatar(path: str | None, keep: tuple[str | None, ...] = ()) -> None:
    """Delete a replaced avatar copy; only files inside `profiles/avatars` and never one still in `keep`."""
    if not path or path in keep:
        return
    file = resolve_avatar(path)
    try:
        if file.parent.resolve() == AVATARS_DIR.resolve():
            file.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Не удалось удалить старую аватарку {}: {}", path, exc)


@dataclass(frozen=True)
class ProfileResult:
    account_name: str
    success: bool
    changed_fields: tuple[str, ...]
    details: str
    skipped_fields: tuple[str, ...] = ()  # wanted but not applied (the name when YouTube's limit is used up)


def _runner_target(cfg: BrowserSettings) -> TargetSettings:
    if cfg.targets:
        return cfg.targets[0]
    return TargetSettings(
        name="youtube",
        base_url="https://www.youtube.com",
        item_url_template="https://www.youtube.com/watch?v={external_id}",
        submit_input_selector="#contenteditable-root",
        submit_button_selector="#submit-button button",
        confirmation_selector="ytd-comment-view-model",
    )


def _same_text(left: str | None, right: str | None) -> bool:
    def normalize(text: str | None) -> str:
        return (text or "").replace("\r\n", "\n").strip()

    return normalize(left) == normalize(right)


class ProfileEditor:
    """Publishes an account's profile draft to its YouTube channel through Studio."""

    def __init__(self, browser_settings: BrowserSettings, simulator: InteractionSimulator | None = None) -> None:
        self._cfg = browser_settings
        self._simulator = simulator or InteractionSimulator(InteractionConfig(key_delay_ms_min=50, key_delay_ms_max=100))

    async def apply(self, account: AccountRecord, db: Database | None = None) -> ProfileResult:
        wanted = {field: getattr(account, f"profile_{field}") for field in profile_pending_fields(account)}
        if not wanted:
            return ProfileResult(account.name, True, (), "")
        name_blocked = "name" in wanted and name_changes_left(account) == 0
        try:
            proxy = resolve_account_proxy(account)
        except ProxyUnavailableError as exc:  # fail closed: the channel is never edited from the own IP
            return ProfileResult(account.name, False, (), str(exc))

        runner = YouTubeRunner(
            self._cfg,
            _runner_target(self._cfg),
            session_dir=account.session_path,
            proxy=proxy,
            account_name=account.name,
            simulator=self._simulator,
            engine=account.engine,
            geo_country=account.geo_country or account.expected_country,
        )
        try:
            await runner.start()
            assert runner.context is not None
            pages = getattr(runner.context, "pages", [])
            page = pages[0] if pages else await runner.context.new_page()
            await self._open_profile_tab(page, runner, account)
            avatar_before = await self._avatar_src(page) if "avatar" in wanted else None
            changed, skipped = await self._fill(page, wanted, name_blocked)
            previous = applied_snapshot(account)
            if changed:
                await self._publish(page)
                if db is not None and "name" in changed:  # YouTube has it now: count it before the check below can fail
                    await db.record_account_profile_applied(account.id, previous, name_changed=True)
                await self._verify(page, wanted, changed, avatar_before)
            done = {field: value for field, value in wanted.items() if field not in skipped}
            if db is not None and done:
                await db.record_account_profile_applied(account.id, {**previous, **done}, name_changed=False)
            if "avatar" in changed:
                forget_avatar(previous.get("avatar"), keep=(wanted["avatar"], account.profile_avatar))
            note = "никнейм: лимит YouTube (2 смены за 14 дней), попробуйте позже" if skipped else ""
            logger.success("Профиль аккаунта {!r}: изменено {}", account.name, ", ".join(changed) or "ничего")
            return ProfileResult(account.name, bool(done), tuple(changed), note, tuple(skipped))
        except Exception as exc:
            logger.error("Ошибка при смене профиля аккаунта {!r}: {}", account.name, exc)
            return ProfileResult(account.name, False, (), f"{exc.__class__.__name__}: {exc}")
        finally:
            try:
                await runner.close()
            except Exception as exc:
                logger.debug("Ошибка при закрытии браузера: {}", exc)

    # ------------------------------------------------------------ Studio

    async def _open_profile_tab(self, page: Any, runner: Any, account: AccountRecord) -> None:
        """Home page first: it proves the sign-in and gives the channel id Studio's profile URL needs."""
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        await self._simulator.human_pause(2.0, 4.0)
        await runner._dismiss_consent(page)
        config = await GoogleAuthenticator(self._simulator)._read_ytcfg(page)  # waits until ytcfg is there
        if not config.get("loggedIn") or not config.get("channelId"):
            raise RuntimeError("аккаунт не вошёл или у него нет канала: сначала войдите в аккаунт")
        logger.info("Аккаунт {!r}: открываю профиль канала в YouTube Studio", account.name)
        await page.goto(STUDIO_PROFILE_URL.format(channel_id=config["channelId"]), wait_until="domcontentloaded")
        if await first_visible(page, NAME_INPUT, FIELD_TIMEOUT_SECONDS) is None:
            raise RuntimeError("вкладка «Профиль» в Studio не открылась (нет поля имени)")
        await self._simulator.human_pause(1.0, 2.0)

    async def _fill(self, page: Any, wanted: dict[str, str | None], name_blocked: bool) -> tuple[list[str], list[str]]:
        """Type the differing values and upload a new avatar; returns (fields that really changed, fields skipped).

        With `name_blocked` (YouTube's name limit is used up) the name is only compared: a name that differs is skipped.
        """
        changed: list[str] = []
        skipped: list[str] = []
        if "name" in wanted and not await self._set_text(page, NAME_INPUT, wanted["name"] or "", write=not name_blocked):
            (skipped if name_blocked else changed).append("name")
        if "description" in wanted and not await self._set_text(page, DESCRIPTION_INPUT, wanted["description"] or ""):
            changed.append("description")
        if "avatar" in wanted and wanted["avatar"]:
            await self._upload_avatar(page, wanted["avatar"])
            changed.append("avatar")
        return changed, skipped

    async def _set_text(self, page: Any, selectors: tuple[str, ...], value: str, write: bool = True) -> bool:
        """Replace the field's text by typing; True when the field already held `value` (nothing was typed).

        With `write=False` a differing field is left alone (and False is returned).
        """
        field = await first_visible(page, selectors, FIELD_TIMEOUT_SECONDS)
        if field is None:
            raise RuntimeError(f"поле не найдено: {selectors[0]}")
        if _same_text(await field.input_value(), value):
            return True
        if not write:
            return False
        await self._simulator.click_locator(page, field)
        await page.keyboard.press("Control+A", delay=random.uniform(40, 110))  # a held key, not a 0 ms keydown/keyup
        await asyncio.sleep(random.uniform(0.1, 0.25))
        await page.keyboard.press("Delete", delay=random.uniform(40, 110))
        await self._simulator.type_text(page, value, allow_typos=False)
        await self._simulator.human_pause(0.6, 1.4)
        return False

    async def _upload_avatar(self, page: Any, stored_path: str) -> None:
        file = resolve_avatar(stored_path)
        if not file.is_file():
            raise RuntimeError(f"файл аватарки пропал: {stored_path}")
        for selector in AVATAR_FILE_INPUT:
            inputs = page.locator(selector)
            if await inputs.count() > 0:
                await inputs.first.set_input_files(str(file))
                break
        else:
            raise RuntimeError("не найдено поле загрузки аватарки")
        await self._simulator.human_pause(1.5, 3.0)
        done = await first_visible(page, CROP_DONE_BUTTON, FIELD_TIMEOUT_SECONDS)
        if done is not None:  # the cropping dialog only shows up for some pictures
            await self._simulator.click_locator(page, done)
            await self._simulator.human_pause(1.0, 2.0)

    async def _publish(self, page: Any) -> None:
        button = await first_visible(page, PUBLISH_BUTTON, FIELD_TIMEOUT_SECONDS)
        if button is None:
            raise RuntimeError("кнопка публикации не найдена")
        await self._simulator.click_locator(page, button)
        await self._wait_published(page, button)
        await self._simulator.human_pause(1.5, 3.0)

    @staticmethod
    async def _wait_published(page: Any, button: Any) -> None:
        """Studio disables PUBLISH again once there is nothing left to save."""
        waited = 0.0
        while waited < PUBLISH_TIMEOUT_SECONDS:
            if await button.is_disabled():
                return
            await page.wait_for_timeout(500)
            waited += 0.5
        raise RuntimeError("Studio не подтвердил сохранение вовремя")

    async def _verify(
        self, page: Any, wanted: dict[str, str | None], changed: list[str], avatar_before: str | None
    ) -> None:
        """Reload the tab and compare: a value that did not stick means a stale selector or a refusal by YouTube."""
        await page.reload(wait_until="domcontentloaded")
        for field, selectors in (("name", NAME_INPUT), ("description", DESCRIPTION_INPUT)):
            if field not in changed:
                continue
            element = await first_visible(page, selectors, FIELD_TIMEOUT_SECONDS)
            if element is None or not _same_text(await element.input_value(), wanted[field]):
                raise RuntimeError(f"после сохранения {field} на канале не совпал с введённым")
        if "avatar" in changed:
            after = await self._avatar_src(page)
            if after is None:
                raise RuntimeError("после сохранения не удалось прочитать превью аватарки")
            if after == avatar_before:
                raise RuntimeError("после сохранения аватарка на канале не изменилась")

    @staticmethod
    async def _avatar_src(page: Any) -> str | None:
        preview = await first_visible(page, AVATAR_PREVIEW, 5.0)
        return await preview.get_attribute("src") if preview is not None else None


__all__ = [
    "AVATAR_MAX_BYTES",
    "AvatarError",
    "ProfileEditor",
    "ProfileResult",
    "forget_avatar",
    "import_avatar",
    "resolve_avatar",
    "validate_avatar",
]
