-- 外贸询盘助手 数据库表结构
-- SQLite 3.42+ / WAL 模式
-- 设计原则：所有时间统一 UTC 时间戳（INTEGER），展示层再转本地时区

PRAGMA foreign_keys = ON;

-- ============ 账户配置 ============
-- 邮箱账户。密码存 AES 加密后的密文，密钥在环境变量里，不落库
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL UNIQUE,
    display_name    TEXT,
    imap_host       TEXT    NOT NULL DEFAULT 'imap.qq.com',
    imap_port       INTEGER NOT NULL DEFAULT 993,
    smtp_host       TEXT    NOT NULL DEFAULT 'smtp.qq.com',
    smtp_port       INTEGER NOT NULL DEFAULT 465,
    -- 密文格式：base64(iv):base64(ciphertext)
    secret_enc      TEXT    NOT NULL,
    -- 监听哪个文件夹（QQ邮箱一般监听 INBOX）
    watch_folder    TEXT    NOT NULL DEFAULT 'INBOX',
    -- 监听起始点：首次接入时从这天之后的邮件开始处理（跳过历史邮件）
    since_ts        INTEGER,
    is_active       INTEGER NOT NULL DEFAULT 1,
    last_sync_ts    INTEGER,
    last_error      TEXT,
    created_ts      INTEGER NOT NULL
);

-- ============ 客户 ============
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL UNIQUE,
    name            TEXT,
    company         TEXT,
    country         TEXT,
    -- 客户来源渠道：email / manual / import
    source          TEXT    NOT NULL DEFAULT 'email',
    -- 累计统计（由触发器/业务层维护，便于看板快速查询）
    first_seen_ts   INTEGER NOT NULL,
    last_seen_ts    INTEGER,
    msg_count       INTEGER NOT NULL DEFAULT 0,
    reply_count     INTEGER NOT NULL DEFAULT 0,
    -- 当前最高意向分
    score           INTEGER NOT NULL DEFAULT 0,
    -- 客户阶段：new / engaging / quoted / negotiating / won / lost / spam
    stage           TEXT    NOT NULL DEFAULT 'new',
    -- 备注
    note            TEXT,
    -- ===== 自动背调结果 =====
    -- 背景档案 JSON：{company_type, channel_role, business_scope, credibility,
    --   risk_flags_cn, evidence, country_conf, source, model ...}
    background      TEXT,
    -- 公司类型（便于列表筛选）
    company_type    TEXT,
    -- 渠道身份：经销商 / 批发商 / 进口商 / 终端用户 ...
    channel_role    TEXT,
    -- 可信度：高 / 中 / 低
    credibility     TEXT,
    -- 背调时间
    profiled_ts     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_contacts_score ON contacts(score DESC);
CREATE INDEX IF NOT EXISTS idx_contacts_last  ON contacts(last_seen_ts DESC);

-- ============ 会话线索 ============
-- 同一客户的同一串邮件归并成一条线索
CREATE TABLE IF NOT EXISTS threads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id      INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    -- 归一化后的主题（去掉 Re:/Fwd: 前缀）
    subject         TEXT,
    -- 用首封邮件的 Message-ID 作为线索锚点
    root_msgid      TEXT,
    msg_count       INTEGER NOT NULL DEFAULT 0,
    last_ts         INTEGER,
    -- 线索级意向：取该线索内最高分
    score           INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_threads_contact ON threads(contact_id);
CREATE INDEX IF NOT EXISTS idx_threads_last    ON threads(last_ts DESC);
CREATE INDEX IF NOT EXISTS idx_threads_root    ON threads(root_msgid);

-- ============ 邮件 ============
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    contact_id      INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    thread_id       INTEGER REFERENCES threads(id) ON DELETE CASCADE,
    -- 去重用的指纹：优先 Message-ID，缺失则用 sha256(from+subject+date+size)
    fingerprint     TEXT    NOT NULL UNIQUE,
    msgid           TEXT,
    in_reply_to     TEXT,
    refs            TEXT,
    folder          TEXT,
    uid             TEXT,
    -- 方向：in 收件 / out 发件
    direction       TEXT    NOT NULL,
    from_addr       TEXT,
    from_name       TEXT,
    to_addr         TEXT,
    subject         TEXT,
    -- 纯文本正文（HTML 会转成文本存这里，便于检索与喂模型）
    body_text       TEXT,
    -- 原始 HTML 保留，审核台预览用
    body_html       TEXT,
    -- 附件清单 JSON：[{filename,size,mime,saved}]
    attachments     TEXT,
    sent_ts         INTEGER,
    -- AI 分析结果
    category        TEXT,
    score           INTEGER,
    entities        TEXT,
    ai_summary      TEXT,
    ai_rationale    TEXT,
    ai_model        TEXT,
    analyzed_ts     INTEGER,
    -- AI 中文摘要（独立于 ai_summary，专门存翻译后的中文要点）
    summary_cn      TEXT,
    key_points_cn   TEXT,   -- JSON 数组
    urgency_cn      TEXT,
    translated_cn   TEXT,   -- 来信正文的中文翻译
    summary_source  TEXT,   -- ai / rule，标明摘要来源
    summarized_ts   INTEGER,
    -- 处理状态：new / analyzed / drafted / auto_sent / approved / rejected / skipped
    status          TEXT    NOT NULL DEFAULT 'new',
    -- 是否已读（用于后台未读徽标）
    is_read         INTEGER NOT NULL DEFAULT 0,
    created_ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_thread   ON messages(thread_id, sent_ts);
CREATE INDEX IF NOT EXISTS idx_msg_contact  ON messages(contact_id, sent_ts DESC);
CREATE INDEX IF NOT EXISTS idx_msg_status   ON messages(status, sent_ts DESC);
CREATE INDEX IF NOT EXISTS idx_msg_sent     ON messages(sent_ts DESC);

-- ============ 自动回复草稿 / 审批队列 ============
CREATE TABLE IF NOT EXISTS drafts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    contact_id      INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    -- AI 生成的回复草稿
    subject         TEXT,
    body_text       TEXT NOT NULL,
    -- 使用的模板
    template_key    TEXT,
    ai_model        TEXT,
    -- 状态：pending 待审 / approved 已通过 / sent 已发送 / rejected 已否决
    status          TEXT    NOT NULL DEFAULT 'pending',
    -- 最终实际发出的内容（人工可能改过）
    final_text      TEXT,
    reviewed_by     TEXT,
    reviewed_ts     INTEGER,
    sent_ts         INTEGER,
    send_error      TEXT,
    created_ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_draft_status ON drafts(status, created_ts DESC);

-- ============ 自动规则 ============
-- 决定什么样的邮件可以全自动回复，什么样的必须人工审核
CREATE TABLE IF NOT EXISTS rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    -- 匹配条件 JSON：{categories:[], min_score:null, max_score:null, keywords:[]}
    cond_json       TEXT    NOT NULL DEFAULT '{}',
    -- 动作：auto_reply 自动回 / draft_only 仅起草 / notify 只提醒 / drop 丢弃
    action          TEXT    NOT NULL DEFAULT 'draft_only',
    template_key    TEXT,
    priority        INTEGER NOT NULL DEFAULT 100,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_ts      INTEGER NOT NULL
);

-- ============ 回复模板 ============
CREATE TABLE IF NOT EXISTS templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    -- 适用类别：inquiry / quote / after_sale / complaint / spam / any
    category        TEXT    NOT NULL DEFAULT 'any',
    -- 语言：zh / en / auto
    lang            TEXT    NOT NULL DEFAULT 'en',
    subject         TEXT,
    body            TEXT    NOT NULL,
    -- 给 AI 的额外指示（比如"要体现我们支持 FOB 和 CIF"）
    ai_hint         TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_ts      INTEGER NOT NULL
);

-- ============ 客户标签 ============
CREATE TABLE IF NOT EXISTS tags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    color           TEXT    NOT NULL DEFAULT '#185FA5'
);
CREATE TABLE IF NOT EXISTS contact_tags (
    contact_id      INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    tag_id          INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    created_ts      INTEGER NOT NULL,
    PRIMARY KEY (contact_id, tag_id)
);

-- ============ 操作日志 ============
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    actor           TEXT,
    action          TEXT    NOT NULL,
    target_type     TEXT,
    target_id       INTEGER,
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);

-- ============ 大模型调用与 Token 测算日志 ============
CREATE TABLE IF NOT EXISTS ai_logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                INTEGER NOT NULL,
    model             TEXT NOT NULL,
    purpose           TEXT NOT NULL,         -- analysis (分类打分) / summary (中文摘要) / draft (起草回复) / profile (客户背调) / test (连通测试)
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0.0,
    cost_rmb          REAL NOT NULL DEFAULT 0.0,
    latency_ms        INTEGER,
    message_id        INTEGER,
    contact_id        INTEGER,
    status            TEXT NOT NULL DEFAULT 'ok', -- ok / error
    error_msg         TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_logs_ts ON ai_logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_ai_logs_purpose ON ai_logs(purpose, ts DESC);

-- ============ 系统设置（键值对） ============
CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_ts      INTEGER
);

-- ============ 每日统计快照 ============
-- 看板趋势图直接读这张表，避免每次全表扫描
CREATE TABLE IF NOT EXISTS daily_stats (
    day             TEXT PRIMARY KEY,   -- YYYY-MM-DD（本地时区）
    in_count        INTEGER NOT NULL DEFAULT 0,
    out_count       INTEGER NOT NULL DEFAULT 0,
    auto_count      INTEGER NOT NULL DEFAULT 0,
    high_intent     INTEGER NOT NULL DEFAULT 0,
    new_contacts    INTEGER NOT NULL DEFAULT 0,
    avg_score       REAL,
    updated_ts      INTEGER
);

-- ============ 货运代理名录 ============
CREATE TABLE IF NOT EXISTS forwarders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,           -- 货代名称/公司名（如：深圳快捷达国际物流）
    contact_person  TEXT,                       -- 联系人姓名（如：张经理 / Lisa）
    phone           TEXT,                       -- 联系电话 / 手机
    email           TEXT,                       -- 业务邮箱
    wechat_or_im    TEXT,                       -- 微信 / WhatsApp / QQ
    advantages      TEXT,                       -- 优势专线/特色服务（如：欧美超大件海卡、欧洲双清包税）
    rating          INTEGER NOT NULL DEFAULT 5, -- 合作评价（1-5星）
    address         TEXT,                       -- 仓库或办公地址
    note            TEXT,                       -- 合作备忘/注意事项
    created_ts      INTEGER NOT NULL,
    updated_ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forwarders_name ON forwarders(name);

-- ============ 货代报价明细与比价表 ============
CREATE TABLE IF NOT EXISTS freight_quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    forwarder_id    INTEGER NOT NULL REFERENCES forwarders(id) ON DELETE CASCADE,
    title           TEXT    NOT NULL,           -- 渠道/方案名称（如：美森快船超大件限时达、DHL大陆特惠）
    channel_type    TEXT    NOT NULL,           -- 运输类型：sea (海运) / air (空运) / express (国际快递) / rail (铁运) / truck (卡航)
    origin          TEXT    NOT NULL,           -- 起运地/港口（如：深圳/宁波/上海）
    destination     TEXT    NOT NULL,           -- 目的地/港口/国家（如：美国洛杉矶、德国汉堡、英国全境）
    unit_price      REAL    NOT NULL,           -- 基准单价
    price_unit      TEXT    NOT NULL,           -- 计价单位：kg (元/KG)、cbm (元/CBM)、20gp (元/小柜)、40hq (元/高柜)、flat (元/票)
    currency        TEXT    NOT NULL DEFAULT 'CNY', -- 币种：CNY / USD / EUR
    min_charge      TEXT,                       -- 起运标准/最低收费（如：21KG起、1CBM起）
    transit_time_text TEXT,                     -- 预估时效（如：12-15天、3-5工作日）
    valid_until     TEXT,                       -- 报价有效期（YYYY-MM-DD）
    extra_fees      TEXT,                       -- 杂费/报关/包税说明（如：报关费350元/票，超长费另计）
    remarks         TEXT,                       -- 备注
    created_ts      INTEGER NOT NULL,
    updated_ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_freight_quotes_fwd     ON freight_quotes(forwarder_id);
CREATE INDEX IF NOT EXISTS idx_freight_quotes_dest    ON freight_quotes(destination);
CREATE INDEX IF NOT EXISTS idx_freight_quotes_channel ON freight_quotes(channel_type);

-- ============ 外贸订单全生命周期流转 ============
CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no            TEXT    NOT NULL UNIQUE,    -- 形式发票号/订单号（如：PI-20260919-001）
    title               TEXT    NOT NULL,           -- 项目名称/货品概要
    contact_id          INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    forwarder_quote_id  INTEGER REFERENCES freight_quotes(id) ON DELETE SET NULL, -- 关联货代渠道
    stage               TEXT    NOT NULL DEFAULT 'draft', -- 8大流转阶段
    trade_term          TEXT    NOT NULL DEFAULT 'FOB',   -- 贸易术语：FOB, CIF, CFR, DDP, DAP, EXW
    payment_term        TEXT    DEFAULT '30% T/T Deposit, 70% Balance before shipment',
    currency            TEXT    NOT NULL DEFAULT 'USD',   -- 结算币种：USD, EUR, CNY, GBP
    total_amount        REAL    NOT NULL DEFAULT 0.0,     -- 订单总额
    deposit_amount      REAL    NOT NULL DEFAULT 0.0,     -- 已收定金
    balance_amount      REAL    NOT NULL DEFAULT 0.0,     -- 已收尾款
    settlement_fx_rate  REAL    NOT NULL DEFAULT 7.20,    -- 结算参考汇率
    factory_cost        REAL    NOT NULL DEFAULT 0.0,     -- 工厂生产/采购成本(CNY)
    shipping_cost       REAL    NOT NULL DEFAULT 0.0,     -- 货代运费支出(CNY)
    other_cost          REAL    NOT NULL DEFAULT 0.0,     -- 报关/内陆运费/港杂费(CNY)
    est_delivery_date   TEXT,                             -- 预计交货期 ETD (YYYY-MM-DD)
    actual_delivery_date TEXT,                            -- 实际出运日期 (YYYY-MM-DD)
    tracking_or_bl_no   TEXT,                             -- 提单号 / 快递单号 (B/L No.)
    destination_port    TEXT,                             -- 目的港 / 目的国
    loading_port        TEXT    DEFAULT 'Shenzhen, China',-- 装运港 (Port of Loading)
    carrier             TEXT,                             -- 承运人 / 船名航次 / 快递公司
    shipping_marks      TEXT    DEFAULT 'N/M',            -- 外贸唛头 (Shipping Marks)
    pi_date             TEXT,                             -- 形式发票日期 (YYYY-MM-DD)
    ci_date             TEXT,                             -- 商业发票日期 (YYYY-MM-DD)
    note                TEXT,                             -- 订单详细要求/生产注意事项
    created_ts          INTEGER NOT NULL,
    updated_ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_contact ON orders(contact_id);
CREATE INDEX IF NOT EXISTS idx_orders_stage   ON orders(stage);
CREATE INDEX IF NOT EXISTS idx_orders_etd     ON orders(est_delivery_date);

-- ============ 订单流转时间轴流水 ============
CREATE TABLE IF NOT EXISTS order_timeline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    from_stage  TEXT,                               -- 变更前阶段
    to_stage    TEXT    NOT NULL,                   -- 变更后阶段
    action_name TEXT    NOT NULL,                   -- 节点动作
    operator    TEXT    NOT NULL DEFAULT 'admin',   -- 操作人
    detail      TEXT,                               -- 流水备注/水单/单号
    created_ts  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_timeline_oid ON order_timeline(order_id, created_ts);

-- ============ 外贸基础产品库 ============
CREATE TABLE IF NOT EXISTS products (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sku                 TEXT    NOT NULL UNIQUE,          -- 产品型号/货号/编码 (如 A1200-PRO)
    name_en             TEXT    NOT NULL,                 -- 英文品名 (如 High Speed Brushless Motor)
    name_cn             TEXT,                             -- 中文品名 (如 高速无刷电机)
    hs_code             TEXT,                             -- 海关HS编码 (如 8501310000)
    category            TEXT    DEFAULT 'default',        -- 产品分类
    specs               TEXT,                             -- 规格参数/技术描述/材质
    unit                TEXT    NOT NULL DEFAULT 'PCS',   -- 计量单位 (PCS, SETS, PAIRS, CTNS, M, KG)
    price_usd           REAL    NOT NULL DEFAULT 0.0,     -- 参考外币售价 USD
    cost_cny            REAL    NOT NULL DEFAULT 0.0,     -- 参考成本/出厂采购价 CNY
    moq                 INTEGER NOT NULL DEFAULT 1,       -- 最小起订量 (MOQ)
    carton_qty          INTEGER NOT NULL DEFAULT 1,       -- 装箱率/每箱数量 (PCS/CTN)
    carton_length_cm    REAL    NOT NULL DEFAULT 0.0,     -- 外箱长 (cm)
    carton_width_cm     REAL    NOT NULL DEFAULT 0.0,     -- 外箱宽 (cm)
    carton_height_cm    REAL    NOT NULL DEFAULT 0.0,     -- 外箱高 (cm)
    carton_cbm          REAL    NOT NULL DEFAULT 0.0,     -- 单箱体积 (CBM)
    carton_gw_kg        REAL    NOT NULL DEFAULT 0.0,     -- 单箱毛重 (GW KG)
    carton_nw_kg        REAL    NOT NULL DEFAULT 0.0,     -- 单箱净重 (NW KG)
    image_url           TEXT,                             -- 产品图片URL或Base64缩略图
    note                TEXT,                             -- 内部备忘
    is_active           INTEGER NOT NULL DEFAULT 1,       -- 状态：1 在售 / 0 归档
    created_ts          INTEGER NOT NULL,
    updated_ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_sku  ON products(sku);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name_en);

-- ============ 订单产品明细品项 ============
CREATE TABLE IF NOT EXISTS order_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id          INTEGER REFERENCES products(id) ON DELETE SET NULL,
    item_no             INTEGER NOT NULL DEFAULT 1,       -- 项号序号 (1, 2, 3...)
    sku                 TEXT,                             -- 型号/货号
    name_en             TEXT    NOT NULL,                 -- 英文品名
    name_cn             TEXT,                             -- 中文品名
    hs_code             TEXT,                             -- HS编码
    specs               TEXT,                             -- 规格型号/描述
    unit                TEXT    NOT NULL DEFAULT 'PCS',   -- 计量单位
    quantity            REAL    NOT NULL DEFAULT 1.0,     -- 订购数量
    unit_price          REAL    NOT NULL DEFAULT 0.0,     -- 外币单价
    amount              REAL    NOT NULL DEFAULT 0.0,     -- 总金额 = quantity * unit_price
    unit_cost_cny       REAL    NOT NULL DEFAULT 0.0,     -- 出厂采购单价 CNY
    cartons             INTEGER NOT NULL DEFAULT 0,       -- 箱数 (CTNS)
    net_weight_kg       REAL    NOT NULL DEFAULT 0.0,     -- 净重总计 (NW KG)
    gross_weight_kg     REAL    NOT NULL DEFAULT 0.0,     -- 毛重总计 (GW KG)
    cbm                 REAL    NOT NULL DEFAULT 0.0,     -- 体积总计 (CBM)
    created_ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_items_oid ON order_items(order_id, item_no);

-- ============ 社媒外贸开发与获客线索 ============
CREATE TABLE IF NOT EXISTS social_leads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL,                 -- 潜客联系人姓名 (如 Michael Schmidt)
    company             TEXT,                             -- 公司名称 (如 Schmidt Industrial GmbH)
    country             TEXT,                             -- 国家/地区 (如 Germany / 美国 / 迪拜)
    position            TEXT,                             -- 职位/头衔 (如 Purchasing Manager, Sourcing Specialist, CEO)
    source_platform     TEXT    NOT NULL DEFAULT 'linkedin', -- 平台：linkedin, whatsapp, facebook, instagram, tiktok, google, exhibition, other
    whatsapp            TEXT,                             -- WhatsApp / 手机号 (纯数字国际区号)
    linkedin_url        TEXT,                             -- LinkedIn 个人主页或公司主页 URL
    social_handle       TEXT,                             -- 社媒账号/用户名/主页
    email               TEXT,                             -- 电子邮箱 (初期可为空)
    website             TEXT,                             -- 公司官网
    industry_or_niche   TEXT,                             -- 主营行业/产品类目 (如 Hardware Tools, Electric Motors)
    stage               TEXT    NOT NULL DEFAULT 'discovered', -- 阶段：discovered / connected / chatted / catalog_sent / converted / unqualified
    rating              INTEGER NOT NULL DEFAULT 3,       -- 意向星级 (1-5星)
    next_followup_date  TEXT,                             -- 下次跟进提醒日期 (YYYY-MM-DD)
    notes               TEXT,                             -- 需求描述/痛点/跟进备忘
    contact_id          INTEGER REFERENCES contacts(id) ON DELETE SET NULL, -- 转为正式客户后外键
    created_ts          INTEGER NOT NULL,
    updated_ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_social_leads_stage    ON social_leads(stage);
CREATE INDEX IF NOT EXISTS idx_social_leads_platform ON social_leads(source_platform);
CREATE INDEX IF NOT EXISTS idx_social_leads_followup ON social_leads(next_followup_date);

-- ============ 社媒触达与跟进流水记录 ============
CREATE TABLE IF NOT EXISTS social_touchpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL REFERENCES social_leads(id) ON DELETE CASCADE,
    channel     TEXT    NOT NULL,                         -- 触达渠道：whatsapp, linkedin, facebook, instagram, call, other
    touch_type  TEXT    NOT NULL,                         -- 动作：first_touch (首次破冰), catalog_pitch (推介图册), sample_followup (索样跟进), quote_followup (报价追踪), holiday_greeting (节日问候), call (电话沟通), note (内部备忘)
    content     TEXT    NOT NULL,                         -- 发送内容摘要或沟通记录
    feedback    TEXT,                                     -- 买家反馈：no_reply (已发未回), replied (已回复探讨), asked_catalog (索要目录), asked_quote (索要报价), rejected (暂无需求/拒绝)
    next_action TEXT,                                     -- 下一步待办
    operator    TEXT    NOT NULL DEFAULT 'admin',         -- 业务员
    created_ts  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_social_touchpoints_lead ON social_touchpoints(lead_id, created_ts);

-- ============ 外贸单证与凭证管理 (OCR 识别与电子归档) ============
CREATE TABLE IF NOT EXISTS vouchers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no      TEXT    NOT NULL,                         -- 单据/凭证编号（如 INV-20260901, BL-MSK88992）
    voucher_type    TEXT    NOT NULL DEFAULT 'commercial_invoice', -- 凭证类型：commercial_invoice, proforma_invoice, bill_of_lading, packing_list, bank_slip, customs_declaration, contract, other
    title           TEXT,                                     -- 凭证标题/简述
    trade_date      TEXT,                                     -- 业务发生日期 (YYYY-MM-DD)
    currency        TEXT    NOT NULL DEFAULT 'USD',           -- 币种 (USD, EUR, CNY, GBP等)
    amount          REAL    NOT NULL DEFAULT 0.0,             -- 凭证金额
    contact_id      INTEGER REFERENCES contacts(id) ON DELETE SET NULL, -- 关联客户
    order_id        INTEGER REFERENCES orders(id) ON DELETE SET NULL,   -- 关联订单
    shipper         TEXT,                                     -- 发货人/卖方公司
    consignee       TEXT,                                     -- 收货人/买方公司
    product_desc    TEXT,                                     -- 品名与货品描述
    file_path       TEXT,                                     -- 凭证原件扫描件/文件路径
    ocr_status      TEXT    NOT NULL DEFAULT 'pending',       -- OCR状态：success, failed, manual, pending
    ocr_raw_text    TEXT,                                     -- 本地 OCR 识别原始文本
    status          TEXT    NOT NULL DEFAULT 'confirmed',     -- 状态：confirmed (已确认), pending (待核对), archived (已归档)
    notes           TEXT,                                     -- 备忘与备注说明
    created_ts      INTEGER NOT NULL,
    updated_ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vouchers_no      ON vouchers(voucher_no);
CREATE INDEX IF NOT EXISTS idx_vouchers_type    ON vouchers(voucher_type);
CREATE INDEX IF NOT EXISTS idx_vouchers_date    ON vouchers(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_vouchers_contact ON vouchers(contact_id);
CREATE INDEX IF NOT EXISTS idx_vouchers_order   ON vouchers(order_id);
CREATE INDEX IF NOT EXISTS idx_vouchers_status  ON vouchers(status);
