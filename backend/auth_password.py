"""密码哈希、验证与强度校验。

使用 PBKDF2-HMAC-SHA256（600000 轮迭代）对密码进行加盐哈希，
存储格式为 `pbkdf2_sha256$iterations$salt_hex$hash_hex`。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

# PBKDF2 参数
_PBKDF2_ALGORITHM = "sha256"
_PBKDF2_ROUNDS = 600_000
_PBKDF2_DKLEN = 32

# 密码强度要求
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def hash_password(password: str) -> str:
    """对密码进行 PBKDF2-HMAC-SHA256 加盐哈希。

    返回格式: pbkdf2_sha256$iterations$salt_hex$hash_hex
    """
    salt = secrets.token_bytes(32)
    hash_bytes = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        _PBKDF2_ROUNDS,
        dklen=_PBKDF2_DKLEN,
    )
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${hash_bytes.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """验证密码是否与存储的哈希匹配。

    使用 hmac.compare_digest 进行常数时间比较，防止时序攻击。
    """
    if not stored_hash:
        return False
    try:
        parts = stored_hash.split("$", 3)
        if len(parts) != 4:
            return False
        algorithm, rounds_str, salt_hex, hash_hex = parts
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.pbkdf2_hmac(
            _PBKDF2_ALGORITHM,
            password.encode("utf-8"),
            salt,
            rounds,
            dklen=len(expected),
        )
        return hmac.compare_digest(expected, actual)
    except (ValueError, AttributeError, TypeError):
        return False


def validate_password_strength(password: str) -> tuple[bool, str]:
    """校验密码强度。

    返回 (是否通过, 错误消息)。
    要求：
    - 长度 8~128
    - 至少包含大写字母、小写字母、数字各一个
    """
    if not password:
        return False, "密码不能为空"
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"密码长度不能少于 {MIN_PASSWORD_LENGTH} 位"
    if len(password) > MAX_PASSWORD_LENGTH:
        return False, f"密码长度不能超过 {MAX_PASSWORD_LENGTH} 位"
    has_upper = any(c.isupper() for c in password)
    has_lower = any(c.islower() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_upper and has_lower and has_digit):
        missing = []
        if not has_upper:
            missing.append("大写字母")
        if not has_lower:
            missing.append("小写字母")
        if not has_digit:
            missing.append("数字")
        return False, f"密码必须包含{'、'.join(missing)}"
    return True, ""
