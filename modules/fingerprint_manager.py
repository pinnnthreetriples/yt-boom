from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from loguru import logger

FINGERPRINT_FILE = "fingerprint.json"
FORMAT_VERSION = 1
HOST_GPU_FILE = "_host_gpu.json"  # next to the account profiles; delete it to re-detect after a GPU or driver change

# Firefox hands pages a sanitized GPU "bucket" through plain getParameter(RENDERER) that Camoufox does not spoof,
# only UNMASKED_RENDERER. A pinned GPU that differs from the host bucket makes the two disagree, so we read the bucket.
_HOST_RENDERER_PROBE = """
import json
from camoufox.sync_api import Camoufox

with Camoufox(headless=True, os="windows") as browser:
    page = browser.new_page()
    print(json.dumps(page.evaluate("() => { const g = document.createElement('canvas').getContext('webgl'); return g.getParameter(g.RENDERER); }")))
"""

_OS_MARKERS: tuple[tuple[str, str], ...] = (
    ("Windows", "windows"),
    ("Macintosh", "macos"),
    ("Mac OS X", "macos"),
    ("Linux", "linux"),
    ("X11", "linux"),
)


def _os_of(fingerprint: dict[str, Any]) -> str | None:
    """OS a fingerprint claims (from its user agent); None when it cannot be told."""
    user_agent = str((fingerprint.get("navigator") or {}).get("userAgent") or "")
    for marker, name in _OS_MARKERS:
        if marker in user_agent:
            return name
    return None


def _read_saved(path: Path) -> tuple[dict[str, Any], str | None] | None:
    """(fingerprint, os) from the file: the versioned wrapper or the older flat dict; None when missing or damaged."""
    if not path.is_file():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Не удалось прочитать отпечаток {}: {}. Генерирую новый", path, exc)
        return None
    if isinstance(saved, dict) and isinstance(saved.get("fingerprint"), dict) and "format" in saved:
        fingerprint, stored_os = saved["fingerprint"], saved.get("os")
        stored_os = str(stored_os).lower() if stored_os else None
    else:
        fingerprint, stored_os = saved, None
    if not isinstance(fingerprint, dict) or not fingerprint:
        logger.warning("Файл отпечатка {} пуст или повреждён, генерирую новый", path)
        return None
    return fingerprint, stored_os or _os_of(fingerprint)


def _write_atomic(path: Path, fingerprint: dict[str, Any], target_os: str) -> None:
    payload = {"format": FORMAT_VERSION, "os": target_os.lower(), "fingerprint": fingerprint}
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # a crash mid-write never leaves a half-written file under the real name


def _probe_host_renderer() -> str | None:
    """The plain WebGL RENDERER string of this machine, read in a throwaway headless Camoufox (own process: no event loop clash)."""
    try:
        done = subprocess.run([sys.executable, "-c", _HOST_RENDERER_PROBE], capture_output=True, text=True, timeout=120)
        renderer = json.loads(done.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        logger.warning("Не удалось определить GPU хоста: {}", exc)
        return None
    return renderer if isinstance(renderer, str) else None


def host_gpu(cache_dir: Path) -> dict[str, str] | None:
    """The GPU (vendor, renderer) Firefox shows on this host, for fpgen's `gpu` condition; None when it cannot be told.

    Detected once and kept in `cache_dir/_host_gpu.json`.
    """
    path = Path(cache_dir) / HOST_GPU_FILE
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("vendor") and cached.get("renderer"):
            return {"vendor": str(cached["vendor"]), "renderer": str(cached["renderer"])}
    except (OSError, ValueError):
        pass
    renderer = _probe_host_renderer()
    match = re.match(r"ANGLE \(([^,]+),", renderer or "")
    if renderer is None or match is None:
        logger.warning("GPU хоста не распознан ({!r}): отпечаток не будет привязан к нему", renderer)
        return None
    gpu = {"vendor": f"Google Inc. ({match.group(1)})", "renderer": renderer}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(gpu), encoding="utf-8")
    except OSError as exc:
        logger.warning("Не удалось сохранить GPU хоста {}: {}", path, exc)
    return gpu


def get_or_create_fingerprint(session_dir: Path, target_os: str = "windows", *, force_new: bool = False) -> dict[str, Any]:
    """Pin the device fingerprint (GPU, audio, screen) to the account: generate it once and reuse it on every start.

    On a Windows host a new Windows fingerprint takes the host's own GPU so plain and unmasked WebGL agree.

    The file stores the OS it was generated for (older flat files are read as is). A different requested OS or
    `force_new` makes a new fingerprint: an identity must not claim Windows on a macOS profile.
    """
    path = Path(session_dir) / FINGERPRINT_FILE
    saved = None if force_new else _read_saved(path)
    if saved is not None:
        fingerprint, stored_os = saved
        if stored_os is None or stored_os == target_os.lower():
            return fingerprint
        logger.warning("В настройках os={}, а сохранённый отпечаток для {}: генерирую новый ({})", target_os, stored_os, path)

    from camoufox.fingerprints import generate_fingerprint

    fingerprint = None
    gpu = host_gpu(path.parent.parent) if target_os.lower() == "windows" and sys.platform == "win32" else None
    if gpu:
        try:
            fingerprint = generate_fingerprint(os=target_os, gpu=gpu)
        except Exception as exc:
            logger.warning("Нет отпечатка с GPU хоста {}: {}. Беру случайный", gpu["renderer"], exc)
    if fingerprint is None:
        fingerprint = generate_fingerprint(os=target_os)
    try:
        _write_atomic(path, fingerprint, target_os)
    except OSError as exc:
        logger.warning("Не удалось сохранить отпечаток {}: {}. Он не будет закреплён за аккаунтом", path, exc)
        return fingerprint
    logger.info("Создан и сохранён цифровой отпечаток: {}", path)
    return fingerprint


def load_fingerprint(session_dir: Path) -> dict[str, Any] | None:
    """The fingerprint pinned to the profile, read-only: never generates or rewrites the file."""
    saved = _read_saved(Path(session_dir) / FINGERPRINT_FILE)
    return saved[0] if saved else None


def is_compatible(fingerprint: dict[str, Any]) -> bool:
    """True when the installed Camoufox can turn this fingerprint into a browser config (format drift after updates)."""
    try:
        from camoufox.fingerprints import from_fpgen

        from_fpgen(fingerprint)
    except Exception as exc:
        logger.debug("Отпечаток несовместим с установленным Camoufox: {}", exc)
        return False
    return True


__all__ = ["FINGERPRINT_FILE", "FORMAT_VERSION", "get_or_create_fingerprint", "is_compatible", "load_fingerprint"]
