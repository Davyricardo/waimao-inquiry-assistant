"""统一配置 —— 使用 pydantic-settings 托管所有配置项。

改进：
- BaseSettings 自动读取 .env 文件
- 字段带类型标注，pydantic 自动类型转换与验证
- 启动时立即发现配置错误（而非运行时 KeyError）
- 完全向后兼容：所有字段名与调用方一致
"""
import os
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent

# 智能探测保密金库路径 (优先级：MB_VAULT_DIR 环境变量 > 同级「外贸询盘助手-保密数据」> 当前目录)
_env_vault = os.environ.get("MB_VAULT_DIR", "").strip()
if _env_vault and Path(_env_vault).is_dir():
    VAULT_DIR = Path(_env_vault).resolve()
elif (BASE_DIR.parent / "外贸询盘助手-保密数据").is_dir():
    VAULT_DIR = (BASE_DIR.parent / "外贸询盘助手-保密数据").resolve()
else:
    VAULT_DIR = BASE_DIR

# 确定 .env 配置文件路径：本地优先，其次金库
if (BASE_DIR / ".env").is_file():
    _ENV_FILE = str(BASE_DIR / ".env")
elif (VAULT_DIR / ".env").is_file():
    _ENV_FILE = str(VAULT_DIR / ".env")
else:
    _ENV_FILE = str(BASE_DIR / ".env")

# 确定默认数据目录：若未显式指定 MB_DATA_DIR 环境变量，优先使用金库 data/，否则使用本地 data/
if "MB_DATA_DIR" not in os.environ and (VAULT_DIR / "data").is_dir():
    _DEFAULT_DATA_DIR = VAULT_DIR / "data"
else:
    _DEFAULT_DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    """应用配置。优先级：环境变量 > .env 文件 > 字段默认值。"""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="MB_",          # 所有字段对应 MB_XXX 环境变量
        extra="ignore",            # 忽略 .env 中不认识的键
        case_sensitive=False,
        populate_by_name=True,
    )

    # ============ 数据目录 ============
    DATA_DIR: Path = Field(default=_DEFAULT_DATA_DIR, alias="MB_DATA_DIR")
    DB: Optional[Path] = Field(default=None, alias="MB_DB")

    # ============ Web ============
    HOST: str = Field(default="127.0.0.1")
    PORT: int = Field(default=8090)
    SECRET_KEY: str = Field(default="")
    ADMIN_USER: str = Field(default="admin")
    ADMIN_PASS_HASH: str = Field(default="")
    SESSION_TTL: int = Field(default=12 * 3600)

    # ============ 凭据加密 ============
    CRED_KEY: str = Field(default="")

    # ============ 邮件引擎 ============
    POLL_INTERVAL: int = Field(default=60)
    MAX_FETCH: int = Field(default=50)
    LOOKBACK_DAYS: int = Field(default=7)

    # ============ AI ============
    AI_BASE_URL: str = Field(default="https://api.deepseek.com")
    AI_API_KEY: str = Field(default="")
    AI_MODEL: str = Field(default="deepseek-chat")
    AI_TIMEOUT: int = Field(default=60)
    AI_FALLBACK: str = Field(default="1")   # "1" / "0"

    # ============ 业务阈值 ============
    HIGH_INTENT: int = Field(default=70)
    AUTO_SEND: str = Field(default="0")     # "1" / "0"

    # ============ 时区 ============
    TZ_OFFSET: int = Field(default=8)

    # ============ 跳过文件夹（常量，不从环境变量读取） ============
    SKIP_FOLDERS: frozenset = frozenset({
        "Sent Messages", "Drafts", "Trash", "Junk", "Deleted Messages",
        "已发送", "草稿箱", "已删除", "垃圾邮件", "广告邮件"
    })

    @field_validator("DATA_DIR", mode="before")
    @classmethod
    def _resolve_data_dir(cls, v):
        p = Path(v)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ===== 派生属性（保持与旧 config 的字段名完全一致） =====
    @property
    def DB_PATH(self) -> Path:
        if self.DB:
            p = Path(self.DB)
        else:
            p = self.DATA_DIR / "mailbutler.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def ATTACH_DIR(self) -> Path:
        d = self.DATA_DIR / "attachments"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def MAX_FETCH_PER_CYCLE(self) -> int:
        return self.MAX_FETCH

    @property
    def INITIAL_LOOKBACK_DAYS(self) -> int:
        return self.LOOKBACK_DAYS

    @property
    def AI_FALLBACK_RULES(self) -> bool:
        return self.AI_FALLBACK == "1"

    @property
    def AUTO_SEND_BOOL(self) -> bool:
        return self.AUTO_SEND == "1"

    @property
    def TZ_OFFSET_HOURS(self) -> int:
        return self.TZ_OFFSET


# 全局单例（模块级导入即生效）
_settings = Settings()

# ===== 向后兼容：旧代码 from app import config; config.HOST 等保持不变 =====
DATA_DIR = _settings.DATA_DIR
DB_PATH = _settings.DB_PATH
ATTACH_DIR = _settings.ATTACH_DIR
HOST = _settings.HOST
PORT = _settings.PORT
SECRET_KEY = _settings.SECRET_KEY
ADMIN_USER = _settings.ADMIN_USER
ADMIN_PASS_HASH = _settings.ADMIN_PASS_HASH
SESSION_TTL = _settings.SESSION_TTL
CRED_KEY = _settings.CRED_KEY
POLL_INTERVAL = _settings.POLL_INTERVAL
MAX_FETCH_PER_CYCLE = _settings.MAX_FETCH_PER_CYCLE
INITIAL_LOOKBACK_DAYS = _settings.INITIAL_LOOKBACK_DAYS
SKIP_FOLDERS = _settings.SKIP_FOLDERS
AI_BASE_URL = _settings.AI_BASE_URL
AI_API_KEY = _settings.AI_API_KEY
AI_MODEL = _settings.AI_MODEL
AI_TIMEOUT = _settings.AI_TIMEOUT
AI_FALLBACK_RULES = _settings.AI_FALLBACK_RULES
HIGH_INTENT = _settings.HIGH_INTENT
AUTO_SEND = _settings.AUTO_SEND_BOOL
TZ_OFFSET_HOURS = _settings.TZ_OFFSET_HOURS
