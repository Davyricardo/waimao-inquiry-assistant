"""邮件引擎：IMAP 拉取 + MIME 解析 + 会话归并 + 入库。

要点：
- 用 UID 而非序号，避免邮箱变动导致错位
- 去重指纹优先 Message-ID，缺失时用内容 sha256
- 只处理未读/新增邮件，已处理的记在 fingerprint 里
- 会话归并：靠 In-Reply-To / References 找父消息，找不到就用规范化主题 + 发件人
"""
import email
import email.header
import email.utils
import hashlib
import imaplib
import json
import re
import ssl
import time
from email.message import Message

from . import config, crypto_util, db

REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd|答复|转发|回复)\s*(\[\d+\])?\s*[:：]\s*)+",
                          re.IGNORECASE)


# ============ 工具函数 ============

def decode_header_val(raw) -> str:
    """还原 MIME 编码的头部（=?utf-8?B?...?= 之类）。"""
    if not raw:
        return ""
    parts = []
    for text, enc in email.header.decode_header(raw):
        if isinstance(text, bytes):
            for candidate in (enc, "utf-8", "gb18030", "latin-1"):
                if not candidate:
                    continue
                try:
                    parts.append(text.decode(candidate))
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                parts.append(text.decode("utf-8", errors="replace"))
        else:
            parts.append(text)
    return "".join(parts).strip()


def normalize_subject(subject: str) -> str:
    """去掉 Re:/Fwd: 前缀，用于线索归并。"""
    s = subject or ""
    prev = None
    while prev != s:
        prev = s
        s = REPLY_PREFIX.sub("", s)
    return s.strip()


def parse_addr(raw: str):
    """解析发件人，返回 (邮箱, 显示名)。"""
    name, addr = email.utils.parseaddr(raw or "")
    return (addr or "").strip().lower(), decode_header_val(name)


def html_to_text(html: str) -> str:
    """粗暴但可靠的 HTML 转文本——不引入 bs4 依赖。"""
    if not html:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'"))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def extract_body(msg: Message):
    """提取正文，返回 (纯文本, HTML)。优先 text/plain。"""
    text_parts, html_parts, attachments = [], [], []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                fn = decode_header_val(part.get_filename())
                payload = part.get_payload(decode=True) or b""
                attachments.append({
                    "filename": fn, "size": len(payload), "mime": ctype,
                })
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True) or b""
                text_parts.append(payload.decode(
                    part.get_content_charset() or "utf-8", errors="replace"))
            elif ctype == "text/html":
                payload = part.get_payload(decode=True) or b""
                html_parts.append(payload.decode(
                    part.get_content_charset() or "utf-8", errors="replace"))
    else:
        ctype = msg.get_content_type()
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        if ctype == "text/html":
            html_parts.append(payload.decode(charset, errors="replace"))
        else:
            text_parts.append(payload.decode(charset, errors="replace"))

    text = "\n".join(t for t in text_parts if t.strip())
    html = "\n".join(h for h in html_parts if h.strip())
    if not text.strip() and html.strip():
        text = html_to_text(html)
    # 引用历史内容截断：只保留最新一条，避免把整串历史喂给模型
    text = cut_quoted(text)
    return text, html, attachments


def cut_quoted(text: str) -> str:
    """截掉引用部分，只留本次新写的内容。"""
    if not text:
        return ""
    lines = text.splitlines()
    out = []
    for ln in lines:
        s = ln.strip()
        # 常见的引用起始标志
        if re.match(r"^-{2,}\s*(original message|forwarded message|原始邮件)", s, re.I):
            break
        if re.match(r"^on .{5,80} wrote:$", s, re.I):
            break
        if s.startswith(">"):
            continue
        out.append(ln)
    result = "\n".join(out).strip()
    return result if result else text[:4000]


def looks_spamish(from_addr: str, subject: str, body: str) -> bool:
    """轻量启发式：明显垃圾邮件直接标记，不必浪费模型调用。"""
    src = f"{subject} {body[:500]}".lower()
    markers = ["unsubscribe to stop", "viagra", "casino", "loan offer",
               "bitcoin investment", "work from home earn", "crypto airdrop"]
    if any(m in src for m in markers):
        return True
    return False


# ============ IMAP 接入 ============

def imap_connect(account) -> imaplib.IMAP4_SSL:
    pw = crypto_util.decrypt_secret(account["secret_enc"])
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(account["imap_host"], account["imap_port"],
                             ssl_context=ctx, timeout=30)
    conn.login(account["email"], pw)
    return conn


def fetch_new_mails(account, limit: int = None) -> list:
    """拉取未处理的新邮件。返回解析后的 dict 列表。"""
    limit = limit or config.MAX_FETCH_PER_CYCLE
    conn = imap_connect(account)
    results = []
    try:
        folder = account["watch_folder"] or "INBOX"
        typ, _ = conn.select(folder, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"无法打开文件夹 {folder}")

        since_ts = account["since_ts"] or (
            db.now_ts() - config.INITIAL_LOOKBACK_DAYS * 86400)
        since_str = time.strftime("%d-%b-%Y", time.gmtime(since_ts))

        typ, data = conn.uid("search", None, f'(SINCE "{since_str}")')
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        uids = uids[-limit:]  # 取最近 N 封

        # 已入库的指纹，用于跳过
        with db.ro() as c:
            known = {r["fingerprint"] for r in c.execute(
                "SELECT fingerprint FROM messages")}

        for uid in uids:
            uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
            typ, msg_data = conn.uid("fetch", uid_s, "(RFC822)")
            if typ != "OK" or not msg_data:
                continue
            raw = None
            for part in msg_data:
                if isinstance(part, tuple) and len(part) > 1:
                    raw = part[1]
                    break
            if not raw:
                continue

            parsed = parse_raw_mail(raw, account, uid_s)
            if parsed["fingerprint"] in known:
                continue
            known.add(parsed["fingerprint"])
            results.append(parsed)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return results


def parse_raw_mail(raw: bytes, account, uid: str) -> dict:
    msg = email.message_from_bytes(raw)
    msgid = (msg.get("Message-ID") or "").strip()
    from_addr, from_name = parse_addr(msg.get("From", ""))
    subject = decode_header_val(msg.get("Subject", ""))
    to_addr = decode_header_val(msg.get("To", ""))
    text, html, atts = extract_body(msg)

    # 时间：Date 头缺失或异常时回退到当前时间
    try:
        dt = email.utils.parsedate_to_datetime(msg.get("Date", ""))
        sent_ts = int(dt.timestamp())
    except Exception:
        sent_ts = db.now_ts()

    if msgid:
        fp = "mid:" + msgid
    else:
        h = hashlib.sha256()
        h.update(f"{from_addr}|{subject}|{sent_ts}|{len(raw)}".encode())
        fp = "sha:" + h.hexdigest()

    return {
        "fingerprint": fp,
        "msgid": msgid,
        "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
        "refs": (msg.get("References") or "").strip(),
        "folder": account["watch_folder"],
        "uid": uid,
        "direction": "in",
        "from_addr": from_addr,
        "from_name": from_name,
        "to_addr": to_addr,
        "subject": subject,
        "body_text": text[:200_000],
        "body_html": html[:400_000],
        "attachments": json.dumps(atts, ensure_ascii=False),
        "sent_ts": sent_ts,
        "spam_hint": looks_spamish(from_addr, subject, text),
    }


def refs_list(in_reply_to: str, refs: str) -> list:
    out = []
    for blob in (refs or "", in_reply_to or ""):
        out.extend(re.findall(r"<[^>]+>", blob))
    return out


# ============ 入库 ============

def ingest(parsed: dict, account_id: int) -> int | None:
    """把解析好的邮件写入数据库，同时维护客户与线索。返回 message.id。"""
    ts = db.now_ts()
    with db.tx() as conn:
        exist = conn.execute(
            "SELECT id FROM messages WHERE fingerprint=?", (parsed["fingerprint"],)
        ).fetchone()
        if exist:
            return None

        # --- 客户 ---
        contact = conn.execute(
            "SELECT * FROM contacts WHERE email=?", (parsed["from_addr"],)
        ).fetchone()
        if contact:
            contact_id = contact["id"]
            conn.execute(
                "UPDATE contacts SET last_seen_ts=?, msg_count=msg_count+1 WHERE id=?",
                (parsed["sent_ts"], contact_id))
        else:
            cur = conn.execute(
                "INSERT INTO contacts (email,name,source,first_seen_ts,"
                "last_seen_ts,msg_count,reply_count,score,stage) "
                "VALUES (?,?,?,?,?,1,0,0,'new')",
                (parsed["from_addr"], parsed["from_name"], "email",
                 parsed["sent_ts"], parsed["sent_ts"]))
            contact_id = cur.lastrowid

        # --- 线索归并 ---
        thread_id = None
        anchors = refs_list(parsed["in_reply_to"], parsed["refs"])
        if anchors:
            qs = ",".join("?" * len(anchors))
            row = conn.execute(
                f"SELECT thread_id FROM messages WHERE msgid IN ({qs}) "
                f"AND thread_id IS NOT NULL LIMIT 1", anchors).fetchone()
            if row:
                thread_id = row["thread_id"]
        if thread_id is None:
            # 兜底：同客户 + 规范化主题 视为同一条线索（限 30 天内）
            row = conn.execute(
                "SELECT id FROM threads WHERE contact_id=? AND (subject=? OR subject IS NULL) "
                "AND (last_ts IS NULL OR last_ts > ?) ORDER BY last_ts DESC LIMIT 1",
                (contact_id, normalize_subject(parsed["subject"]), ts - 30 * 86400)
            ).fetchone()
            if row:
                thread_id = row["id"]
        if thread_id is None:
            cur = conn.execute(
                "INSERT INTO threads (contact_id,subject,root_msgid,msg_count,"
                "last_ts,score,status) VALUES (?,?,?,1,?,0,'open')",
                (contact_id, normalize_subject(parsed["subject"]),
                 parsed["msgid"], parsed["sent_ts"]))
            thread_id = cur.lastrowid
        else:
            conn.execute(
                "UPDATE threads SET msg_count=msg_count+1, last_ts=? WHERE id=?",
                (parsed["sent_ts"], thread_id))

        # --- 邮件 ---
        cur = conn.execute(
            "INSERT INTO messages (account_id,contact_id,thread_id,fingerprint,"
            "msgid,in_reply_to,refs,folder,uid,direction,from_addr,from_name,"
            "to_addr,subject,body_text,body_html,attachments,sent_ts,status,"
            "is_read,created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new',0,?)",
            (account_id, contact_id, thread_id, parsed["fingerprint"],
             parsed["msgid"], parsed["in_reply_to"], parsed["refs"],
             parsed["folder"], parsed["uid"], parsed["direction"],
             parsed["from_addr"], parsed["from_name"], parsed["to_addr"],
             parsed["subject"], parsed["body_text"], parsed["body_html"],
             parsed["attachments"], parsed["sent_ts"], ts))
        return cur.lastrowid


def record_outgoing(account_id: int, contact_id: int, thread_id: int,
                    to_addr: str, subject: str, body: str,
                    msgid: str = None) -> int:
    """记录一封外发回复。让后台能看到完整的往来。"""
    ts = db.now_ts()
    fp = "out:" + (msgid or hashlib.sha256(
        f"{to_addr}|{subject}|{ts}".encode()).hexdigest())
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO messages (account_id,contact_id,thread_id,fingerprint,"
            "msgid,direction,from_addr,to_addr,subject,body_text,sent_ts,"
            "status,is_read,created_ts) "
            "VALUES (?,?,?,?,?,'out',?,?,?,?,?,'auto_sent',1,?)",
            (account_id, contact_id, thread_id, fp, msgid, "", to_addr,
             subject, body, ts, ts))
        conn.execute(
            "UPDATE contacts SET reply_count=reply_count+1 WHERE id=?", (contact_id,))
        conn.execute("UPDATE threads SET last_ts=? WHERE id=?", (ts, thread_id))
        return cur.lastrowid
