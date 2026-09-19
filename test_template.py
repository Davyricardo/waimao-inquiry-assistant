"""模板引擎单元测试。模板引擎是自己写的，必须有独立测试兜底。"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from app.templating import _render_block, _eval_cond, _eval_expr, _interpolate

PASS = FAIL = 0


def ck(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [ OK ] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望: {want!r}\n         实际: {got!r}")


def r(tpl, **ctx):
    return _render_block(tpl, ctx)


print("=" * 60)
print(" 模板引擎测试")
print("=" * 60)

print("\n-- 基础插值 --")
ck("简单变量", _interpolate("a={{ x }}", {"x": 5}), "a=5")
ck("变量为空", _interpolate("a={{ x }}", {"x": None}), "a=")
ck("HTML 转义", _interpolate("{{ x }}", {"x": "<b>"}), "&lt;b&gt;")
ck("原始插值不转义", _interpolate("{{{ x }}}", {"x": "<b>"}), "<b>")
ck("字典取值", _interpolate("{{ a.b }}", {"a": {"b": "ok"}}), "ok")
ck("列表下标", _interpolate("{{ a[1] }}", {"a": ["x", "y"]}), "y")

print("\n-- 条件判断 --")
ck("简单真", r("{% if x %}Y{% endif %}", x=1), "Y")
ck("简单假", r("{% if x %}Y{% endif %}", x=0), "")
ck("else 分支", r("{% if x %}A{% else %}B{% endif %}", x=0), "B")
ck("else 分支取真", r("{% if x %}A{% else %}B{% endif %}", x=1), "A")
ck("等值比较", r("{% if x == 5 %}Y{% endif %}", x=5), "Y")
ck("不等比较", r("{% if x != 5 %}Y{% endif %}", x=3), "Y")
ck("大于比较", r("{% if x > 5 %}Y{% endif %}", x=9), "Y")
ck("is none", r("{% if x is none %}Y{% endif %}", x=None), "Y")
ck("is not none", r("{% if x is not none %}Y{% endif %}", x=1), "Y")
ck("not 取反", r("{% if not x %}Y{% endif %}", x=0), "Y")
ck("and 组合", r("{% if x == 1 and y == 2 %}Y{% endif %}", x=1, y=2), "Y")
ck("or 组合", r("{% if x == 9 or y == 2 %}Y{% endif %}", x=1, y=2), "Y")

print("\n-- 嵌套 if（曾经的 bug 点）--")
tpl = "{% if a %}A{% if b %}B{% else %}C{% endif %}{% else %}D{% endif %}"
ck("外真内真", r(tpl, a=1, b=1), "AB")
ck("外真内假", r(tpl, a=1, b=0), "AC")
ck("外假", r(tpl, a=0, b=1), "D")

tpl2 = ("{% if d.delta is not none %}{% if d.delta >= 0 %}+{{ d.delta }}%"
        "{% else %}{{ d.delta }}%{% endif %}{% else %}昨日 {{ d.yday_in }}{% endif %}")
ck("三层嵌套-正增长", r(tpl2, d={"delta": 12, "yday_in": 5}), "+12%")
ck("三层嵌套-负增长", r(tpl2, d={"delta": -3, "yday_in": 5}), "-3%")
ck("三层嵌套-无对比", r(tpl2, d={"delta": None, "yday_in": 5}), "昨日 5")

print("\n-- 循环 --")
ck("基础循环", r("{% for i in xs %}{{ i }}{% endfor %}", xs=[1, 2, 3]), "123")
ck("循环空列表", r("{% for i in xs %}{{ i }}{% endfor %}", xs=[]), "")
ck("循环对象属性", r("{% for i in xs %}{{ i.n }};{% endfor %}",
                    xs=[{"n": "a"}, {"n": "b"}]), "a;b;")
ck("loop.first", r("{% for i in xs %}{% if loop.first %}F{% endif %}{% endfor %}",
                   xs=[1, 2]), "F")
ck("loop.last", r("{% for i in xs %}{% if loop.last %}L{% endif %}{% endfor %}",
                  xs=[1, 2]), "L")
ck("循环内嵌 if", r("{% for i in xs %}{% if i > 1 %}{{ i }}{% endif %}{% endfor %}",
                    xs=[1, 2, 3]), "23")

print("\n-- 循环 + 嵌套 if（复合场景）--")
tpl3 = ("{% for m in ms %}{{ m.n }}:"
        "{% if m.s >= 75 %}高{% else %}{% if m.s >= 50 %}中{% else %}低{% endif %}{% endif %};"
        "{% endfor %}")
ck("循环内多层判断", r(tpl3, ms=[{"n": "a", "s": 90}, {"n": "b", "s": 60},
                                  {"n": "c", "s": 10}]),
   "a:高;b:中;c:低;")

print("\n-- 边界 --")
ck("未定义变量", _interpolate("{{ nope }}", {}), "")
ck("未定义属性", _interpolate("{{ a.missing }}", {"a": {}}), "")
ck("空模板", r("", x=1), "")
ck("if 未闭合不崩", r("{% if x %}Y", x=1) in ("Y", "{% if x %}Y"), True)
ck("字面量字符串", r("{% if x == 'ok' %}Y{% endif %}", x="ok"), "Y")

print("\n-- or / and 兜底（曾经的 bug：{{ }} 里 or 被当成路径解析，静默输出空）--")
_bg = {"nested": {"role": "经销商", "name": "Mueller GmbH"},
       "empty": "", "nil": None}
ck("嵌套属性", r("{{ c.nested.role }}", c=_bg), "经销商")
ck("or 走第一支", r("{{ c.nested.role or '未识别' }}", c=_bg), "经销商")
ck("or 走兜底", r("{{ c.missing or '未识别' }}", c=_bg), "未识别")
ck("or 链式（首支命中）", r("{{ c.nested.name or c.other or '未识别' }}", c=_bg),
   "Mueller GmbH")
ck("or 链式（次级命中）", r("{{ c.nope or c.nested.name or '未识别' }}", c=_bg),
   "Mueller GmbH")
ck("or 链式（全落空）", r("{{ c.nope or c.nada or '未识别' }}", c=_bg), "未识别")
ck("空串视为假值", r("{{ c.empty or '兜底' }}", c=_bg), "兜底")
ck("None 视为假值", r("{{ c.nil or '兜底' }}", c=_bg), "兜底")
ck("and 两真取后者", r("{{ c.nested.role and c.nested.name }}", c=_bg), "Mueller GmbH")
ck("and 遇假短路", r("{{ c.missing and c.nested.name or '兜底' }}", c=_bg), "兜底")
# 关键回归：引号内的字面量不能被当成 or 分隔符
ck("不切引号内 or", r("{{ c.missing or 'a or b' }}", c=_bg), "a or b")
ck("不切引号内 and", r("{{ c.missing or 'x and y' }}", c=_bg), "x and y")
ck("比较与 or 共存",
   r("{% if c.nested.role == '经销商' %}Y{% endif %}", c=_bg), "Y")
ck("if 里用 or", r("{% if c.missing or c.nested.role %}Y{% endif %}", c=_bg), "Y")

print()
print("-- elif 分支 --")
_elif = "{% if n >= 75 %}hi{% elif n >= 50 %}mid{% else %}lo{% endif %}"
ck("elif 命中第一支", r(_elif, n=80), "hi")
ck("elif 命中第二支", r(_elif, n=60), "mid")
ck("elif 落到 else", r(_elif, n=10), "lo")
ck("elif 边界 75", r(_elif, n=75), "hi")
ck("elif 边界 50", r(_elif, n=50), "mid")
ck("elif 边界 49", r(_elif, n=49), "lo")
ck("多 elif 链接",
   r("{% if n==1 %}a{% elif n==2 %}b{% elif n==3 %}c{% else %}d{% endif %}", n=3), "c")
ck("无 else 且全不命中",
   r("{% if a %}X{% elif b %}Y{% endif %}", a=0, b=0), "")
ck("无 else 命中 elif",
   r("{% if a %}X{% elif b %}Y{% endif %}", a=0, b=1), "Y")
ck("elif 内嵌 if",
   r("{% if a %}1{% elif b %}{% if c %}2a{% else %}2b{% endif %}{% endif %}",
     a=0, b=1, c=1), "2a")
ck("elif 内嵌 for",
   r("{% if a %}x{% elif b %}{% for i in it %}{{ i }}{% endfor %}{% endif %}",
     a=0, b=1, it=[1, 2]), "12")
ck("elif 与 or 共用",
   r("{% if a %}1{% elif b or c %}2{% else %}3{% endif %}", a=0, b=0, c=1), "2")
ck("elif 不残留标签", r(_elif, n=80), "hi")
ck("elif 属性场景（真实用法）",
   r("<span class=\"score {% if s >= 75 %}hi{% elif s >= 50 %}mid{% else %}lo{% endif %}\">",
     s=55), '<span class="score mid">')

print()
print("=" * 60)
print(f" 通过 {PASS} · 失败 {FAIL}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
