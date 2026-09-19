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

from . import config, crypto_util, db

LOCKOUT = {}          # ip -> (fail_count, until_ts)
MAX_FAILS = 8
WINDOW = 300

# 常见推荐密保问题预设
PRESET_SECURITY_QUESTIONS = [
    "您母亲的姓名是？",
    "您出生的城市是？",
    "您的第一所小学名称是？",
    "您最喜欢的外贸出口产品品类是？",
    "您的第一只宠物名字是？",
    "您高中班主任的名字是？",
    "自定义密保问题",
]


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


# ============ 管理员凭据与密保安全管理 ============

def get_admin_user() -> str:
    """获取当前生效的管理员用户名。优先 settings 表，兜底环境变量。"""
    try:
        with db.ro() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='admin_user'").fetchone()
            if row and row["value"] and row["value"].strip():
                return row["value"].strip()
    except Exception:
        pass
    return config.ADMIN_USER or "admin"


def get_admin_pass_hash() -> str:
    """获取当前生效的管理员密码哈希。优先 settings 表，兜底环境变量。"""
    try:
        with db.ro() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='admin_pass_hash'").fetchone()
            if row and row["value"] and row["value"].strip():
                return row["value"].strip()
    except Exception:
        pass
    return config.ADMIN_PASS_HASH or ""


def check_login(user: str, password: str) -> bool:
    """登录凭据校验。支持动态用户名与动态密码哈希。"""
    pass_hash = get_admin_pass_hash()
    if not pass_hash:
        return False
    admin_user = get_admin_user()
    if not hmac.compare_digest(user or "", admin_user):
        return False
    return crypto_util.verify_password(password, pass_hash)


def get_security_question() -> str:
    """获取当前已配置的密保问题。"""
    try:
        with db.ro() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='security_question'").fetchone()
            if row and row["value"] and row["value"].strip():
                return row["value"].strip()
    except Exception:
        pass
    return ""


def has_security_question() -> bool:
    """判断系统是否已设置密保问题与密保答案。"""
    try:
        with db.ro() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='security_answer_hash'").fetchone()
            return bool(row and row["value"] and row["value"].strip())
    except Exception:
        return False


def verify_security_answer(answer: str) -> bool:
    """校验密保答案。经过 strip() 并转小写后核验 PBKDF2 哈希。"""
    norm = str(answer or "").strip().lower()
    if not norm:
        return False
    try:
        with db.ro() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='security_answer_hash'").fetchone()
            if not row or not row["value"]:
                return False
            return crypto_util.verify_password(norm, row["value"].strip())
    except Exception:
        return False


def update_admin_credentials(
    current_password: str,
    security_answer: str = "",
    new_username: str = "",
    new_password: str = "",
    new_security_question: str = "",
    new_security_answer: str = "",
) -> tuple[bool, str, str]:
    """修改管理员凭据及密保设置。
    返回: (成功状态, 提示信息, 最终生效的用户名)
    """
    # 1. 严格核验当前管理员原密码
    curr_hash = get_admin_pass_hash()
    if not curr_hash or not crypto_util.verify_password(current_password, curr_hash):
        return False, "当前原密码输入错误，身份核验失败", ""

    # 2. 密保问题核验
    if has_security_question():
        if not verify_security_answer(security_answer):
            return False, "密保问题验证失败：密保答案不正确", ""
    else:
        # 首次修改管理员信息时，系统强制要求设定密保问题与答案
        q = (new_security_question or "").strip()
        a = (new_security_answer or "").strip()
        if not q or not a:
            return False, "为了保障系统账户安全，首次修改管理员信息时必须设定密保问题及答案", ""

    # 3. 用户名校验
    target_user = get_admin_user()
    if new_username and new_username.strip():
        u = new_username.strip()
        if len(u) < 2 or len(u) > 40:
            return False, "管理员用户名长度须在 2 到 40 个字符之间", ""
        target_user = u

    # 4. 新密码校验
    new_pass_hash = None
    if new_password and new_password.strip():
        p = new_password.strip()
        if len(p) < 6:
            return False, "新密码长度不能少于 6 个字符", ""
        new_pass_hash = crypto_util.hash_password(p)

    # 5. 新密保问题校验（如果用户填写了要更新的密保）
    new_q = (new_security_question or "").strip()
    new_a = (new_security_answer or "").strip().lower()
    new_a_hash = None
    if new_q and new_a:
        if len(new_q) < 2:
            return False, "密保问题内容过短，请选择或输入清晰的问题", ""
        new_a_hash = crypto_util.hash_password(new_a)
    elif new_q or new_a:
        return False, "更新密保设置时，密保问题与答案必须同时填写", ""

    # 6. 原子写入 settings 表
    ts = db.now_ts()
    updates = [("admin_user", target_user)]
    if new_pass_hash:
        updates.append(("admin_pass_hash", new_pass_hash))
    if new_a_hash:
        updates.append(("security_question", new_q))
        updates.append(("security_answer_hash", new_a_hash))
        updates.append(("security_question_set_ts", str(ts)))

    try:
        with db.tx() as conn:
            for k, v in updates:
                conn.execute(
                    "INSERT INTO settings (key,value,updated_ts) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                    (k, v, ts),
                )
    except Exception as e:
        return False, f"保存凭据至数据库失败: {e}", ""

    return True, "管理员凭据与安全设置已成功保存", target_user


def reset_admin_password_by_security(security_answer: str, new_password: str) -> tuple[bool, str]:
    """通过密保问题答案重置管理员密码。"""
    if not has_security_question():
        return False, "系统尚未配置密保问题，无法通过密保找回密码"
    if not verify_security_answer(security_answer):
        return False, "密保答案验证不正确"
    p = str(new_password or "").strip()
    if len(p) < 6:
        return False, "新密码长度不能少于 6 个字符"

    new_hash = crypto_util.hash_password(p)
    ts = db.now_ts()
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO settings (key,value,updated_ts) VALUES ('admin_pass_hash',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                (new_hash, ts),
            )
        return True, "密码已通过密保安全重置，请使用新密码登录"
    except Exception as e:
        return False, f"重置密码失败: {e}"


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
