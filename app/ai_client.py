"""AI 客户端层：统一的模型调用入口 + 运行时可配置的凭据解析。

设计要点
--------
1. **凭据优先级**：数据库 settings 表 > 环境变量 > 默认值。
   用户在后台设置页填完 API Key 就立刻生效，不用改 .env、不用重启。
2. **API Key 加密存储**：与邮箱授权码一样走 AES-256-GCM，落库的是密文。
3. **失败绝不抛异常**：调用方拿 None 自行降级（规则分析 / 模板渲染），
   保证一封信分析失败不会卡死整条流水线。
4. **双模调用**：
   - `chat()`         — 同步调用，供 Worker / 流水线后台任务使用
   - `chat_async()`   — async，供 FastAPI 路由直接 await，不阻塞事件循环
5. **连接复用**：httpx.Client / AsyncClient 复用底层连接，避免每次重建。
6. **不走 OpenClaw 网关**：直连上游 /chat/completions，省两个数量级 token。
"""
import json
import re
from typing import Optional

import httpx

from . import crypto_util, db

# ============ 凭据解析 ============

K_API_KEY  = "ai_api_key"    # 存密文
K_BASE_URL = "ai_base_url"
K_MODEL    = "ai_model"
K_ENABLED  = "ai_enabled"   # "1"/"0"，总开关

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL    = "deepseek-chat"

# 常见服务商预设，设置页下拉用
AI_PRESETS = {
    "deepseek": {
        "label": "DeepSeek（推荐，性价比高）",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "key_url": "https://platform.deepseek.com/api_keys",
        "hint": "国内直连稳定，中文能力好，价格低。注册后在「API Keys」页面创建。",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_url": "https://platform.openai.com/api-keys",
        "hint": "需要境外网络环境。服务器若在国内可能连不上。",
    },
    "dashscope": {
        "label": "阿里云百炼（通义千问）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "key_url": "https://bailian.console.aliyun.com/",
        "hint": "阿里云账号直接开通，兼容 OpenAI 格式，国内节点快。",
    },
    "moonshot": {
        "label": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "hint": "长文本能力强，适合邮件这种中长文本。",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "hint": "glm-4-flash 有免费额度，适合先试。",
    },
    "custom": {
        "label": "自定义（兼容 OpenAI 格式）",
        "base_url": "",
        "model": "",
        "key_url": "",
        "hint": "填任何兼容 /chat/completions 的服务地址即可。",
    },
}

# ============ 复用 HTTP 客户端（避免每次 AI 调用新建连接） ============

_LIMITS = httpx.Limits(max_keepalive_connections=5, max_connections=10)


def _sync_client(timeout: int = 60) -> httpx.Client:
    """创建同步 httpx 客户端（调用方负责 close，或在 with 块中使用）。"""
    return httpx.Client(
        limits=_LIMITS,
        timeout=httpx.Timeout(connect=10.0, read=float(timeout), write=30.0, pool=5.0),
        follow_redirects=False,
    )


def _async_client(timeout: int = 60) -> httpx.AsyncClient:
    """创建异步 httpx 客户端。"""
    return httpx.AsyncClient(
        limits=_LIMITS,
        timeout=httpx.Timeout(connect=10.0, read=float(timeout), write=30.0, pool=5.0),
        follow_redirects=False,
    )


# ============ 凭据读取 ============

def _get_setting(key: str) -> str:
    """读 settings 表。任何异常都返回空串。"""
    try:
        with db.ro() as conn:
            r = conn.execute("SELECT value FROM settings WHERE key=?",
                             (key,)).fetchone()
        return (r["value"] or "") if r else ""
    except Exception:
        return ""


def get_api_key_enc() -> str:
    return _get_setting(K_API_KEY)


def get_api_key() -> str:
    enc = _get_setting(K_API_KEY)
    if enc:
        try:
            return crypto_util.decrypt_secret(enc)
        except Exception:
            pass
    from . import config
    return config.AI_API_KEY or ""


def get_base_url() -> str:
    v = _get_setting(K_BASE_URL).strip()
    if v:
        return v
    from . import config
    return config.AI_BASE_URL or DEFAULT_BASE_URL


def get_model() -> str:
    v = _get_setting(K_MODEL).strip()
    if v:
        return v
    from . import config
    return config.AI_MODEL or DEFAULT_MODEL


def is_enabled() -> bool:
    v = _get_setting(K_ENABLED)
    if v in ("0", "1"):
        return v == "1"
    return bool(get_api_key())


def is_configured() -> bool:
    return bool(get_api_key())


def status_text() -> str:
    if not is_configured():
        return "未配置（当前为规则降级模式）"
    if not is_enabled():
        return "已配置但已关闭"
    return f"已启用 · {get_model()}"


# ============ 构建请求载荷 ============

def _build_payload(messages: list, max_tokens: int, temperature: float) -> dict:
    return {
        "model": get_model(),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def _safe_ascii(s: str) -> str:
    """将字符串中不可作 HTTP header 值的字符过滤掉。

    HTTP/1.1 头字段值只能含 latin-1 可表示的可见字符（0x20-0xFF，不含控制符）。
    如果 API Key 里不小心夹带了中文或其他 Unicode 字符，httpx 会抛出
    UnicodeEncodeError: 'latin-1' codec can't encode characters ...
    """
    try:
        s.encode("latin-1")
        return s          # 全部合法，直接用
    except (UnicodeEncodeError, UnicodeDecodeError):
        # 过滤：只保留 latin-1 可表示的字符
        cleaned = "".join(c for c in s if ord(c) < 256)
        return cleaned


def _build_headers() -> dict:
    key = _safe_ascii(get_api_key())
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }


def _endpoint() -> str:
    base = get_base_url().rstrip("/")
    # Base URL 不应含非 ASCII 字符；若含有则说明配置错误
    try:
        base.encode("ascii")
    except (UnicodeEncodeError, UnicodeDecodeError):
        # 尝试提取可能混在中文说明里的 URL 部分
        import re as _re
        m = _re.search(r"https?://[^\s\u4e00-\u9fff]+", base)
        base = m.group(0).rstrip("/") if m else DEFAULT_BASE_URL
    return base + "/chat/completions"


def _parse_response(data: dict) -> Optional[str]:
    choices = data.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    if "couldn't generate a response" in content.lower():
        return None
    return content or None


# ============ 同步调用（Worker / 流水线后台任务） ============

def chat(messages: list, max_tokens: int = 800, temperature: float = 0.2,
         timeout: int = 60) -> Optional[str]:
    """同步调用模型，返回正文文本。任何失败返回 None，由调用方降级。"""
    if not is_enabled():
        return None
    key = get_api_key()
    if not key:
        return None

    payload = _build_payload(messages, max_tokens, temperature)
    try:
        with _sync_client(timeout) as client:
            resp = client.post(
                _endpoint(),
                content=json.dumps(payload).encode("utf-8"),
                headers=_build_headers(),
            )
            resp.raise_for_status()
            return _parse_response(resp.json())
    except (httpx.HTTPError, httpx.TimeoutException, json.JSONDecodeError,
            OSError, ValueError, UnicodeEncodeError):
        return None


# ============ 异步调用（FastAPI 路由，不阻塞事件循环） ============

async def chat_async(messages: list, max_tokens: int = 800,
                     temperature: float = 0.2, timeout: int = 60) -> Optional[str]:
    """异步调用模型。在 FastAPI 路由中 await 此函数，避免阻塞事件循环。"""
    if not is_enabled():
        return None
    key = get_api_key()
    if not key:
        return None

    payload = _build_payload(messages, max_tokens, temperature)
    try:
        async with _async_client(timeout) as client:
            resp = await client.post(
                _endpoint(),
                content=json.dumps(payload).encode("utf-8"),
                headers=_build_headers(),
            )
            resp.raise_for_status()
            return _parse_response(resp.json())
    except (httpx.HTTPError, httpx.TimeoutException, json.JSONDecodeError,
            OSError, ValueError, UnicodeEncodeError):
        return None


# ============ 连通性测试 ============

def test_connection() -> tuple[bool, str]:
    """测试连通性。返回 (是否成功, 说明)。用于设置页的「测试」按钮。"""
    key = get_api_key()
    if not key:
        return False, "尚未填写 API Key"

    # 提前检测常见配置错误：Key 或 URL 含非 ASCII 字符
    try:
        key.encode("latin-1")
    except (UnicodeEncodeError, UnicodeDecodeError):
        bad_chars = [c for c in key if ord(c) > 127]
        sample = "".join(bad_chars[:5])
        return False, (f"API Key 含非法字符（位置 7-12 附近含：{sample!r}），"
                       "请重新粘贴纯英文数字组成的 Key，不要夹带中文或特殊符号")
    try:
        get_base_url().encode("ascii")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False, ("Base URL 含非 ASCII 字符，"
                       "请填写纯英文地址，例如 https://api.deepseek.com")

    try:
        raw = chat([{"role": "user", "content": "回复两个字：正常"}],
                   max_tokens=400, timeout=45)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if raw:
        return True, f"连接成功 · 模型 {get_model()}"
    return False, ("调用失败：请检查 Key、Base URL、模型名，"
                   "以及服务器能否访问该地址")


async def test_connection_async() -> tuple[bool, str]:
    """异步版连通性测试，供 FastAPI 路由 await。"""
    if not get_api_key():
        return False, "尚未填写 API Key"
    try:
        raw = await chat_async([{"role": "user", "content": "回复两个字：正常"}],
                                max_tokens=400, timeout=45)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if raw:
        return True, f"连接成功 · 模型 {get_model()}"
    return False, ("调用失败：请检查 Key、Base URL、模型名，"
                   "以及服务器能否访问该地址")


# ============ JSON 提取 ============

def extract_json(text: str) -> Optional[dict]:
    """从模型输出里抠出 JSON，容忍前后废话与 ``` 包裹。"""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


# ============ 凭据持久化 ============

def save_credentials(api_key: Optional[str], base_url: str, model: str,
                     enabled: bool) -> None:
    """写入 AI 配置。api_key 为 None 表示不改动已存的 Key。"""
    ts = db.now_ts()
    rows = [(K_BASE_URL, base_url.strip()),
            (K_MODEL, model.strip()),
            (K_ENABLED, "1" if enabled else "0")]
    if api_key is not None and api_key.strip():
        rows.append((K_API_KEY, crypto_util.encrypt_secret(api_key.strip())))
    with db.tx() as conn:
        for k, v in rows:
            conn.execute(
                "INSERT INTO settings (key,value,updated_ts) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                "updated_ts=excluded.updated_ts", (k, v, ts))


def clear_api_key() -> None:
    """清空已存的 API Key。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM settings WHERE key=?", (K_API_KEY,))
