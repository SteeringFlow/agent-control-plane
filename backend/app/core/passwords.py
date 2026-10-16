"""最小口令验证器（Cloud-0.1.1-C）——**仅标准库**。

刻意**不**引入 bcrypt / passlib / argon2 / JWT 等依赖（Gate §3 禁区）：

- 用 stdlib `hashlib.pbkdf2_hmac`（sha256）+ 随机 salt；
- 存储格式：`pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`；
- 比对用 `hmac.compare_digest`（常量时间）。

这是「最小 Browser Auth」的凭据校验原语，**不是**密码策略 / 复杂度 / 过期 /
重置 / Remember Me 框架——那些属登录体验优化，C.1 明确不做。
"""

from __future__ import annotations

import hashlib
import hmac
import os

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 200_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """把明文口令编码为可入库的验证器字符串（每次调用使用新随机 salt）。"""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _ITERATIONS
    )
    return f"{_ALGORITHM}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """常量时间校验明文口令与验证器是否匹配；任何格式异常一律返回 False。"""
    try:
        algorithm, iterations_s, salt_hex, hash_hex = encoded.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError, AttributeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


__all__ = ["hash_password", "verify_password"]
