"""数据库访问层。SQLite + WAL + SQLAlchemy 2.0。

架构：双轨并行
- **原生 sqlite3**：`tx()` / `ro()` 保持不变，供所有现有 queries.py 使用
- **SQLAlchemy Engine**：`get_engine()` / `orm_session()` 供新路由/ORM 查询使用

约定：
- 所有时间戳统一用 UTC 秒（INTEGER），created_ts/sent_ts 等一律如此
- SQLite 连接统一设 WAL + foreign_keys=ON + busy_timeout=30000
- ORM Session 通过 Session(bind=engine) 创建，使用完毕即关闭
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from . import config
from .models import Base

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# ===================================================================
# SQLAlchemy Engine（全局单例，线程安全）
# ===================================================================
_engine: Engine | None = None


def _configure_sqlite(dbapi_connection, connection_record):
    """每次获得原始 sqlite3 连接时应用 PRAGMA。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def get_engine() -> Engine:
    """返回全局 SQLAlchemy Engine（惰性初始化）。"""
    global _engine
    if _engine is None:
        url = f"sqlite:///{config.DB_PATH}"
        _engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            echo=False,           # 生产环境关闭 SQL 日志，调试时可改 True
            pool_pre_ping=True,
        )
        event.listen(_engine, "connect", _configure_sqlite)
    return _engine


def get_session_factory():
    """返回 sessionmaker 工厂。"""
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def orm_session():
    """ORM 会话上下文：自动提交，异常自动回滚。

    用法::

        with orm_session() as sess:
            contact = sess.get(Contact, cid)
    """
    factory = get_session_factory()
    sess: Session = factory()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


# ===================================================================
# 原有 sqlite3 接口（保持不变，供全部 queries.py 继续使用）
# ===================================================================

def now_ts() -> int:
    return int(time.time())


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def tx():
    """事务上下文：正常提交，异常回滚。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def ro():
    """只读上下文。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# ===================================================================
# 数据库初始化（幂等）
# ===================================================================

def init_db() -> None:
    """建表 + 迁移 + 建索引 + 预置数据。幂等，可反复执行。

    执行顺序很关键：
      1. schema.sql → CREATE TABLE / CREATE INDEX
      2. _migrate()  → ALTER TABLE 补新列
      3. POST_MIGRATE_INDEXES → 依赖新列的索引
      4. _seed()     → 预置数据
    """
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect()
    try:
        conn.executescript(sql)
        _migrate(conn)
        _seed(conn)
    finally:
        conn.close()

    # 同时确保 SQLAlchemy Engine 已初始化（方便后续 orm_session() 直接使用）
    get_engine()


# 增量迁移：{表名: [(列名, 列定义), ...]}
MIGRATIONS = {
    "messages": [
        ("summary_cn", "TEXT"),
        ("key_points_cn", "TEXT"),
        ("urgency_cn", "TEXT"),
        ("translated_cn", "TEXT"),
        ("summary_source", "TEXT"),
        ("summarized_ts", "INTEGER"),
    ],
    "contacts": [
        ("background", "TEXT"),
        ("company_type", "TEXT"),
        ("channel_role", "TEXT"),
        ("credibility", "TEXT"),
        ("profiled_ts", "INTEGER"),
        ("whatsapp", "TEXT"),
        ("linkedin_url", "TEXT"),
        ("social_lead_id", "INTEGER"),
    ],
    "orders": [
        ("loading_port", "TEXT DEFAULT 'Shenzhen, China'"),
        ("carrier", "TEXT"),
        ("shipping_marks", "TEXT DEFAULT 'N/M'"),
        ("pi_date", "TEXT"),
        ("ci_date", "TEXT"),
    ],
}

POST_MIGRATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_contacts_cred ON contacts(credibility)",
]


def _migrate(conn: sqlite3.Connection) -> None:
    """给已存在的表补新列。缺什么补什么，已有的跳过。"""
    for table, cols in MIGRATIONS.items():
        try:
            have = {r["name"] for r in
                    conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error:
            continue
        if not have:
            continue
        for col, decl in cols:
            if col in have:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.Error:
                pass
    for ddl in POST_MIGRATE_INDEXES:
        try:
            conn.execute(ddl)
        except sqlite3.Error:
            pass


def _seed(conn: sqlite3.Connection) -> None:
    """预置默认模板与规则。已存在则跳过。"""
    ts = now_ts()

    defaults = [
        ("inquiry_ack", "询盘收到自动确认", "inquiry", "en",
         "Re: {subject}",
         "Dear {name},\n\n"
         "Thank you for your inquiry. We have received your message and our "
         "sales team is reviewing it now.\n\n"
         "We will get back to you within 1 business day with detailed "
         "information and pricing.\n\n"
         "Best regards,\n"
         "{signature}",
         "礼貌确认收到，说明 1 个工作日内回复。不要报价，不要承诺交期。"),

        ("catalog_send", "发送产品目录", "inquiry", "en",
         "Re: {subject}",
         "Dear {name},\n\n"
         "Thanks for your interest in our products. Please find our latest "
         "catalog attached.\n\n"
         "If you could share the specific models and quantities you need, "
         "we can quote you precisely.\n\n"
         "Best regards,\n"
         "{signature}",
         "引导客户给出具体型号和数量，为后续报价铺垫。"),

        ("quote_reply", "报价回复", "quote", "en",
         "Re: {subject}",
         "Dear {name},\n\n"
         "Thank you for your inquiry. Please find our quotation below.\n\n"
         "[QUOTE DETAILS HERE]\n\n"
         "Prices are valid for 30 days. Payment terms and lead time can be "
         "discussed further.\n\n"
         "Best regards,\n"
         "{signature}",
         "涉及价格必须人工填写，AI 不得自行编造价格。留空待人工补。"),

        ("sample_offer", "样品寄送回复", "inquiry", "en",
         "Re: {subject}",
         "Dear {name},\n\n"
         "We are glad to offer samples for your evaluation. Sample cost and "
         "shipping details are as follows:\n\n"
         "[SAMPLE DETAILS HERE]\n\n"
         "Best regards,\n"
         "{signature}",
         "样品费用需人工确认后填写。"),

        ("after_sale", "售后问题回复", "after_sale", "en",
         "Re: {subject}",
         "Dear {name},\n\n"
         "Thank you for reaching out. We are sorry for the inconvenience.\n\n"
         "Our after-sales team is looking into your case and will respond "
         "within 24 hours.\n\n"
         "Best regards,\n"
         "{signature}",
         "售后类必须先安抚，具体方案需人工确认。"),
    ]
    for k, name, cat, lang, subj, body, hint in defaults:
        conn.execute(
            "INSERT OR IGNORE INTO templates "
            "(key,name,category,lang,subject,body,ai_hint,is_active,created_ts) "
            "VALUES (?,?,?,?,?,?,?,1,?)",
            (k, name, cat, lang, subj, body, hint, ts),
        )

    rules = [
        ("垃圾邮件自动丢弃", '{"categories":["spam"]}', "drop", None, 10),
        ("高意向询盘人工审核", '{"categories":["inquiry","quote"],"min_score":70}',
         "draft_only", "inquiry_ack", 20),
        ("常规询盘起草待审", '{"categories":["inquiry","quote"]}',
         "draft_only", "inquiry_ack", 30),
        ("售后起草待审", '{"categories":["after_sale"]}',
         "draft_only", "after_sale", 40),
        ("投诉仅提醒", '{"categories":["complaint"]}', "notify", None, 50),
    ]
    for name, cond, action, tpl, prio in rules:
        exist = conn.execute("SELECT 1 FROM rules WHERE name=?", (name,)).fetchone()
        if not exist:
            conn.execute(
                "INSERT INTO rules (name,cond_json,action,template_key,priority,"
                "is_active,created_ts) VALUES (?,?,?,?,?,1,?)",
                (name, cond, action, tpl, prio, ts),
            )

    for k, v in [("auto_send", "0"), ("high_intent", "70"),
                 ("reply_signature", "Best regards,\n{sender_name}")]:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key,value,updated_ts) VALUES (?,?,?)",
            (k, v, ts),
        )
