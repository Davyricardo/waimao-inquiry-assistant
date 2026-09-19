"""会话与鉴权。

设计：
- 服务端签名 cookie（HMAC），不落库，简单可靠
- 登录失败限速：同一 IP 连续失败递增延迟并锁定
- CSRF：所有写操作校验同源 token
"""
import base64
import hashlib
import hmac
import json
import time

from . import config, crypto_util

LOCKOUT = {}          # ip -> (fail_count, until_ts)
MAX_FAILS = 8
WINDOW = 300


def _sign(payload: bytes) -> str:
    key = (config.SECRET_KEY or "insecure-dev").encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def make_session(user: str) -> str:
    data = {"u": user, "exp": int(time.time()) + config.SESSION_TTL,
            "n": crypto_util.new_token(8)}
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode() + "." + _sign(raw)


def read_session(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    b64, sig = token.rsplit(".", 1)
    try:
        raw = base64.urlsafe_b64decode(b64.encode())
    except Exception:
        return None
    if not hmac.compare_digest(_sign(raw), sig):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


def check_login(user: str, password: str) -> bool:
    if not config.ADMIN_PASS_HASH:
        return False
    if not hmac.compare_digest(user or "", config.ADMIN_USER):
        return False
    return crypto_util.verify_password(password, config.ADMIN_PASS_HASH)


def throttle_check(ip: str) -> tuple[bool, int]:
    """返回 (是否被锁, 剩余秒数)。"""
    cnt, until = LOCKOUT.get(ip, (0, 0))
    if until > time.time():
        return True, int(until - time.time())
    return False, 0


def throttle_fail(ip: str) -> None:
    cnt, _ = LOCKOUT.get(ip, (0, 0))
    cnt += 1
    if cnt >= MAX_FAILS:
        LOCKOUT[ip] = (cnt, time.time() + WINDOW)
    else:
        LOCKOUT[ip] = (cnt, 0)


def throttle_reset(ip: str) -> None:
    LOCKOUT.pop(ip, None)


def csrf_token(session_token: str) -> str:
    """由会话 token 派生，无需额外存储。"""
    key = (config.SECRET_KEY or "insecure-dev").encode()
    return hmac.new(key, (session_token + "csrf").encode(),
                    hashlib.sha256).hexdigest()[:32]
