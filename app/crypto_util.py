"""凭据加密与管理员密码哈希。

安全设计：
- 邮箱授权码用 AES-256-GCM 加密后入库，密钥来自环境变量 MB_CRED_KEY
- 管理员密码用 PBKDF2-HMAC-SHA256（60 万次迭代）加盐哈希，绝不存明文
- 密钥缺失时直接抛错，宁可起不来也不降级成明文存储
"""
import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config

PBKDF2_ITER = 600_000


# ============ 凭据加解密 ============

def _key() -> bytes:
    raw = config.CRED_KEY
    if not raw:
        raise RuntimeError(
            "缺少 MB_CRED_KEY 环境变量。请先生成："
            "python -c \"import secrets;print(secrets.token_urlsafe(32))\""
        )
    # 任意长度字符串 -> 32 字节密钥
    return hashlib.sha256(raw.encode("utf-8")).digest()


def encrypt_secret(plain: str) -> str:
    """加密邮箱授权码。返回 base64(iv):base64(ciphertext)。"""
    aes = AESGCM(_key())
    iv = os.urandom(12)
    ct = aes.encrypt(iv, plain.encode("utf-8"), None)
    return base64.b64encode(iv).decode() + ":" + base64.b64encode(ct).decode()


def decrypt_secret(enc: str) -> str:
    aes = AESGCM(_key())
    iv_b64, ct_b64 = enc.split(":", 1)
    iv = base64.b64decode(iv_b64)
    ct = base64.b64decode(ct_b64)
    return aes.decrypt(iv, ct, None).decode("utf-8")


def decrypt_secret_safe(enc: str) -> str:
    """解密失败时返回空串而不是抛异常。

    换过 CRED_KEY 之后老密文会解不开，调用方（页面展示）不应该因此 500。
    """
    if not enc:
        return ""
    try:
        return decrypt_secret(enc)
    except Exception:
        return ""


def mask_secret(plain: str, keep_head: int = 4, keep_tail: int = 4) -> str:
    """把凭据打码成 abcd****wxyz，用于页面回显。

    太短的（<= keep_head + keep_tail）整体打码成 ****，
    避免把 16 位授权码的头尾拼出可猜的一半。
    """
    s = str(plain or "")
    if not s:
        return ""
    if len(s) <= keep_head + keep_tail:
        return "*" * len(s)
    return s[:keep_head] + "*" * (len(s) - keep_head - keep_tail) + s[-keep_tail:]


# ============ 管理员密码 ============

def hash_password(password: str) -> str:
    """返回 pbkdf2_sha256$iter$salt_b64$hash_b64"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITER)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITER,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """常量时间比对，避免时序侧信道。"""
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, int(iters))
        return hmac.compare_digest(dk, expect)
    except Exception:
        return False


def new_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)
