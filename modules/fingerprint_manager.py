from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

FINGERPRINT_FILE = "fingerprint.json"


def get_or_create_fingerprint(session_dir: Path, target_os: str = "windows") -> dict[str, Any]:
    """Pin the device fingerprint (GPU, audio, screen) to the account: generate it once and reuse it on every start."""
    path = Path(session_dir) / FINGERPRINT_FILE
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved:
                return saved
            logger.warning("Файл отпечатка {} пуст или повреждён, генерирую новый", path)
        except (OSError, ValueError) as exc:
            logger.warning("Не удалось прочитать отпечаток {}: {}. Генерирую новый", path, exc)

    from camoufox.fingerprints import generate_fingerprint

    fingerprint: dict[str, Any] = generate_fingerprint(os=target_os)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fingerprint, ensure_ascii=False), encoding="utf-8")
    logger.info("Создан и сохранён цифровой отпечаток: {}", path)
    return fingerprint


__all__ = ["FINGERPRINT_FILE", "get_or_create_fingerprint"]
