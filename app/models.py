"""SQLAlchemy 2.0 ORM 模型声明。

设计原则：
- 使用 DeclarativeBase + mapped_column，充分利用 Python 类型标注
- 与现有 SQLite schema.sql 完全对应，字段名/类型一致
- 所有时间戳统一 UTC 整数秒（Integer），与现有约定相同
- 外键约束开启（SQLite 需要在连接层额外 PRAGMA，见 db.py）
- 不替换现有 queries.py 中的原生 SQL，只提供可选的 ORM 访问路径
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import (
    Boolean, Float, ForeignKey, Index, Integer, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类。"""
    pass


# ============================================================
# accounts — 邮箱账户
# ============================================================
class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[Optional[str]] = mapped_column(Text)
    imap_host: Mapped[str] = mapped_column(Text, nullable=False, default="imap.qq.com")
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, default=993)
    smtp_host: Mapped[str] = mapped_column(Text, nullable=False, default="smtp.qq.com")
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=465)
    secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    watch_folder: Mapped[str] = mapped_column(Text, nullable=False, default="INBOX")
    since_ts: Mapped[Optional[int]] = mapped_column(Integer)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_sync_ts: Mapped[Optional[int]] = mapped_column(Integer)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    # relationships
    messages: Mapped[list["Message"]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )


# ============================================================
# contacts — 客户
# ============================================================
class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        Index("idx_contacts_score", "score"),
        Index("idx_contacts_last", "last_seen_ts"),
        Index("idx_contacts_cred", "credibility"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[Optional[str]] = mapped_column(Text)
    company: Mapped[Optional[str]] = mapped_column(Text)
    country: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="email")
    first_seen_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    last_seen_ts: Mapped[Optional[int]] = mapped_column(Integer)
    msg_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reply_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    note: Mapped[Optional[str]] = mapped_column(Text)
    background: Mapped[Optional[str]] = mapped_column(Text)
    company_type: Mapped[Optional[str]] = mapped_column(Text)
    channel_role: Mapped[Optional[str]] = mapped_column(Text)
    credibility: Mapped[Optional[str]] = mapped_column(Text)
    profiled_ts: Mapped[Optional[int]] = mapped_column(Integer)
    whatsapp: Mapped[Optional[str]] = mapped_column(Text)
    linkedin_url: Mapped[Optional[str]] = mapped_column(Text)
    social_lead_id: Mapped[Optional[int]] = mapped_column(Integer)

    threads: Mapped[list["Thread"]] = relationship(
        back_populates="contact", cascade="all, delete-orphan", passive_deletes=True
    )
    messages: Mapped[list["Message"]] = relationship(
        back_populates="contact", cascade="all, delete-orphan", passive_deletes=True
    )
    orders: Mapped[list["Order"]] = relationship(
        back_populates="contact", cascade="all, delete-orphan", passive_deletes=True
    )
    social_leads: Mapped[list["SocialLead"]] = relationship(
        back_populates="contact", passive_deletes=True
    )


# ============================================================
# threads — 会话线索
# ============================================================
class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (
        Index("idx_threads_contact", "contact_id"),
        Index("idx_threads_last", "last_ts"),
        Index("idx_threads_root", "root_msgid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    subject: Mapped[Optional[str]] = mapped_column(Text)
    root_msgid: Mapped[Optional[str]] = mapped_column(Text)
    msg_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_ts: Mapped[Optional[int]] = mapped_column(Integer)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")

    contact: Mapped["Contact"] = relationship(back_populates="threads")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan", passive_deletes=True
    )


# ============================================================
# messages — 邮件
# ============================================================
class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_msg_thread", "thread_id", "sent_ts"),
        Index("idx_msg_contact", "contact_id", "sent_ts"),
        Index("idx_msg_status", "status", "sent_ts"),
        Index("idx_msg_sent", "sent_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE")
    )
    contact_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("contacts.id", ondelete="CASCADE")
    )
    thread_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("threads.id", ondelete="CASCADE")
    )
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    msgid: Mapped[Optional[str]] = mapped_column(Text)
    in_reply_to: Mapped[Optional[str]] = mapped_column(Text)
    refs: Mapped[Optional[str]] = mapped_column(Text)
    folder: Mapped[Optional[str]] = mapped_column(Text)
    uid: Mapped[Optional[str]] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    from_addr: Mapped[Optional[str]] = mapped_column(Text)
    from_name: Mapped[Optional[str]] = mapped_column(Text)
    to_addr: Mapped[Optional[str]] = mapped_column(Text)
    subject: Mapped[Optional[str]] = mapped_column(Text)
    body_text: Mapped[Optional[str]] = mapped_column(Text)
    body_html: Mapped[Optional[str]] = mapped_column(Text)
    attachments: Mapped[Optional[str]] = mapped_column(Text)
    sent_ts: Mapped[Optional[int]] = mapped_column(Integer)
    category: Mapped[Optional[str]] = mapped_column(Text)
    score: Mapped[Optional[int]] = mapped_column(Integer)
    entities: Mapped[Optional[str]] = mapped_column(Text)
    ai_summary: Mapped[Optional[str]] = mapped_column(Text)
    ai_rationale: Mapped[Optional[str]] = mapped_column(Text)
    ai_model: Mapped[Optional[str]] = mapped_column(Text)
    analyzed_ts: Mapped[Optional[int]] = mapped_column(Integer)
    summary_cn: Mapped[Optional[str]] = mapped_column(Text)
    key_points_cn: Mapped[Optional[str]] = mapped_column(Text)
    urgency_cn: Mapped[Optional[str]] = mapped_column(Text)
    translated_cn: Mapped[Optional[str]] = mapped_column(Text)
    summary_source: Mapped[Optional[str]] = mapped_column(Text)
    summarized_ts: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    is_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    account: Mapped[Optional["Account"]] = relationship(back_populates="messages")
    contact: Mapped[Optional["Contact"]] = relationship(back_populates="messages")
    thread: Mapped[Optional["Thread"]] = relationship(back_populates="messages")
    drafts: Mapped[list["Draft"]] = relationship(
        back_populates="message", cascade="all, delete-orphan", passive_deletes=True
    )


# ============================================================
# drafts — 回复草稿 / 审批队列
# ============================================================
class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (
        Index("idx_draft_status", "status", "created_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    subject: Mapped[Optional[str]] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    template_key: Mapped[Optional[str]] = mapped_column(Text)
    ai_model: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    final_text: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_by: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_ts: Mapped[Optional[int]] = mapped_column(Integer)
    sent_ts: Mapped[Optional[int]] = mapped_column(Integer)
    send_error: Mapped[Optional[str]] = mapped_column(Text)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    message: Mapped["Message"] = relationship(back_populates="drafts")


# ============================================================
# rules — 自动规则
# ============================================================
class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    cond_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    action: Mapped[str] = mapped_column(Text, nullable=False, default="draft_only")
    template_key: Mapped[Optional[str]] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)


# ============================================================
# templates — 回复模板
# ============================================================
class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="any")
    lang: Mapped[str] = mapped_column(Text, nullable=False, default="en")
    subject: Mapped[Optional[str]] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    ai_hint: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)


# ============================================================
# tags / contact_tags — 客户标签
# ============================================================
class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    color: Mapped[str] = mapped_column(Text, nullable=False, default="#185FA5")


class ContactTag(Base):
    __tablename__ = "contact_tags"
    __table_args__ = (
        UniqueConstraint("contact_id", "tag_id"),
    )

    contact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False, primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=False, primary_key=True
    )
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)


# ============================================================
# audit_log — 操作日志
# ============================================================
class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[Optional[str]] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[Optional[str]] = mapped_column(Text)
    target_id: Mapped[Optional[int]] = mapped_column(Integer)
    detail: Mapped[Optional[str]] = mapped_column(Text)


# ============================================================
# settings — 键值配置
# ============================================================
class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    updated_ts: Mapped[Optional[int]] = mapped_column(Integer)


# ============================================================
# daily_stats — 每日统计快照
# ============================================================
class DailyStat(Base):
    __tablename__ = "daily_stats"

    day: Mapped[str] = mapped_column(Text, primary_key=True)
    in_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    out_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    auto_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_intent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_contacts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_score: Mapped[Optional[float]] = mapped_column(Float)
    updated_ts: Mapped[Optional[int]] = mapped_column(Integer)


# ============================================================
# forwarders — 货运代理
# ============================================================
class Forwarder(Base):
    __tablename__ = "forwarders"
    __table_args__ = (
        Index("idx_forwarders_name", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    contact_person: Mapped[Optional[str]] = mapped_column(Text)
    phone: Mapped[Optional[str]] = mapped_column(Text)
    email: Mapped[Optional[str]] = mapped_column(Text)
    wechat_or_im: Mapped[Optional[str]] = mapped_column(Text)
    advantages: Mapped[Optional[str]] = mapped_column(Text)
    rating: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    address: Mapped[Optional[str]] = mapped_column(Text)
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    quotes: Mapped[list["FreightQuote"]] = relationship(
        back_populates="forwarder", cascade="all, delete-orphan", passive_deletes=True
    )


# ============================================================
# freight_quotes — 货代报价
# ============================================================
class FreightQuote(Base):
    __tablename__ = "freight_quotes"
    __table_args__ = (
        Index("idx_freight_quotes_fwd", "forwarder_id"),
        Index("idx_freight_quotes_dest", "destination"),
        Index("idx_freight_quotes_channel", "channel_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    forwarder_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("forwarders.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    destination: Mapped[str] = mapped_column(Text, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    price_unit: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="CNY")
    min_charge: Mapped[Optional[str]] = mapped_column(Text)
    transit_time_text: Mapped[Optional[str]] = mapped_column(Text)
    valid_until: Mapped[Optional[str]] = mapped_column(Text)
    extra_fees: Mapped[Optional[str]] = mapped_column(Text)
    remarks: Mapped[Optional[str]] = mapped_column(Text)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    forwarder: Mapped["Forwarder"] = relationship(back_populates="quotes")
    orders: Mapped[list["Order"]] = relationship(
        back_populates="forwarder_quote", passive_deletes=True
    )


# ============================================================
# orders — 外贸订单
# ============================================================
class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("idx_orders_contact", "contact_id"),
        Index("idx_orders_stage", "stage"),
        Index("idx_orders_etd", "est_delivery_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    contact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    forwarder_quote_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("freight_quotes.id", ondelete="SET NULL")
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    trade_term: Mapped[str] = mapped_column(Text, nullable=False, default="FOB")
    payment_term: Mapped[Optional[str]] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="USD")
    total_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    deposit_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    balance_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    settlement_fx_rate: Mapped[float] = mapped_column(Float, nullable=False, default=7.20)
    factory_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    shipping_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    other_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    est_delivery_date: Mapped[Optional[str]] = mapped_column(Text)
    actual_delivery_date: Mapped[Optional[str]] = mapped_column(Text)
    tracking_or_bl_no: Mapped[Optional[str]] = mapped_column(Text)
    destination_port: Mapped[Optional[str]] = mapped_column(Text)
    loading_port: Mapped[Optional[str]] = mapped_column(Text, default="Shenzhen, China")
    carrier: Mapped[Optional[str]] = mapped_column(Text)
    shipping_marks: Mapped[Optional[str]] = mapped_column(Text, default="N/M")
    pi_date: Mapped[Optional[str]] = mapped_column(Text)
    ci_date: Mapped[Optional[str]] = mapped_column(Text)
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    contact: Mapped["Contact"] = relationship(back_populates="orders")
    forwarder_quote: Mapped[Optional["FreightQuote"]] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", passive_deletes=True
    )
    timeline: Mapped[list["OrderTimeline"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", passive_deletes=True
    )


# ============================================================
# order_timeline — 订单流转时间轴
# ============================================================
class OrderTimeline(Base):
    __tablename__ = "order_timeline"
    __table_args__ = (
        Index("idx_order_timeline_oid", "order_id", "created_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    from_stage: Mapped[Optional[str]] = mapped_column(Text)
    to_stage: Mapped[str] = mapped_column(Text, nullable=False)
    action_name: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str] = mapped_column(Text, nullable=False, default="admin")
    detail: Mapped[Optional[str]] = mapped_column(Text)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped["Order"] = relationship(back_populates="timeline")


# ============================================================
# products — 外贸产品库
# ============================================================
class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("idx_products_sku", "sku"),
        Index("idx_products_name", "name_en"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name_en: Mapped[str] = mapped_column(Text, nullable=False)
    name_cn: Mapped[Optional[str]] = mapped_column(Text)
    hs_code: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(Text, default="default")
    specs: Mapped[Optional[str]] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text, nullable=False, default="PCS")
    price_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    moq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    carton_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    carton_length_cm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    carton_width_cm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    carton_height_cm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    carton_cbm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    carton_gw_kg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    carton_nw_kg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    image_url: Mapped[Optional[str]] = mapped_column(Text)
    note: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    order_items: Mapped[list["OrderItem"]] = relationship(
        back_populates="product", passive_deletes=True
    )


# ============================================================
# order_items — 订单产品明细
# ============================================================
class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        Index("idx_order_items_oid", "order_id", "item_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="SET NULL")
    )
    item_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sku: Mapped[Optional[str]] = mapped_column(Text)
    name_en: Mapped[str] = mapped_column(Text, nullable=False)
    name_cn: Mapped[Optional[str]] = mapped_column(Text)
    hs_code: Mapped[Optional[str]] = mapped_column(Text)
    specs: Mapped[Optional[str]] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text, nullable=False, default="PCS")
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit_cost_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cartons: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    net_weight_kg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gross_weight_kg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cbm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped[Optional["Product"]] = relationship(back_populates="order_items")


# ============================================================
# social_leads — 社媒潜客线索
# ============================================================
class SocialLead(Base):
    __tablename__ = "social_leads"
    __table_args__ = (
        Index("idx_social_leads_stage", "stage"),
        Index("idx_social_leads_platform", "source_platform"),
        Index("idx_social_leads_followup", "next_followup_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    company: Mapped[Optional[str]] = mapped_column(Text)
    country: Mapped[Optional[str]] = mapped_column(Text)
    position: Mapped[Optional[str]] = mapped_column(Text)
    source_platform: Mapped[str] = mapped_column(Text, nullable=False, default="linkedin")
    whatsapp: Mapped[Optional[str]] = mapped_column(Text)
    linkedin_url: Mapped[Optional[str]] = mapped_column(Text)
    social_handle: Mapped[Optional[str]] = mapped_column(Text)
    email: Mapped[Optional[str]] = mapped_column(Text)
    website: Mapped[Optional[str]] = mapped_column(Text)
    industry_or_niche: Mapped[Optional[str]] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text, nullable=False, default="discovered")
    rating: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_followup_date: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    contact_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("contacts.id", ondelete="SET NULL")
    )
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    contact: Mapped[Optional["Contact"]] = relationship(back_populates="social_leads")
    touchpoints: Mapped[list["SocialTouchpoint"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", passive_deletes=True
    )


# ============================================================
# social_touchpoints — 社媒触达流水
# ============================================================
class SocialTouchpoint(Base):
    __tablename__ = "social_touchpoints"
    __table_args__ = (
        Index("idx_social_touchpoints_lead", "lead_id", "created_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("social_leads.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    touch_type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    feedback: Mapped[Optional[str]] = mapped_column(Text)
    next_action: Mapped[Optional[str]] = mapped_column(Text)
    operator: Mapped[str] = mapped_column(Text, nullable=False, default="admin")
    created_ts: Mapped[int] = mapped_column(Integer, nullable=False)

    lead: Mapped["SocialLead"] = relationship(back_populates="touchpoints")
