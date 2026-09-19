"""极简模板渲染。

不引入 Jinja2 —— 少一个依赖，启动更快，且我们的模板需求很有限。
支持：{{ var }} 转义插值、{{{ var }}} 原始插值、{% include %}、{% for %} / {% endfor %}、{% if %} / {% endif %}
"""
import html
import re
import time
from pathlib import Path

TPL_DIR = Path(__file__).resolve().parent / "templates"
_cache = {}      # 模板源码缓存
_rcache = {}     # 渲染结果缓存（同一请求内重复 include 时用）


# ============ 模板内置工具（注入到每个渲染上下文） ============

def ts_fmt(ts, fmt: str = "%m-%d %H:%M") -> str:
    """UTC 时间戳 → 本地时间字符串。"""
    if not ts:
        return "—"
    from . import config
    return time.strftime(fmt, time.gmtime(int(ts) + config.TZ_OFFSET_HOURS * 3600))


def ts_full(ts) -> str:
    return ts_fmt(ts, "%Y-%m-%d %H:%M")


def ts_day(ts) -> str:
    return ts_fmt(ts, "%Y-%m-%d")


def ago(ts) -> str:
    """相对时间。"""
    if not ts:
        return "—"
    from . import config
    delta = time.time() - int(ts)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前"
    return ts_day(ts)


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def truncate(s, n: int = 60) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "…"


def initial(name_or_email) -> str:
    """取头像文字：优先用名字首字母，中文取首字。"""
    s = str(name_or_email or "?")
    if not s:
        return "?"
    if "@" in s and " " not in s:
        s = s.split("@")[0]
    s = s.strip()
    if not s:
        return "?"
    return s[0].upper() if s[0].isascii() else s[0]


def sel(current, value) -> str:
    """下拉框选中态。"""
    return "selected" if str(current) == str(value) else ""


def checked(v) -> str:
    return "checked" if v else ""


def default(v, d):
    return v if (v is not None and v != "") else d


def local_time_badge(country_or_contact) -> str:
    """模板函数：为传入的国家名或联系人对象渲染紧凑型当地时间状态徽章。"""
    if not country_or_contact:
        return ""
    from . import geo_time
    if isinstance(country_or_contact, dict):
        info = geo_time.get_contact_time_info(country_or_contact)
    else:
        info = geo_time.get_country_time_info(str(country_or_contact))
    return info["badge_html"] if info else ""


def local_time_info(country_or_contact) -> dict:
    """模板函数：获取国家或联系人的详细时区与作息字典。"""
    if not country_or_contact:
        return {}
    from . import geo_time
    if isinstance(country_or_contact, dict):
        info = geo_time.get_contact_time_info(country_or_contact)
    else:
        info = geo_time.get_country_time_info(str(country_or_contact))
    return info or {}


BUILTINS = {
    "ts_fmt": ts_fmt, "ts_full": ts_full, "ts_day": ts_day, "ago": ago,
    "esc": esc, "truncate": truncate, "initial": initial, "sel": sel,
    "checked": checked, "default": default,
    "local_time_badge": local_time_badge, "local_time_info": local_time_info,
}


def _load(name: str) -> str:
    if name not in _cache:
        _cache[name] = (TPL_DIR / name).read_text(encoding="utf-8")
    return _cache[name]


def _resolve(expr: str, ctx: dict):
    """支持 a.b.c 与 a['b'] 与函数调用 a(b) 的简单取值。"""
    expr = expr.strip()
    # 函数调用：func(arg1, arg2)
    m = re.match(r"^([\w.\[\]]+)\((.*)\)$", expr, re.S)
    if m:
        fn_path, args_s = m.group(1), m.group(2)
        args = []
        if args_s.strip():
            depth, cur = 0, ""
            for ch in args_s:
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                if ch == "," and depth == 0:
                    args.append(cur)
                    cur = ""
                else:
                    cur += ch
            args.append(cur)
            args = [_resolve(a, ctx) for a in args]
        # 方法调用（如 d.items()）：先分离「宿主 + 方法名」再解析宿主。
        # 注意：必须在解析完 args 之后再拆 host，否则 d.items 会被
        # 路径分段解析成 bound method，拿不到宿主对象。
        if "." in fn_path:
            host_path, method_name = fn_path.rsplit(".", 1)
            host = _resolve(host_path, ctx)
            fn = getattr(host, method_name, None)
        else:
            fn = _resolve(fn_path, ctx)
            method_name = fn_path
        if callable(fn):
            try:
                return fn(*args)
            except Exception:
                return ""
        return ""

    # 字面量
    if (expr.startswith('"') and expr.endswith('"')) or \
       (expr.startswith("'") and expr.endswith("'")):
        return expr[1:-1]
    if re.match(r"^-?\d+$", expr):
        return int(expr)
    if expr in ("True", "true"):
        return True
    if expr in ("False", "false"):
        return False
    if expr in ("None", "none", "null"):
        return None

    # 路径取值
    parts = re.split(r"\.|\[|\]", expr)
    parts = [p.strip().strip("'\"") for p in parts if p.strip()]
    cur = ctx
    for p in parts:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        else:
            cur = getattr(cur, p, None)
    return cur


def _iter_seq(seq) -> list:
    """把 for 的目标序列规整成 list。

    规则：
      - None / 空 -> []
      - dict      -> list(dict.items())，即条目为 (k, v) 二元组
      - 其它可迭代 -> list(...)
      - 字符串 / 不可迭代 -> []（避免把字符串当逐字遍历）
    """
    if seq is None:
        return []
    if isinstance(seq, dict):
        return list(seq.items())
    if isinstance(seq, (str, bytes)):
        return []
    try:
        return list(seq)
    except TypeError:
        return []


def _unpack(item, n: int):
    """把一个条目解构成 n 个值。不匹配或不可解构时返回 None（该轮跳过）。"""
    if isinstance(item, (str, bytes)):
        return None
    try:
        vals = list(item)
    except TypeError:
        return None
    if len(vals) != n:
        return None
    return vals


def _eval_cond(cond: str, ctx: dict) -> bool:
    cond = cond.strip()
    # 支持 and / or / not 的简单组合，按 or 拆再按 and 拆
    for or_part in re.split(r"\s+or\s+", cond):
        ok = True
        for and_part in re.split(r"\s+and\s+", or_part):
            and_part = and_part.strip()
            neg = False
            if and_part.startswith("not "):
                neg = True
                and_part = and_part[4:].strip()
            val = _eval_expr(and_part, ctx)
            if neg:
                val = not val
            if not val:
                ok = False
                break
        if ok:
            return True
    return False


def _split_outside_quotes(expr: str, sep_re: str) -> list:
    """按分隔符切分，但跳过引号内的内容。

    `{{ a or '未识别' }}` 不该在 `'未识别'` 里找分隔符；
    用简单的引号状态机手动扫描，比正则可靠。
    """
    parts, cur, quote = [], "", None
    i = 0
    while i < len(expr):
        ch = expr[i]
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            cur += ch
            i += 1
            continue
        m = re.match(sep_re, expr[i:])
        if m:
            parts.append(cur)
            cur = ""
            i += m.end()
            continue
        cur += ch
        i += 1
    parts.append(cur)
    return parts


def _eval_expr(expr: str, ctx: dict):
    expr = expr.strip()

    # ---- 逻辑运算：or / and ----
    # 必须放在比较运算之前求值：`a or b == c` 里的 or 优先级最低，
    # 但模板里几乎不会这么写，先拆 or/and 更符合直觉且更安全。
    # 关键：用引号感知的切分，避免把 '未识别' 这类字面量切开。
    or_parts = _split_outside_quotes(expr, r"\s+or\s+")
    if len(or_parts) > 1:
        for p in or_parts:
            v = _eval_expr(p, ctx)
            if v:
                return v
        return _eval_expr(or_parts[-1], ctx)
    and_parts = _split_outside_quotes(expr, r"\s+and\s+")
    if len(and_parts) > 1:
        last = None
        for p in and_parts:
            last = _eval_expr(p, ctx)
            if not last:
                return last
        return last

    # is none / is not none
    m = re.match(r"^(.+?)\s+is\s+(not\s+)?none$", expr, re.I)
    if m:
        val = _eval_expr(m.group(1), ctx)
        is_none = val is None
        return (not is_none) if m.group(2) else is_none
    # is defined / is not defined
    m = re.match(r"^(.+?)\s+is\s+(not\s+)?defined$", expr, re.I)
    if m:
        val = _eval_expr(m.group(1), ctx)
        has = val is not None
        return (not has) if m.group(2) else has
    # 比较运算（按长度降序，避免 != 被 == 抢先匹配）
    for op in ("==", "!=", ">=", "<=", ">", "<"):
        if op in expr:
            left, right = expr.split(op, 1)
            a = _eval_expr(left, ctx)
            b = _eval_expr(right, ctx)
            try:
                if op == "==":
                    return a == b
                if op == "!=":
                    return a != b
                if op == ">=":
                    return (a or 0) >= (b or 0)
                if op == "<=":
                    return (a or 0) <= (b or 0)
                if op == ">":
                    return (a or 0) > (b or 0)
                if op == "<":
                    return (a or 0) < (b or 0)
            except TypeError:
                return False
    return _resolve(expr, ctx)


TAG = re.compile(
    r"\{%\s*(if\s+[^%]*?|elif\s+[^%]*?|else|endif"
    r"|for\s+[^%]*?|endfor)\s*%\}"
)
IF_OPEN = re.compile(r"\{%\s*if\s+([^%]+?)\s*%\}")
FOR_OPEN = re.compile(
    r"\{%\s*for\s+([\w\s,]+?)\s+in\s+([\w.\[\]'\"]+(?:\([^)]*\))?)\s*%\}"
)
INC_OPEN = re.compile(r"\{%\s*include\s+['\"]([\w.]+)['\"]\s*%\}")


def _match_if(tpl: str, start: int) -> dict | None:
    """从 start（{% if %} 的位置）找配对结束，并拆出所有分支。

    返回 {"branches": [(cond_or_None, body_start, body_end), ...],
          "endif_end": int}

    - 首个分支的 cond 是 if 的条件；`elif` 的 cond 是各自条件；
      `else` 的 cond 为 None（永远命中）。
    - 做严格深度计数，支持任意层嵌套。
    """
    depth = 0
    first_cond = None
    body_start = None
    branches = []
    for m in TAG.finditer(tpl, start):
        tag = m.group(1).strip()
        if tag.startswith("if ") and (len(tag) > 3 and tag[2].isspace()):
            depth += 1
            if depth == 1:
                first_cond = tag[3:].strip()
                body_start = m.end()
        elif tag.startswith("elif ") and depth == 1:
            branches.append((first_cond, body_start, m.start()))
            first_cond = tag[5:].strip()
            body_start = m.end()
        elif tag == "else" and depth == 1:
            branches.append((first_cond, body_start, m.start()))
            first_cond = None            # None 表示兜底分支
            body_start = m.end()
        elif tag == "endif":
            depth -= 1
            if depth == 0:
                if body_start is None:
                    return None
                branches.append((first_cond, body_start, m.start()))
                return {"branches": branches, "endif_end": m.end()}
    return None


def _match_for(tpl: str, start: int) -> tuple[int, int, int] | None:
    """从 start（{% for %} 的位置）找配对的 {% endfor %}。

    返回 (body_start, body_end, endfor_tag_end)。
    body_end 是配对 endfor 的**起始**位置，endfor_tag_end 是该标签的结束位置
    （用于把整个 for 块从模板里摘掉）。

    ⚠️ 必须做深度计数：嵌套 for 时不能简单用非贪婪正则找第一个 endfor，
    否则会截在**内层** endfor 上，把外层 {% endfor %} 留在模板里当文本输出。
    """
    depth = 0
    body_start = None
    for m in TAG.finditer(tpl, start):
        tag = m.group(1).strip()
        if tag.startswith("for "):
            depth += 1
            if depth == 1:
                body_start = m.end()
        elif tag == "endfor":
            depth -= 1
            if depth == 0 and body_start is not None:
                return (body_start, m.start(), m.end())
    return None


def _render_block(tpl: str, ctx: dict, depth_guard: int = 0) -> str:
    """递归渲染控制结构。

    关键：按**出现位置**决定先处理哪个。若先处理 for 内部的 if，
    会把循环体的片段提前消解掉，导致 loop 变量与循环语义错乱。
    因此总是处理位置上最靠前的那个标签。
    """
    if depth_guard > 40:
        return tpl

    im = IF_OPEN.search(tpl)
    fm = FOR_OPEN.search(tpl)

    # 谁在前面先处理谁
    if fm and (not im or fm.start() < im.start()):
        res = _match_for(tpl, fm.start())
        if res:
            body_start, body_end, tag_end = res
            targets = [t.strip() for t in fm.group(1).split(",") if t.strip()]
            seq = _resolve(fm.group(2), ctx)
            seq = _iter_seq(seq)
            n = len(seq)
            body = tpl[body_start:body_end]
            out = []
            for i, item in enumerate(seq):
                sub = dict(ctx)
                if len(targets) == 1:
                    sub[targets[0]] = item
                else:
                    vals = _unpack(item, len(targets))
                    if vals is None:
                        continue
                    for t, v in zip(targets, vals):
                        sub[t] = v
                sub["loop"] = {"index": i + 1, "index0": i,
                               "first": i == 0, "last": i == n - 1}
                out.append(_render_block(body, sub, depth_guard + 1))
            tpl = tpl[:fm.start()] + "".join(out) + tpl[tag_end:]
            return _render_block(tpl, ctx, depth_guard + 1)
        # 配对失败：跳过这个 for 标签继续找
        im2 = IF_OPEN.search(tpl, fm.end())
        if not im2:
            return _interpolate(tpl, ctx)

    if im:
        res = _match_if(tpl, im.start())
        if res:
            chosen = ""
            for cond, bs, be in res["branches"]:
                # cond 为 None 的是 else 兜底分支，永远命中
                if cond is None or _eval_cond(cond, ctx):
                    chosen = _render_block(tpl[bs:be], ctx, depth_guard + 1)
                    break
            tpl = tpl[:im.start()] + chosen + tpl[res["endif_end"]:]
            return _render_block(tpl, ctx, depth_guard + 1)

    # ---------- include ----------
    inc = INC_OPEN.search(tpl)
    if inc:
        tpl = tpl[:inc.start()] + _render_block(_load(inc.group(1)), ctx,
                                                depth_guard + 1) + tpl[inc.end():]
        return _render_block(tpl, ctx, depth_guard + 1)

    # 控制结构已全部消解，剩下的文本做变量插值
    return _interpolate(tpl, ctx)


def _interpolate(tpl: str, ctx: dict) -> str:
    tpl = re.sub(r"\{\{\{\s*(.+?)\s*\}\}\}",
                 lambda m: str(_eval_expr(m.group(1), ctx) or ""), tpl)
    tpl = re.sub(r"\{\{\s*(.+?)\s*\}\}",
                 lambda m: html.escape(str(_eval_expr(m.group(1), ctx)
                                           if _eval_expr(m.group(1), ctx)
                                           is not None else "")), tpl)
    return tpl


def render(name: str, **ctx) -> str:
    """渲染页面：先填页面片段，再套进 layout。

    变量插值在 _render_block 收尾时统一完成，此处不再重复插值
    （重复插值会把用户内容里的 {{ }} 再次解析，也会双重转义）。

    login.html 是自带完整 HTML 的独立页，不套 layout。
    """
    page = ctx.get("page") or name.replace(".html", "")

    merged = dict(BUILTINS)
    merged.update(ctx)

    if page.startswith("_") or name == "login.html":
        return _render_block(_load(name), merged)

    merged["page_title"] = (ctx.get("page_title")
                            or PAGE_TITLES.get(page, "外贸询盘助手"))
    for key in ("dashboard", "messages", "contacts", "freight", "orders",
                "products", "social", "company", "review", "rules", "templates", "settings"):
        merged["nav_" + key] = "active" if page == key else ""

    body = _render_block(_load(name), merged)
    # 注意：body 已是最终 HTML，不能作为普通变量参与转义，
    # 因此放进独立上下文并以原始插值注入
    shell_ctx = dict(merged)
    shell_ctx["body_raw"] = body
    shell = _load("layout.html").replace("{{{ body }}}", "{{{ body_raw }}}")
    return _render_block(shell, shell_ctx)


PAGE_TITLES = {
    "dashboard": "数据看板", "messages": "询盘列表", "contacts": "客户管理",
    "freight": "货运代理", "orders": "订单管理", "products": "产品库",
    "social": "社媒开发", "company": "公司管理", "review": "审核台", "rules": "自动规则",
    "templates": "回复模板", "settings": "系统设置",
}


def render_str(tpl: str, ctx: dict = None, **kwargs) -> str:
    """渲染模板字符串（自动注入 BUILTINS 与上下文字典）。"""
    merged = dict(BUILTINS)
    if ctx:
        merged.update(ctx)
    if kwargs:
        merged.update(kwargs)
    return _render_block(tpl, merged)
