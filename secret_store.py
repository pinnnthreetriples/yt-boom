from __future__ import annotations

import base64
import sys

DPAPI_PREFIX = "dpapi:"


class SecretStoreError(RuntimeError):
    """A stored secret cannot be decrypted (another Windows user or machine, damaged value)."""


def _blob_call(function_name: str, data: bytes) -> bytes:
    """Run CryptProtectData / CryptUnprotectData on `data` (current-user scope, no UI)."""
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = getattr(crypt32, function_name)
    function.restype = wintypes.BOOL
    function.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR if function_name == "CryptProtectData" else ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    buffer = ctypes.create_string_buffer(data, len(data))
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = _DataBlob()
    crypt_protect_ui_forbidden = 0x1
    if not function(ctypes.byref(source), None, None, None, None, crypt_protect_ui_forbidden, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


def protect_secret(value: str | None) -> str | None:
    """Encrypt a secret with Windows DPAPI for storage: `dpapi:<base64>`. Other platforms keep plain text."""
    if not value or sys.platform != "win32" or value.startswith(DPAPI_PREFIX):
        return value
    encrypted = _blob_call("CryptProtectData", value.encode("utf-8"))
    return DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")


def reveal_secret(value: str | None) -> str | None:
    """Decrypt a stored secret. Values without the `dpapi:` prefix (written by older versions) are plain text."""
    if not value or not value.startswith(DPAPI_PREFIX):
        return value
    if sys.platform != "win32":
        raise SecretStoreError("значение зашифровано Windows DPAPI и недоступно на этой платформе")
    try:
        encrypted = base64.b64decode(value[len(DPAPI_PREFIX):], validate=True)
        return _blob_call("CryptUnprotectData", encrypted).decode("utf-8")
    except (OSError, ValueError) as exc:
        raise SecretStoreError("не удалось расшифровать значение (другой пользователь Windows или повреждённые данные)") from exc


__all__ = ["DPAPI_PREFIX", "SecretStoreError", "protect_secret", "reveal_secret"]
