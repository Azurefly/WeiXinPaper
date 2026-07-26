from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import os
import secrets
import stat
from ctypes import wintypes
from pathlib import Path

PREFIX = "enc:v1:"
_DPAPI_PREFIX = b"dpapi:v1:"
_PASSWORD_ROUNDS = 600_000


def _key_path() -> Path:
    configured = os.environ.get("STUDIO_MASTER_KEY_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / "data" / ".master.key").resolve()


def _salt_path() -> Path:
    return _key_path().with_suffix(_key_path().suffix + ".salt")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.unlink(missing_ok=True)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _password_key(password: str) -> bytes:
    salt_path = _salt_path()
    if salt_path.exists():
        salt = salt_path.read_bytes()
        if len(salt) < 16:
            raise RuntimeError("master password salt file invalid")
    else:
        salt = secrets.token_bytes(32)
        _atomic_write(salt_path, salt)
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ROUNDS, dklen=32)


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DATA_BLOB, object]:
    buffer = ctypes.create_string_buffer(data)
    return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(b"WeiXinGZH-AI-Studio-2.1.2")
    out_blob = _DATA_BLOB()
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "WeiXinGZH AI Studio master key",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(out_blob),
    )
    _ = (in_buffer, entropy_buffer)
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(b"WeiXinGZH-AI-Studio-2.1.2")
    out_blob = _DATA_BLOB()
    description = wintypes.LPWSTR()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(out_blob),
    )
    _ = (in_buffer, entropy_buffer)
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(out_blob.pbData)


def _validate_posix_permissions(path: Path) -> None:
    if os.name == "nt" or not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise RuntimeError("master key file permissions are too broad and cannot be repaired") from exc
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise RuntimeError("master key file must be readable only by the current user")


def _key() -> bytes:
    raw_env = os.environ.get("STUDIO_MASTER_KEY", "").strip()
    if raw_env:
        return hashlib.sha256(raw_env.encode("utf-8")).digest()

    password = os.environ.get("STUDIO_MASTER_PASSWORD", "")
    if password:
        if len(password) < 12:
            raise RuntimeError("STUDIO_MASTER_PASSWORD must contain at least 12 characters")
        return _password_key(password)

    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stored = path.read_bytes()
        if os.name == "nt":
            if stored.startswith(_DPAPI_PREFIX):
                value = _dpapi_unprotect(base64.urlsafe_b64decode(stored[len(_DPAPI_PREFIX):]))
            elif len(stored) >= 32:
                # Migrate the 2.1.1 raw key to current-user DPAPI protection without changing the key.
                value = stored[:32]
                protected = _DPAPI_PREFIX + base64.urlsafe_b64encode(_dpapi_protect(value))
                _atomic_write(path, protected)
            else:
                raise RuntimeError("master key file invalid")
        else:
            _validate_posix_permissions(path)
            value = stored[:32]
        if len(value) != 32:
            raise RuntimeError("master key file invalid")
        return value

    value = secrets.token_bytes(32)
    if os.name == "nt":
        stored = _DPAPI_PREFIX + base64.urlsafe_b64encode(_dpapi_protect(value))
    else:
        stored = value
    _atomic_write(path, stored)
    _validate_posix_permissions(path)
    return value


def key_storage_description() -> str:
    if os.environ.get("STUDIO_MASTER_KEY", "").strip():
        return "environment-key"
    if os.environ.get("STUDIO_MASTER_PASSWORD", ""):
        return "password-derived"
    return "windows-dpapi" if os.name == "nt" else "user-only-key-file"


def encrypt(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value
    key = _key()
    nonce = secrets.token_bytes(16)
    raw = value.encode("utf-8")
    encrypted = bytearray()
    for offset in range(0, len(raw), 32):
        stream = hmac.new(key, nonce + (offset // 32).to_bytes(8, "big"), hashlib.sha256).digest()
        encrypted.extend(left ^ right for left, right in zip(raw[offset:offset + 32], stream))
    tag = hmac.new(key, b"auth" + nonce + bytes(encrypted), hashlib.sha256).digest()
    return PREFIX + base64.urlsafe_b64encode(nonce + tag + bytes(encrypted)).decode("ascii")


def decrypt(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value
    try:
        blob = base64.urlsafe_b64decode(value[len(PREFIX):])
    except Exception as exc:
        raise RuntimeError("encrypted secret format invalid") from exc
    if len(blob) < 48:
        raise RuntimeError("encrypted secret format invalid")
    nonce, tag, cipher = blob[:16], blob[16:48], blob[48:]
    key = _key()
    expected = hmac.new(key, b"auth" + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise RuntimeError("secret integrity check failed; verify the master key or password")
    output = bytearray()
    for offset in range(0, len(cipher), 32):
        stream = hmac.new(key, nonce + (offset // 32).to_bytes(8, "big"), hashlib.sha256).digest()
        output.extend(left ^ right for left, right in zip(cipher[offset:offset + 32], stream))
    return bytes(output).decode("utf-8")
