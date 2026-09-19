"""SMTP 发信。走 465 SSL 或 587 STARTTLS，两者实测在 VPS 上均可达。

注意：绝不在 VPS 上用 25 端口裸发直投 —— 出站 25 已被屏蔽，
且无发信信誉会让邮件进垃圾箱。一律走认证提交（submission）。
"""
import email.utils
import smtplib
import ssl
from email.message import EmailMessage
from email.header import Header

from . import crypto_util


def send_mail(account, to_addr: str, subject: str, body_text: str,
              reply_to_msgid: str = None, references: str = None,
              from_name: str = None) -> tuple[bool, str]:
    """发送一封纯文本回复。返回 (成功, 说明/错误)。"""
    try:
        password = crypto_util.decrypt_secret(account["secret_enc"])
    except Exception as e:
        return False, f"凭据解密失败: {e}"

    msg = EmailMessage()
    display = from_name or account["display_name"] or account["email"]
    msg["From"] = email.utils.formataddr((display, account["email"]))
    msg["To"] = to_addr
    msg["Subject"] = Header(subject or "(no subject)", "utf-8").encode()
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["Message-ID"] = email.utils.make_msgid(domain=account["email"].split("@")[-1])
    if reply_to_msgid:
        msg["In-Reply-To"] = reply_to_msgid
        msg["References"] = references or reply_to_msgid
    msg.set_content(body_text or "", charset="utf-8")

    ctx = ssl.create_default_context()
    host = account["smtp_host"]
    port = int(account["smtp_port"])
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(account["email"], password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(account["email"], password)
                s.send_message(msg)
        return True, msg["Message-ID"]
    except smtplib.SMTPAuthenticationError:
        return False, "认证失败：授权码错误或已失效，请重新生成"
    except smtplib.SMTPRecipientsRefused:
        return False, "收件人被拒绝：地址无效或被对方服务器拒收"
    except (smtplib.SMTPException, OSError) as e:
        return False, f"发送失败: {type(e).__name__}: {e}"


def test_connection(account) -> tuple[bool, str]:
    """连通性自检：只登录不发信，用于后台"测试连接"按钮。"""
    try:
        password = crypto_util.decrypt_secret(account["secret_enc"])
    except Exception as e:
        return False, f"凭据解密失败: {e}"
    ctx = ssl.create_default_context()
    port = int(account["smtp_port"])
    try:
        if port == 465:
            with smtplib.SMTP_SSL(account["smtp_host"], port, context=ctx,
                                  timeout=20) as s:
                s.login(account["email"], password)
        else:
            with smtplib.SMTP(account["smtp_host"], port, timeout=20) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(account["email"], password)
        return True, "SMTP 登录成功"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"SMTP 认证失败: {e}"
    except Exception as e:
        return False, f"SMTP 连接失败: {type(e).__name__}: {e}"


def test_imap(account) -> tuple[bool, str]:
    """IMAP 连通性自检。"""
    from .mail_engine import imap_connect
    try:
        conn = imap_connect(account)
        try:
            conn.select("INBOX", readonly=True)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return True, "IMAP 登录成功"
    except Exception as e:
        return False, f"IMAP 连接失败: {type(e).__name__}: {e}"
