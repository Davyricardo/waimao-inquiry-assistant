"""本地 UI 截图，用于人工核对界面效果。

用法：python shoot.py [base_url] [out_dir]
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else
           Path(__file__).resolve().parent / "shots")
OUT.mkdir(parents=True, exist_ok=True)

USER = "admin"
PW = sys.argv[3] if len(sys.argv) > 3 else "LocalTest123"

PAGES = [
    ("dashboard", "/"),
    ("messages", "/messages"),
    ("contacts", "/contacts"),
    ("review", "/review"),
    ("rules", "/rules"),
    ("templates", "/templates"),
    ("settings", "/settings"),
]

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = b.new_context(viewport={"width": 1280, "height": 900},
                        device_scale_factor=1.5)
    page = ctx.new_page()

    # 登录
    page.goto(BASE + "/login", wait_until="networkidle")
    page.fill('input[name=username]', USER)
    page.fill('input[name=password]', PW)
    page.click('button[type=submit]')
    page.wait_for_load_state("networkidle")
    cur = page.url
    if "/login" in cur:
        print("登录失败！检查用户名密码")
        body = page.inner_text("body")
        print(body[:400])
        b.close()
        sys.exit(1)
    print(f"登录成功 -> {cur}")

    for name, path in PAGES:
        page.goto(BASE + path, wait_until="networkidle")
        page.wait_for_timeout(700)
        f = OUT / f"{name}.png"
        page.screenshot(path=str(f), full_page=True)
        print(f"  {name:12s} {f.stat().st_size // 1024:5d} KB")

    # 邮件详情：取列表里第一条
    page.goto(BASE + "/messages", wait_until="networkidle")
    link = page.query_selector("table tbody tr td a")
    if link:
        href = link.get_attribute("href")
        page.goto(BASE + href, wait_until="networkidle")
        page.wait_for_timeout(700)
        f = OUT / "message_detail.png"
        page.screenshot(path=str(f), full_page=True)
        print(f"  {'msg_detail':12s} {f.stat().st_size // 1024:5d} KB  ({href})")

    # 移动端
    mctx = b.new_context(**p.devices["iPhone 13"])
    mp = mctx.new_page()
    mp.goto(BASE + "/login", wait_until="networkidle")
    mp.fill('input[name=username]', USER)
    mp.fill('input[name=password]', PW)
    mp.click('button[type=submit]')
    mp.wait_for_load_state("networkidle")
    for name, path in [("mobile_dashboard", "/"), ("mobile_review", "/review"),
                       ("mobile_messages", "/messages")]:
        mp.goto(BASE + path, wait_until="networkidle")
        mp.wait_for_timeout(700)
        f = OUT / f"{name}.png"
        mp.screenshot(path=str(f), full_page=True)
        # 检查页面级横向溢出
        overflow = mp.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth")
        vw = mp.evaluate("() => window.innerWidth")
        flag = "OK" if overflow <= 1 else f"OVERFLOW +{overflow}px"
        print(f"  {name:12s} vw={vw} {flag}")

    b.close()

print(f"\n截图目录：{OUT}")
