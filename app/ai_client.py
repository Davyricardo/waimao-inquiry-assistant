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
import time
from typing import Optional

import httpx

from . import crypto_util, db

# ============ Token 计费基准 (基于 DeepSeek 官方定价与美元汇率) ============
# DeepSeek 官方标准单价：输入 $0.27 / 1M Tokens，输出 $1.10 / 1M Tokens
COST_PROMPT_PER_MILLION = 0.27
COST_COMPLETION_PER_MILLION = 1.10
USD_TO_RMB_RATE = 7.2


def calculate_cost(prompt_tokens: int, completion_tokens: int) -> tuple[float, float]:
    """计算 USD 和 RMB 费用。返回 (cost_usd, cost_rmb)。"""
    usd = (prompt_tokens * COST_PROMPT_PER_MILLION + completion_tokens * COST_COMPLETION_PER_MILLION) / 1_000_000.0
    rmb = usd * USD_TO_RMB_RATE
    return round(usd, 6), round(rmb, 6)


def record_ai_usage(
    model: str,
    purpose: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    latency_ms: Optional[int] = None,
    message_id: Optional[int] = None,
    contact_id: Optional[int] = None,
    status: str = "ok",
    error_msg: Optional[str] = None,
) -> None:
    """将 AI 调用明细和 Token 计费记录写入 ai_logs 表。绝不抛异常。"""
    try:
        if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
            total_tokens = prompt_tokens + completion_tokens
        cost_usd, cost_rmb = calculate_cost(prompt_tokens, completion_tokens)
        ts = db.now_ts()
        with db.tx() as conn:
            conn.execute(
                """INSERT INTO ai_logs (
                    ts, model, purpose, prompt_tokens, completion_tokens, total_tokens,
                    cost_usd, cost_rmb, latency_ms, message_id, contact_id, status, error_msg
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts, model or "unknown", purpose or "general",
                    int(prompt_tokens or 0), int(completion_tokens or 0), int(total_tokens or 0),
                    cost_usd, cost_rmb, latency_ms, message_id, contact_id,
                    status or "ok", error_msg,
                ),
            )
    except Exception:
        pass


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
        "label": "DeepSeek（深度求索 · 推荐）",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "key_url": "https://platform.deepseek.com/api_keys",
        "portal_name": "DeepSeek 开放平台",
        "hint": "国内直连极速稳定，推理与中文能力出众，性价比极高。推荐作为外贸助手首选主力模型。",
    },
    "dashscope": {
        "label": "阿里云百炼（通义千问 Qwen）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1#/api-key",
        "portal_name": "阿里云百炼大模型控制台",
        "hint": "阿里云账号直接开通，国内直连毫秒级响应，兼容 OpenAI 规范。新用户赠送大额免费 Token。",
    },
    "hunyuan": {
        "label": "腾讯混元（Tencent Hunyuan）",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "model": "hunyuan-standard",
        "key_url": "https://console.cloud.tencent.com/hunyuan/api-key",
        "portal_name": "腾讯云混元大模型控制台",
        "hint": "腾讯云官方混元大模型，原生兼容 OpenAI 接口。在腾讯云控制台「API-KEY 管理」页面一键创建。",
    },
    "moonshot": {
        "label": "月之暗面（Moonshot Kimi）",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "portal_name": "Moonshot 开放平台",
        "hint": "长文本与上下文理解能力优异，特别适合复杂外贸长信摘要与买家背景推理。",
    },
    "zhipu": {
        "label": "智谱 AI（GLM-4）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "portal_name": "智谱大模型开放平台",
        "hint": "glm-4-flash 模型提供长期免费调用额度，多语种支持优秀，适合中小外贸企业零成本跑通。",
    },
    "qianfan": {
        "label": "百度千帆（文心一言 ERNIE）",
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-speed-128k",
        "key_url": "https://console.bce.baidu.com/qianfan/ais/console/onlineService",
        "portal_name": "百度智能云千帆平台",
        "hint": "百度千帆平台支持 OpenAI 兼容 API 规范。在千帆控制台「在线服务」申请并创建密钥。",
    },
    "minimax": {
        "label": "MiniMax（海螺 AI）",
        "base_url": "https://api.minimax.chat/v1",
        "model": "abab6.5s-chat",
        "key_url": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
        "portal_name": "MiniMax 开放平台",
        "hint": "MiniMax 开放平台支持 OpenAI 接口规范，在「开放平台 - 账户中心 - 接口密钥」创建专属 API Key。",
    },
    "yi": {
        "label": "零一万物（01.AI）",
        "base_url": "https://api.lingyiwanwu.com/v1",
        "model": "yi-lightning",
        "key_url": "https://platform.lingyiwanwu.com/apikeys",
        "portal_name": "零一万物开放平台",
        "hint": "李开复创立的零一万物大模型，在开放平台「API 密钥管理」创建，具备出色的双语商业理解力。",
    },
    "openai": {
        "label": "OpenAI（ChatGPT）",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_url": "https://platform.openai.com/api-keys",
        "portal_name": "OpenAI Platform Dashboard",
        "hint": "国际顶级通用模型。注意：需部署于境外服务器或具备海外网络环境，国内直连可能超时。",
    },
    "custom": {
        "label": "自定义服务商（兼容 OpenAI 规范）",
        "base_url": "",
        "model": "",
        "key_url": "",
        "portal_name": "本地部署或自建代理",
        "hint": "填入任何兼容 OpenAI /v1/chat/completions 规范的 API 服务地址（例如 Ollama、vLLM、OneAPI 等）。",
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
         timeout: int = 60, purpose: str = "general",
         message_id: Optional[int] = None, contact_id: Optional[int] = None) -> Optional[str]:
    """同步调用模型，返回正文文本。任何失败返回 None，由调用方降级。"""
    if not is_enabled():
        return None
    key = get_api_key()
    if not key:
        return None

    model = get_model()
    payload = _build_payload(messages, max_tokens, temperature)
    t0 = time.time()
    try:
        with _sync_client(timeout) as client:
            resp = client.post(
                _endpoint(),
                content=json.dumps(payload).encode("utf-8"),
                headers=_build_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            latency_ms = int((time.time() - t0) * 1000)
            usage = data.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens") or 0
            completion_tokens = usage.get("completion_tokens") or 0
            total_tokens = usage.get("total_tokens") or (prompt_tokens + completion_tokens)
            record_ai_usage(
                model=model, purpose=purpose,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                total_tokens=total_tokens, latency_ms=latency_ms,
                message_id=message_id, contact_id=contact_id,
                status="ok",
            )
            return _parse_response(data)
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        record_ai_usage(
            model=model, purpose=purpose,
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
            latency_ms=latency_ms, message_id=message_id, contact_id=contact_id,
            status="error", error_msg=f"{type(e).__name__}: {str(e)[:300]}",
        )
        return None


# ============ 异步调用（FastAPI 路由，不阻塞事件循环） ============

async def chat_async(messages: list, max_tokens: int = 800,
                     temperature: float = 0.2, timeout: int = 60,
                     purpose: str = "general",
                     message_id: Optional[int] = None,
                     contact_id: Optional[int] = None) -> Optional[str]:
    """异步调用模型。在 FastAPI 路由中 await 此函数，避免阻塞事件循环。"""
    if not is_enabled():
        return None
    key = get_api_key()
    if not key:
        return None

    model = get_model()
    payload = _build_payload(messages, max_tokens, temperature)
    t0 = time.time()
    try:
        async with _async_client(timeout) as client:
            resp = await client.post(
                _endpoint(),
                content=json.dumps(payload).encode("utf-8"),
                headers=_build_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            latency_ms = int((time.time() - t0) * 1000)
            usage = data.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens") or 0
            completion_tokens = usage.get("completion_tokens") or 0
            total_tokens = usage.get("total_tokens") or (prompt_tokens + completion_tokens)
            record_ai_usage(
                model=model, purpose=purpose,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                total_tokens=total_tokens, latency_ms=latency_ms,
                message_id=message_id, contact_id=contact_id,
                status="ok",
            )
            return _parse_response(data)
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        record_ai_usage(
            model=model, purpose=purpose,
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
            latency_ms=latency_ms, message_id=message_id, contact_id=contact_id,
            status="error", error_msg=f"{type(e).__name__}: {str(e)[:300]}",
        )
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
                   max_tokens=400, timeout=45, purpose="test")
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
                                max_tokens=400, timeout=45, purpose="test")
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
