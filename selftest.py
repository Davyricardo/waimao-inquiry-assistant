"""本地自检：不连真实邮箱，用模拟数据跑通全链路。

验证：
1. 数据库建表与预置数据
2. 凭据加解密往返
3. 密码哈希与校验
4. 邮件解析（构造真实 MIME）
5. 规则分析打分
6. 规则引擎决策
7. 入库与会话归并
8. 看板查询
9. 模板渲染
"""
import email.message
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# 用临时目录与临时密钥，避免污染真实数据
TMP = Path(tempfile.mkdtemp(prefix="mb_test_"))
os.environ["MB_DATA_DIR"] = str(TMP / "data")
os.environ["MB_DB"] = str(TMP / "data" / "test.db")
os.environ["MB_CRED_KEY"] = "test-key-not-for-production"
os.environ["MB_SECRET_KEY"] = "test-session-key"
os.environ["MB_AI_FALLBACK"] = "1"
os.environ.pop("MB_AI_API_KEY", None)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ OK ] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


print("=" * 66)
print("1. 数据库初始化")
print("=" * 66)
from app import db
db.init_db()
check("建库成功", Path(os.environ["MB_DB"]).exists())
with db.ro() as c:
    n_tpl = c.execute("SELECT COUNT(*) n FROM templates").fetchone()["n"]
    n_rule = c.execute("SELECT COUNT(*) n FROM rules").fetchone()["n"]
check(f"预置模板 {n_tpl} 条", n_tpl >= 5)
check(f"预置规则 {n_rule} 条", n_rule >= 4)

print()
print("=" * 66)
print("2. 凭据加解密")
print("=" * 66)
from app import crypto_util
secret = "abcd1234efgh5678"
enc = crypto_util.encrypt_secret(secret)
check("加密产生密文", enc != secret and ":" in enc)
check("解密还原一致", crypto_util.decrypt_secret(enc) == secret)
enc2 = crypto_util.encrypt_secret(secret)
check("IV 随机（两次密文不同）", enc != enc2)

print()
print("=" * 66)
print("3. 密码哈希")
print("=" * 66)
h = crypto_util.hash_password("MyTestPass123")
check("哈希格式正确", h.startswith("pbkdf2_sha256$600000$"))
check("正确密码通过", crypto_util.verify_password("MyTestPass123", h))
check("错误密码拒绝", not crypto_util.verify_password("wrong", h))
check("空密码拒绝", not crypto_util.verify_password("", h))

print()
print("=" * 66)
print("4. 邮件解析")
print("=" * 66)
from app import mail_engine

# 日期必须动态生成 —— 写死某一天的话，过了那天「今日询盘」断言就会假失败。
from email.utils import format_datetime
import datetime as _dt

def _today_hdr(hour: int = 14, minute: int = 30, days_ago: int = 0) -> str:
    """按本地时区构造邮件 Date 头，保证测试永远落在「当天」。"""
    off = int(os.environ.get("MB_TZ_OFFSET", "8"))
    tz = _dt.timezone(_dt.timedelta(hours=off))
    d = _dt.datetime.now(tz) - _dt.timedelta(days=days_ago)
    d = d.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return format_datetime(d)

raw = email.message.EmailMessage()
raw["From"] = "Klaus Mueller <k.mueller@mueller-handel.de>"
raw["To"] = "sales@mycompany.com"
raw["Subject"] = "Re: Inquiry about model A-1200"
raw["Message-ID"] = "<abc123@mueller-handel.de>"
raw["In-Reply-To"] = "<parent999@mycompany.com>"
raw["References"] = "<parent999@mycompany.com>"
raw["Date"] = _today_hdr(14, 30)
raw.set_content(
    "Dear Sales team,\n\n"
    "We are interested in purchasing 5,000 pcs of model A-1200.\n"
    "Please quote us your best FOB Shenzhen price and MOQ.\n"
    "We are a distributor in Germany and this could become an annual order.\n\n"
    "Best regards,\nKlaus Mueller\nMueller Handel GmbH\nwww.mueller-handel.de\n\n"
    "On Wed, 17 Sep 2026, sales@mycompany.com wrote:\n> Our catalog is attached.\n"
)

fake_account = {
    "id": 1, "email": "sales@mycompany.com", "watch_folder": "INBOX",
    "display_name": "Sales", "imap_host": "imap.qq.com", "imap_port": 993,
    "smtp_host": "smtp.qq.com", "smtp_port": 465,
}
parsed = mail_engine.parse_raw_mail(raw.as_bytes(), fake_account, "42")
check("发件邮箱解析", parsed["from_addr"] == "k.mueller@mueller-handel.de")
check("发件人姓名解析", parsed["from_name"] == "Klaus Mueller")
check("主题解析", parsed["subject"] == "Re: Inquiry about model A-1200")
check("Message-ID 捕获", parsed["msgid"] == "<abc123@mueller-handel.de>")
check("In-Reply-To 捕获", parsed["in_reply_to"] == "<parent999@mycompany.com>")
check("引用部分被截断", "catalog is attached" not in parsed["body_text"])
check("正文保留关键内容", "5,000 pcs" in parsed["body_text"])
check("指纹以 mid: 开头", parsed["fingerprint"].startswith("mid:"))

print()
print("=" * 66)
print("5. 中文与编码还原")
print("=" * 66)
raw2 = email.message.EmailMessage()
raw2["From"] = "=?utf-8?b?5byg5LiJ?= <zhangsan@example.com>"
raw2["Subject"] = "=?utf-8?b?5rWL6K+V5Li76aKY?="
raw2["Date"] = _today_hdr(10, 0, days_ago=1)
raw2.set_content("中文正文测试")
p2 = mail_engine.parse_raw_mail(raw2.as_bytes(), fake_account, "43")
check("中文姓名还原", p2["from_name"] == "张三", f"得到: {p2['from_name']}")
check("中文主题还原", p2["subject"] == "测试主题", f"得到: {p2['subject']}")
check("中文正文还原", "中文正文测试" in p2["body_text"])

print()
print("=" * 66)
print("6. 规则分析打分（无模型降级模式）")
print("=" * 66)
from app import ai_agent

a1 = ai_agent.rule_analyze(parsed["subject"], parsed["body_text"],
                           parsed["from_addr"])
print(f"  高价值邮件 → 类别={a1['category']} 分数={a1['score']}")
check("识别为询盘或报价", a1["category"] in ("inquiry", "quote"))
check("高意向分数 >= 70", a1["score"] >= 70, f"实际 {a1['score']}")
print(f"  依据: {a1['rationale']}")

spam_body = "Dear sir/madam, we offer guaranteed SEO traffic and bitcoin investment opportunities. God bless"
a2 = ai_agent.rule_analyze("SEO service", spam_body, "spam@cheap-seo.xyz")
print(f"  垃圾邮件 → 类别={a2['category']} 分数={a2['score']}")
check("垃圾邮件分数较低", a2["score"] < 50, f"实际 {a2['score']}")

vague = "please send me your catalog"
a3 = ai_agent.rule_analyze("catalog", vague, "someone@gmail.com")
print(f"  泛泛索要目录 → 类别={a3['category']} 分数={a3['score']}")
check("泛泛询盘分数不高", a3["score"] < 60, f"实际 {a3['score']}")

print()
print("=" * 66)
print("7. 规则引擎决策")
print("=" * 66)
from app import rules_engine
d1 = rules_engine.decide(a1)
print(f"  高意向询盘 → 动作={d1['action']} 规则={d1['rule']['name']}")
check("命中规则且为起草待审", d1["action"] in ("draft_only", "notify"))
d2 = rules_engine.decide(a2)
print(f"  垃圾邮件 → 动作={d2['action']} 规则={d2['rule']['name']}")
check("垃圾邮件被丢弃", d2["action"] == "drop")
complaint = {"category": "complaint", "score": 40}
d3 = rules_engine.decide(complaint)
print(f"  投诉 → 动作={d3['action']}")
check("投诉仅提醒不起草", d3["action"] == "notify")

print()
print("=" * 66)
print("8. 入库与会话归并")
print("=" * 66)
from app import pipeline

with db.tx() as c:
    c.execute("INSERT INTO accounts (email,display_name,secret_enc,imap_host,"
              "imap_port,smtp_host,smtp_port,watch_folder,is_active,created_ts) "
              "VALUES (?,?,?,?,?,?,?,?,1,?)",
              ("sales@mycompany.com", "Sales",
               crypto_util.encrypt_secret("dummy"), "imap.qq.com", 993,
               "smtp.qq.com", 465, "INBOX", db.now_ts()))

mid1 = mail_engine.ingest(parsed, 1)
check("首封邮件入库", mid1 is not None)
mid_dup = mail_engine.ingest(parsed, 1)
check("重复邮件被去重", mid_dup is None)

# 第二封同线索邮件：靠 In-Reply-To 归并
raw3 = email.message.EmailMessage()
raw3["From"] = "Klaus Mueller <k.mueller@mueller-handel.de>"
raw3["Subject"] = "Re: Re: Inquiry about model A-1200"
raw3["Message-ID"] = "<reply456@mueller-handel.de>"
raw3["In-Reply-To"] = "<abc123@mueller-handel.de>"
raw3["Date"] = _today_hdr(16, 0)
raw3.set_content("Thanks. What is your lead time for 10,000 pcs?")
p3 = mail_engine.parse_raw_mail(raw3.as_bytes(), fake_account, "44")
mid2 = mail_engine.ingest(p3, 1)
check("第二封邮件入库", mid2 is not None)

with db.ro() as c:
    t1 = c.execute("SELECT thread_id FROM messages WHERE id=?", (mid1,)).fetchone()
    t2 = c.execute("SELECT thread_id FROM messages WHERE id=?", (mid2,)).fetchone()
check("两封邮件归并到同一线索", t1["thread_id"] == t2["thread_id"],
      f"{t1['thread_id']} vs {t2['thread_id']}")

with db.ro() as c:
    n_msg = c.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
    n_ct = c.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
check(f"邮件表 {n_msg} 条", n_msg == 2)
check(f"客户表 {n_ct} 条（同一客户不重复）", n_ct == 1)

print()
print("=" * 66)
print("9. 分析与起草流水线")
print("=" * 66)
pipeline._analyze_and_draft(mid1)
with db.ro() as c:
    m = c.execute("SELECT * FROM messages WHERE id=?", (mid1,)).fetchone()
    n_draft = c.execute("SELECT COUNT(*) n FROM drafts").fetchone()["n"]
print(f"  分析结果：类别={m['category']} 分数={m['score']} 状态={m['status']}")
check("邮件已标记分析完成", m["status"] != "new", f"状态 {m['status']}")
check("生成了回复草稿", n_draft >= 1, f"草稿数 {n_draft}")

with db.ro() as c:
    d = c.execute("SELECT * FROM drafts ORDER BY id DESC LIMIT 1").fetchone()
print(f"  草稿主题：{d['subject']}")
print(f"  草稿正文前 120 字：{d['body_text'][:120]}...")
check("草稿含收件人称呼", "Mueller" in d["body_text"] or "there" in d["body_text"])

print()
print("=" * 66)
print("10. 看板查询与统计")
print("=" * 66)
from app import queries
pipeline.refresh_daily_stats()
dash = queries.dashboard()
print(f"  今日询盘={dash['today_in']} 高意向={dash['high_intent']} "
      f"待审={dash['pending_review']} 客户={dash['total_contacts']}")
check("今日询盘计数正确", dash["today_in"] == 2, f"实际 {dash['today_in']}")
check("待审草稿计数正确", dash["pending_review"] >= 1)
check("客户总数正确", dash["total_contacts"] == 1)

tr = queries.trend(7)
check(f"趋势数据 {len(tr['labels'])} 天", len(tr["labels"]) == 7)
check("趋势含今日数据", tr["in_count"][-1] >= 1)

leads = queries.top_leads(5)
check("高意向列表有数据", len(leads) >= 1)
if leads:
    print(f"  首位：{leads[0]['name'] or leads[0]['email']} 分数={leads[0]['score']}")

cats = queries.category_breakdown(30)
check("分类分布有数据", len(cats) >= 1)

msg = queries.get_message(mid1)
check("邮件详情可查", msg is not None and len(msg["thread"]) == 2)
check("详情含草稿", len(msg["drafts"]) >= 1)

cont = queries.get_contact(1)
check("客户详情可查", cont is not None and len(cont["recent"]) == 2)

print()
print("=" * 66)
print("11. 模板渲染")
print("=" * 66)
from app.templating import render, _eval_expr, _eval_cond

html_out = render("dashboard.html", page="dashboard",
                  d=dash, tr={**tr, "labels_json": "[]", "in_json": "[]",
                              "high_json": "[]"},
                  cats=cats, leads=leads, csrf="tok", high=70,
                  ok="", err="")
check("看板渲染成功", len(html_out) > 2000)
check("看板含导航", "外贸询盘助手" in html_out and "数据看板" in html_out)
check("看板含指标卡", "今日询盘" in html_out)

ctx = {"a": {"b": [1, 2, 3]}, "x": 5, "s": "hi"}
check("路径取值 a.b", _eval_expr("a.b", ctx) == [1, 2, 3])
check("条件 x==5", _eval_cond("x == 5", ctx))
check("条件 x>10", not _eval_cond("x > 10", ctx))
check("条件组合", _eval_cond("x == 5 and s == 'hi'", ctx))
check("否定条件", _eval_cond("not x == 99", ctx))

login_out = render("login.html", err="")
check("登录页渲染", "登录" in login_out and "csrf" not in login_out.lower())

# XSS 防护
xss_ctx = {"evil": "<script>alert(1)</script>"}
xss_out = render("error.html", msg=xss_ctx["evil"])
check("模板转义生效", "<script>" not in xss_out and "&lt;script&gt;" in xss_out)

print()
print("=" * 66)
print("12. 邮件自动化外发安全性（默认必须关闭）")
print("=" * 66)
with db.ro() as c:
    auto = c.execute("SELECT value FROM settings WHERE key='auto_send'").fetchone()
check("默认关闭自动发送", auto["value"] == "0", f"实际 {auto['value']}")

from app import config as cfg
check("配置默认不允许自动发送", cfg.AUTO_SEND is False)

auto_decision = rules_engine.decide({"category": "inquiry", "score": 95})
check("高意向邮件的自动回复被降级为待审",
      auto_decision["action"] == "draft_only",
      f"实际 {auto_decision['action']}")

print()
print("=" * 66)
print(f"结果：通过 {PASS} · 失败 {FAIL}")
print("=" * 66)
if FAIL:
    print("存在失败项，需修复后再部署。")
    sys.exit(1)
print("全部通过，可以部署。")
sys.exit(0)
