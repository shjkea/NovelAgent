
import ctypes
from ctypes import wintypes
from pathlib import Path
import os


def _environment_name(path) -> str:
    """Map a configured secret filename to an optional environment variable."""
    stem = Path(path).stem.upper()
    aliases = {
        "GROK_API_KEY": "XAI_API_KEY",
        "VOLCENGINE_OPENAPI_AK": "VOLCENGINE_ACCESS_KEY_ID",
        "VOLCENGINE_OPENAPI_SK": "VOLCENGINE_SECRET_ACCESS_KEY",
    }
    for source, target in aliases.items():
        if stem == source or stem.startswith(source + "_"):
            return target + stem[len(source):]
    return stem

class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]

def _blob_from_bytes(data: bytes):
    buf = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(
        len(data),
        ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
    )
    return blob, buf

def _blob_to_bytes(blob):
    if not blob.pbData or not blob.cbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)

def protect_text(text: str) -> bytes:
    raw = text.encode("utf-8")
    if os.name != "nt":
        raise RuntimeError(
            "Persistent API-key storage requires Windows DPAPI. "
            "On Linux or macOS, provide the corresponding environment variable."
        )

    in_blob, in_buf = _blob_from_bytes(raw)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "NovelAgent Secret",
        None, None, None, 0,
        ctypes.byref(out_blob)
    )
    if not ok:
        raise ctypes.WinError()

    try:
        return _blob_to_bytes(out_blob)
    finally:
        kernel32.LocalFree(out_blob.pbData)

def unprotect_text(data: bytes) -> str:
    if os.name != "nt":
        raise RuntimeError("DPAPI secret can only be decrypted on Windows.")

    in_blob, in_buf = _blob_from_bytes(data)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None, None, None, None, 0,
        ctypes.byref(out_blob)
    )
    if not ok:
        raise ctypes.WinError()

    try:
        return _blob_to_bytes(out_blob).decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)

def save_secret(path, text: str):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(protect_text(text))

def load_secret(path):
    p = Path(path)
    env_name = _environment_name(p)
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        return env_value
    if not p.exists():
        return ""
    return unprotect_text(p.read_bytes())
