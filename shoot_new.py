"""截取本轮改动的三个页面（设置页 / 邮件详情 / 客户详情），用于人工核对。

用法：python shoot_new.py
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# 本机环境设了 HTTP_PROXY，Chromium 会连它，但代理拒绝转发 127.0.0.1。
# 必须在启动前清掉，并加 --no-proxy-server 双保险。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"

BASE = "http://127.0.0.1:8090"
OUT = Path(__file__).resolve().parent / "shots"
OUT.mkdir(parents=True, exist_ok=True)
PW = "LocalTest123"

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage",
                                "--no-proxy-server"])
    ctx = b.new_context(viewport={"width": 1400, "height": 1000},
                        device_scale_factor=1.5)
    page = ctx.new_page()

    page.goto(BASE + "/login", wait_until="networkidle")
    page.fill("input[name=username]", "admin")
    page.fill("input[name=password]", PW)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    print("登录后 URL:", page.url)

    # 侧边栏字号：单独截一条窄图看菜单
    page.set_viewport_size({"width": 900, "height": 1000})
    page.goto(BASE + "/", wait_until="networkidle")
    page.screenshot(path=str(OUT / "nav_font.png"), clip={"x": 0, "y": 0,
                                                         "width": 300,
                                                         "height": 620})

    page.set_viewport_size({"width": 1400, "height": 1000})

    targets = [("settings_ai", "/settings"), ("contacts_new", "/contacts")]
    for name, path in targets:
        page.goto(BASE + path, wait_until="networkidle")
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
        print("已截:", name)

    # 客户详情：取第一个客户
    page.goto(BASE + "/contacts", wait_until="networkidle")
    links = page.eval_on_selector_all(
        'a[href^="/contacts/"]', "els => els.map(e => e.getAttribute('href'))")
    if links:
        page.goto(BASE + links[0], wait_until="networkidle")
        page.screenshot(path=str(OUT / "contact_bg.png"), full_page=True)
        print("已截客户详情:", links[0])

    # 邮件详情：取第一封询盘
    page.goto(BASE + "/messages", wait_until="networkidle")
    mlinks = page.eval_on_selector_all(
        'a[href^="/messages/"]', "els => els.map(e => e.getAttribute('href'))")
    msgs = [l for l in mlinks if l.count("/") == 2 and l.split("/")[-1].isdigit()]
    if msgs:
        page.goto(BASE + msgs[0], wait_until="networkidle")
        page.screenshot(path=str(OUT / "message_summary.png"), full_page=True)
        print("已截邮件详情:", msgs[0])

    b.close()

print("\n输出目录:", OUT)
