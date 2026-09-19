"""Pydantic v2 Schema 定义集。

三大分组：
- **Form Schema**  ：HTML 表单 POST 参数校验，继承 BaseModel，字段带 validator
- **Response Schema**：/api/ 端点返回体，供 FastAPI response_model 使用
- **内部 DTO**      ：queries.py 返回值的类型标注辅助

所有 Schema 均可序列化为 dict（.model_dump()），与现有 `dict(sqlite3.Row)` 兼容。
"""
from __future__ import annotations

import re
from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator

T = TypeVar("T")

# ===================================================================
# 通用工具
# ===================================================================

def _strip(v: str | None) -> str:
    return (v or "").strip()


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _float_or_zero(v: Any) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


# ===================================================================
# Response Schema（/api/ 端点返回值）
# ===================================================================

class PaginatedResult(BaseModel, Generic[T]):
    """通用分页响应。"""
    items: List[T]
    total: int
    page: int
    size: int
    pages: int


class DashboardResp(BaseModel):
    today_in: int = 0
    yday_in: int = 0
    delta: Optional[int] = None
    high_intent: int = 0
    pending_review: int = 0
    sent_today: int = 0
    auto_today: int = 0
    total_contacts: int = 0
    unread: int = 0
    auto_rate: int = 0
    account: Optional[dict] = None
    demo_mode: bool = False


class MessageItem(BaseModel):
    id: int
    subject: Optional[str] = None
    from_addr: Optional[str] = None
    from_name: Optional[str] = None
    sent_ts: Optional[int] = None
    category: Optional[str] = None
    score: Optional[int] = None
    status: str = "new"
    direction: str = "in"
    is_read: int = 0
    contact_name: Optional[str] = None
    contact_company: Optional[str] = None

    model_config = {"extra": "allow"}


class ContactItem(BaseModel):
    id: int
    email: str
    name: Optional[str] = None
    company: Optional[str] = None
    country: Optional[str] = None
    stage: str = "new"
    score: int = 0
    msg_count: int = 0
    reply_count: int = 0
    credibility: Optional[str] = None

    model_config = {"extra": "allow"}


class OrderItem(BaseModel):
    id: int
    order_no: str
    title: str
    stage: str
    currency: str = "USD"
    total_amount: float = 0.0
    est_delivery_date: Optional[str] = None

    model_config = {"extra": "allow"}


class ProductItem(BaseModel):
    id: int
    sku: str
    name_en: str
    name_cn: Optional[str] = None
    price_usd: float = 0.0
    cost_cny: float = 0.0
    moq: int = 1
    carton_cbm: float = 0.0
    is_active: int = 1

    model_config = {"extra": "allow"}


class ForwarderItem(BaseModel):
    id: int
    name: str
    contact_person: Optional[str] = None
    rating: int = 5
    quote_count: int = 0

    model_config = {"extra": "allow"}


class SocialLeadItem(BaseModel):
    id: int
    name: str
    company: Optional[str] = None
    source_platform: str = "linkedin"
    stage: str = "discovered"
    rating: int = 3
    next_followup_date: Optional[str] = None

    model_config = {"extra": "allow"}


class ApiOk(BaseModel):
    ok: bool = True
    message: str = ""
    id: Optional[int] = None


class ApiError(BaseModel):
    ok: bool = False
    error: str


# ===================================================================
# Form Schema（HTML 表单 POST 参数校验）
# ===================================================================

VALID_CONTACT_STAGES = {"new", "engaging", "quoted", "negotiating", "won", "lost", "spam"}
VALID_ORDER_STAGES   = {"draft", "pi_confirmed", "deposit_received", "in_production",
                        "qc_passed", "balance_received", "in_transit", "completed"}
VALID_TRADE_TERMS    = {"FOB", "CIF", "CFR", "DDP", "DAP", "EXW"}
VALID_CURRENCIES     = {"USD", "EUR", "CNY", "GBP", "JPY"}
VALID_CHANNELS       = {"sea", "air", "express", "rail", "truck"}
VALID_PRICE_UNITS    = {"kg", "cbm", "20gp", "40hq", "flat"}
VALID_LEAD_STAGES    = {"discovered", "connected", "chatted", "catalog_sent",
                        "converted", "unqualified"}
VALID_PLATFORMS      = {"linkedin", "whatsapp", "facebook", "instagram",
                        "tiktok", "google", "exhibition", "other"}


class ContactForm(BaseModel):
    """POST /contacts/save 表单校验。"""
    id: Optional[str] = ""
    email: str
    name: str = ""
    company: str = ""
    country: str = ""
    stage: str = "new"
    company_type: str = ""
    channel_role: str = ""
    credibility: str = ""
    note: str = ""
    csrf_tok: str = ""

    @field_validator("email", mode="before")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = _strip(v).lower()
        if not v or "@" not in v:
            raise ValueError("请提供有效的客户邮箱")
        return v

    @field_validator("stage", mode="before")
    @classmethod
    def validate_stage(cls, v: str) -> str:
        v = _strip(v).lower() or "new"
        if v not in VALID_CONTACT_STAGES:
            return "new"
        return v

    @field_validator("name", "company", "country", "company_type",
                     "channel_role", "credibility", "note", mode="before")
    @classmethod
    def strip_str(cls, v):
        return _strip(v)


class OrderForm(BaseModel):
    """POST /orders/save 表单校验。"""
    id: str = ""
    contact_id: str = ""
    order_no: str = ""
    title: str = ""
    stage: str = "draft"
    trade_term: str = "FOB"
    payment_term: str = ""
    currency: str = "USD"
    total_amount: str = "0"
    deposit_amount: str = "0"
    balance_amount: str = "0"
    settlement_fx_rate: str = "7.20"
    factory_cost: str = "0"
    shipping_cost: str = "0"
    other_cost: str = "0"
    est_delivery_date: str = ""
    actual_delivery_date: str = ""
    tracking_or_bl_no: str = ""
    destination_port: str = ""
    forwarder_quote_id: str = ""
    note: str = ""
    csrf_tok: str = ""

    @field_validator("stage", mode="before")
    @classmethod
    def validate_stage(cls, v):
        v = _strip(v) or "draft"
        return v if v in VALID_ORDER_STAGES else "draft"

    @field_validator("trade_term", mode="before")
    @classmethod
    def validate_trade_term(cls, v):
        v = _strip(v).upper() or "FOB"
        return v if v in VALID_TRADE_TERMS else "FOB"

    @field_validator("currency", mode="before")
    @classmethod
    def validate_currency(cls, v):
        v = _strip(v).upper() or "USD"
        return v if v in VALID_CURRENCIES else "USD"

    @field_validator("total_amount", "deposit_amount", "balance_amount",
                     "settlement_fx_rate", "factory_cost", "shipping_cost",
                     "other_cost", mode="before")
    @classmethod
    def validate_numeric(cls, v):
        try:
            float(_strip(v) or "0")
        except ValueError:
            return "0"
        return _strip(v) or "0"


class ForwarderForm(BaseModel):
    """POST /freight/forwarder/save 表单校验。"""
    id: str = ""
    name: str
    contact_person: str = ""
    phone: str = ""
    email: str = ""
    wechat_or_im: str = ""
    advantages: str = ""
    rating: str = "5"
    address: str = ""
    note: str = ""
    csrf_tok: str = ""

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, v):
        v = _strip(v)
        if not v:
            raise ValueError("货代公司/代理名称不能为空")
        return v

    @field_validator("rating", mode="before")
    @classmethod
    def validate_rating(cls, v):
        try:
            r = int(_strip(v) or "5")
            return str(max(1, min(5, r)))
        except ValueError:
            return "5"

    @field_validator("contact_person", "phone", "email", "wechat_or_im",
                     "advantages", "address", "note", mode="before")
    @classmethod
    def strip_str(cls, v):
        return _strip(v)


class FreightQuoteForm(BaseModel):
    """POST /freight/quote/save 表单校验。"""
    id: str = ""
    forwarder_id: str = ""
    title: str
    channel_type: str = "sea"
    origin: str = "深圳"
    destination: str = ""
    unit_price: str = "0"
    price_unit: str = "kg"
    currency: str = "CNY"
    min_charge: str = ""
    transit_time_text: str = ""
    valid_until: str = ""
    extra_fees: str = ""
    remarks: str = ""
    csrf_tok: str = ""

    @field_validator("title", mode="before")
    @classmethod
    def validate_title(cls, v):
        v = _strip(v)
        if not v:
            raise ValueError("渠道方案名称不能为空")
        return v

    @field_validator("channel_type", mode="before")
    @classmethod
    def validate_channel(cls, v):
        v = _strip(v) or "sea"
        return v if v in VALID_CHANNELS else "sea"

    @field_validator("price_unit", mode="before")
    @classmethod
    def validate_price_unit(cls, v):
        v = _strip(v) or "kg"
        return v if v in VALID_PRICE_UNITS else "kg"

    @field_validator("unit_price", mode="before")
    @classmethod
    def validate_unit_price(cls, v):
        try:
            float(_strip(v) or "0")
        except ValueError:
            return "0"
        return _strip(v) or "0"

    @field_validator("valid_until", mode="before")
    @classmethod
    def validate_date(cls, v):
        v = _strip(v)
        if v and not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            return ""
        return v


class ProductForm(BaseModel):
    """POST /products/save 表单校验。"""
    id: str = ""
    sku: str
    name_en: str
    name_cn: str = ""
    hs_code: str = ""
    category: str = "default"
    specs: str = ""
    unit: str = "PCS"
    price_usd: str = "0"
    cost_cny: str = "0"
    moq: str = "1"
    carton_qty: str = "1"
    carton_length_cm: str = "0"
    carton_width_cm: str = "0"
    carton_height_cm: str = "0"
    carton_gw_kg: str = "0"
    carton_nw_kg: str = "0"
    image_url: str = ""
    note: str = ""
    csrf_tok: str = ""

    @field_validator("sku", mode="before")
    @classmethod
    def validate_sku(cls, v):
        v = _strip(v).upper()
        if not v:
            raise ValueError("产品型号/货号不能为空")
        return v

    @field_validator("name_en", mode="before")
    @classmethod
    def validate_name_en(cls, v):
        v = _strip(v)
        if not v:
            raise ValueError("英文品名不能为空")
        return v

    @field_validator("price_usd", "cost_cny", "carton_length_cm", "carton_width_cm",
                     "carton_height_cm", "carton_gw_kg", "carton_nw_kg", mode="before")
    @classmethod
    def validate_float(cls, v):
        try:
            float(_strip(v) or "0")
        except ValueError:
            return "0"
        return _strip(v) or "0"

    @field_validator("moq", "carton_qty", mode="before")
    @classmethod
    def validate_int(cls, v):
        try:
            int(_strip(v) or "1")
        except ValueError:
            return "1"
        return _strip(v) or "1"


class SocialLeadForm(BaseModel):
    """POST /social/lead/save 表单校验。"""
    id: str = ""
    name: str
    company: str = ""
    country: str = ""
    position: str = ""
    source_platform: str = "linkedin"
    whatsapp: str = ""
    linkedin_url: str = ""
    social_handle: str = ""
    email: str = ""
    website: str = ""
    industry_or_niche: str = ""
    stage: str = "discovered"
    rating: str = "3"
    next_followup_date: str = ""
    notes: str = ""
    csrf_tok: str = ""

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, v):
        v = _strip(v)
        if not v:
            raise ValueError("潜客姓名不能为空")
        return v

    @field_validator("source_platform", mode="before")
    @classmethod
    def validate_platform(cls, v):
        v = _strip(v) or "linkedin"
        return v if v in VALID_PLATFORMS else "linkedin"

    @field_validator("stage", mode="before")
    @classmethod
    def validate_stage(cls, v):
        v = _strip(v) or "discovered"
        return v if v in VALID_LEAD_STAGES else "discovered"

    @field_validator("rating", mode="before")
    @classmethod
    def validate_rating(cls, v):
        try:
            r = int(_strip(v) or "3")
            return str(max(1, min(5, r)))
        except ValueError:
            return "3"

    @field_validator("next_followup_date", mode="before")
    @classmethod
    def validate_date(cls, v):
        v = _strip(v)
        if v and not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            return ""
        return v

    @field_validator("company", "country", "position", "whatsapp", "linkedin_url",
                     "social_handle", "email", "website", "industry_or_niche",
                     "notes", mode="before")
    @classmethod
    def strip_str(cls, v):
        return _strip(v)
