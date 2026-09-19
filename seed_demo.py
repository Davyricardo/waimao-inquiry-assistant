"""灌模拟数据，用于查看后台界面效果。仅本地演示用，不要在生产跑。"""
import email.message
import os
import random
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

env = {}
for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
os.environ.update(env)

from app import crypto_util, db, mail_engine, pipeline, queries

db.init_db()

# 建账户
with db.ro() as c:
    has = c.execute("SELECT COUNT(*) n FROM accounts").fetchone()["n"]
if not has:
    with db.tx() as c:
        # 演示占位账户：is_active=0 —— 只用来挂演示数据，不参与真实收信，
        # 避免 worker 拿着假密码反复连接真实邮件服务器并刷错误。
        c.execute(
            "INSERT INTO accounts (email,display_name,secret_enc,imap_host,"
            "imap_port,smtp_host,smtp_port,watch_folder,is_active,created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,0,?)",
            ("davy@example.com", "Davy",
             crypto_util.encrypt_secret("demo-not-real"), "imap.qq.com", 993,
             "smtp.qq.com", 465, "INBOX", db.now_ts()))

ACC = {"id": 1, "email": "davy@example.com", "watch_folder": "INBOX"}

SAMPLES = [
    ("Klaus Mueller", "k.mueller@mueller-handel.de", "Mueller Handel GmbH", "德国",
     "Inquiry: 5000 pcs model A-1200, need FOB Shenzhen quote",
     "Dear Sales team,\n\nWe are a distributor of industrial components in Germany.\n"
     "We would like to purchase 5,000 pcs of model A-1200 based on the specifications "
     "on your website. Our target price is around USD 12.50 per unit.\n\n"
     "Please quote us FOB Shenzhen with your best price and MOQ. Also please advise "
     "lead time and payment terms for an annual order of roughly 60,000 pcs.\n\n"
     "We require CE certification for our market.\n\nBest regards,\nKlaus Mueller\n"
     "Mueller Handel GmbH\nwww.mueller-handel.de"),
    ("John Doe", "john@johndoetrading.com", "John Doe Trading LLC", "美国",
     "Ask for MOQ and payment terms",
     "Hi,\n\nWe saw your listing. Can you tell us your MOQ and payment terms?\n"
     "Our competitor is offering similar at a lower price point. Need your "
     "best price urgently as we have a tender closing next week.\n\nJohn"),
    ("Rajesh Kumar", "rajesh.supplies@gmail.com", "", "印度",
     "Please send catalog",
     "please send me your catalog"),
    ("Sophie Laurent", "s.laurent@euromarket.fr", "EuroMarket SARL", "法国",
     "Sample request for trial order - models A-1200 and B-400",
     "Bonjour,\n\nWe operate 40 retail stores across France. We would like to "
     "receive samples of model A-1200 and B-400 for evaluation.\n\n"
     "If the quality meets our standard, we plan a trial order of 2,000 pcs, "
     "followed by monthly replenishment. Please advise sample cost and "
     "shipping to Paris.\n\nOur website: www.euromarket.fr\n\nCordialement,\n"
     "Sophie Laurent"),
    ("Ahmed Ali", "ahmed@gulfimports.ae", "Gulf Imports FZE", "阿联酋",
     "CIF Jebel Ali price for 1x40HQ container",
     "Dear Sir,\n\nPlease provide your CIF Jebel Ali price for one 40HQ container "
     "of model A-1200. We import regularly, roughly 4 containers per quarter.\n\n"
     "We will pay by T/T 30% deposit. Please confirm you accept this.\n\nAhmed"),
    ("Peter Novak", "novak@czparts.cz", "CZ Parts s.r.o.", "捷克",
     "Problem with last shipment - defective units",
     "Hello,\n\nFrom our last order (#20260812) we found 37 units not working "
     "properly. This is causing problems with our own customers.\n\n"
     "We need a solution - replacement or refund. Please respond quickly.\n\nPeter"),
    ("Unknown Sender", "no-reply@cheap-seo.xyz", "", "",
     "Guaranteed SEO traffic for your website",
     "Dear sir/madam,\n\nWe can guarantee first page ranking for your website. "
     "Also we have bitcoin investment opportunities. God bless you.\n\n"
     "Reply to unsubscribe to stop."),
    ("Maria Santos", "maria@santosdistribuidora.br", "Santos Distribuidora", "巴西",
     "Interested in becoming your distributor in Brazil",
     "Prezados,\n\nOur company has 15 years of experience distributing building "
     "materials in Brazil. We are looking for a supplier to become our exclusive "
     "partner for the Brazilian market.\n\nWe currently import about 8 containers "
     "per year from other countries. Could you send us your full catalog and "
     "distributor terms?\n\nMaria Santos"),
    ("Thomas Weber", "t.weber@weber-industrie.de", "Weber Industrie AG", "德国",
     "Urgent: need 2000 units within 3 weeks",
     "Dear team,\n\nOur current supplier let us down. We urgently need 2,000 pcs "
     "of model B-400 within 3 weeks. Can you deliver in this timeframe?\n\n"
     "Please quote EXW and tell us your fastest production time.\n\nThomas"),
    ("Liam O'Brien", "liam@irishwholesale.ie", "Irish Wholesale Ltd", "爱尔兰",
     "Price list request",
     "Hello,\n\nCould you send your latest price list? We may have interest "
     "in several items for the Irish market.\n\nLiam"),
]


def build(from_name, from_addr, subject, body, day_offset):
    m = email.message.EmailMessage()
    m["From"] = f"{from_name} <{from_addr}>"
    m["To"] = "davy@example.com"
    m["Subject"] = subject
    m["Message-ID"] = f"<{random.randint(10**6, 10**7)}@demo>"
    import time as _t
    ts = _t.time() - day_offset * 86400
    m["Date"] = email.utils.formatdate(ts, localtime=False, usegmt=True)
    m.set_content(body)
    return m


import email.utils
random.seed(42)

n_ok = 0
for i, (nm, ea, co, country, subj, body) in enumerate(SAMPLES):
    day = i % 6
    raw = build(nm, ea, subj, body, day)
    p = mail_engine.parse_raw_mail(raw.as_bytes(), ACC, str(1000 + i))
    mid = mail_engine.ingest(p, 1)
    if mid is None:
        continue
    # 补客户信息
    with db.tx() as c:
        c.execute("UPDATE contacts SET company=?,country=? WHERE email=? AND "
                  "(company IS NULL OR company='')", (co or None, country or None, ea))
    pipeline._analyze_and_draft(mid)
    n_ok += 1
    with db.ro() as c:
        r = c.execute("SELECT category,score FROM messages WHERE id=?",
                      (mid,)).fetchone()
    print(f"  {nm:16s} {r['category']:10s} score={r['score']:3d}  {subj[:46]}")

# 回填近几天的每日统计，让趋势图好看
import time as _t
for d in range(6):
    day_str = _t.strftime("%Y-%m-%d", _t.gmtime(_t.time() + 8 * 3600 - d * 86400))
    with db.tx() as c:
        base = random.randint(6, 14)
        c.execute(
            "INSERT INTO daily_stats (day,in_count,out_count,auto_count,"
            "high_intent,new_contacts,avg_score,updated_ts) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET in_count=in_count+?",
            (day_str, base, base - 2, 0, random.randint(1, 4),
             random.randint(1, 3), random.uniform(45, 68), db.now_ts(), 0))

pipeline.refresh_daily_stats()
with db.ro() as c:
    n_msg = c.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
    n_dr = c.execute("SELECT COUNT(*) n FROM drafts WHERE status='pending'").fetchone()["n"]
print(f"\n灌入 {n_ok} 封邮件，库内共 {n_msg} 条，待审草稿 {n_dr} 条")
