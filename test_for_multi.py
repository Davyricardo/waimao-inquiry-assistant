"""复现：模板引擎不支持 `{% for k, v in d.items() %}` 与 `d.items()`。

背景
----
设置页「邮箱服务商」与「AI 模型服务商」两个下拉框渲染为空。
根因两层：
  1. `FOR_OPEN` 正则只允许单个循环变量 `(\\w+)`，`{% for k, p in ... %}` 压根匹配不上，
     整个 for 块被当成普通文本原样输出（连 <option> 都不生成）。
  2. `_resolve("ai_presets.items()")` —— `.items()` 里正则 `^([\\w.]+)\\((.*)\\)$` 匹配后
     走 `_resolve("ai_presets.items")`，路径分段取属性时 `getattr(dict, "items", None)`
     得到 bound method，`callable(fn)` 为真则 `fn()` 无参调用成功返回 dict_items，
     但外层 `m` 分支…… 实测返回空串，需按修复后的行为断言。
"""
import sys
sys.path.insert(0, ".")

from app import templating as T

FAILED = []
PASSED = []


def ok(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print("  [ OK ]", name)
    else:
        FAILED.append(name)
        print("  [FAIL]", name, extra)


def r(tpl, **ctx):
    return T.render_str(tpl, ctx) if hasattr(T, "render_str") else _render(tpl, **ctx)


def _render(tpl, **ctx):
    """直接调内部渲染链路，绕过文件加载。"""
    return T._render_block(tpl, ctx)


print("=" * 58)
print("  模板引擎 for 循环多变量 + dict.items() 支持")
print("=" * 58)

print("\n-- 1. 单个循环变量（回归，必须继续可用）--")
out = _render("{% for x in items %}[{{ x }}]{% endfor %}", items=["a", "b", "c"])
ok("单变量遍历列表", out == "[a][b][c]", repr(out))

print("\n-- 2. for k, v in d.items()：多变量解构 --")
d = {"deepseek": {"label": "DeepSeek"}, "openai": {"label": "OpenAI"}}
out = _render("{% for k, p in presets.items() %}{{ k }}={{ p.label }};{% endfor %}",
              presets=d)
ok("for k, v in d.items() 解构", out == "deepseek=DeepSeek;openai=OpenAI;", repr(out))

print("\n-- 3. 直接生成 <option>（真实场景）--")
out = _render('<select>{% for k, p in presets.items() %}'
              '<option value="{{ k }}">{{ p.label }}</option>{% endfor %}</select>',
              presets=d)
ok("渲染出 option 列表",
   out == '<select><option value="deepseek">DeepSeek</option>'
          '<option value="openai">OpenAI</option></select>', repr(out))
ok("option 数量为 2", out.count("<option") == 2, repr(out))

print("\n-- 4. for k, v in d.items() 但 d 为空 --")
out = _render("{% for k, p in presets.items() %}X{% endfor %}", presets={})
ok("空 dict 不报错且输出空", out == "", repr(out))

print("\n-- 5. d.items() 作为独立表达式 --")
v = T._resolve("presets.items()", {"presets": d})
ok("items() 返回可迭代对象", v is not None and len(list(v)) == 2, repr(v))

print("\n-- 6. for k, v in dict（不带 .items()）--")
out = _render("{% for k, p in presets %}{{ k }};{% endfor %}", presets=d)
ok("不带 .items() 也能解构 dict", out == "deepseek;openai;", repr(out))

print("\n-- 7. for k, v in list of pairs --")
out = _render("{% for k, v in pairs %}{{ k }}:{{ v }};{% endfor %}",
              pairs=[("a", 1), ("b", 2)])
ok("遍历二元组列表", out == "a:1;b:2;", repr(out))

print("\n-- 8. for k, v in tuple of tuples --")
# ("x","y") 作为整体传给 for k, v 是**两个条目**，各自不是二元组 ->
# 无法解构（跳过），这符合 Python 语义（for k, v in ("x","y") 会 TypeError）。
out = _render("{% for k, v in pair %}{{ k }}:{{ v }};{% endfor %}", pair=("x", "y"))
ok("扁平元组无法解构时静默跳过（不崩）", out == "", repr(out))

out = _render("{% for k, v in pairs %}{{ k }}:{{ v }};{% endfor %}",
              pairs=(("a", 1), ("b", 2)))
ok("元组套元组解构 2 次", out == "a:1;b:2;", repr(out))

print("\n-- 9. 循环体里用 loop 变量 --")
out = _render("{% for k, p in presets.items() %}{{ loop.index }}{% endfor %}", presets=d)
ok("多变量循环里 loop 可用", out == "12", repr(out))

print("\n-- 10. 嵌套 for（多变量 + 单变量）--")
out = _render("{% for k, p in presets.items() %}"
              "{% for t in tags %}{{ k }}-{{ t }};{% endfor %}{% endfor %}",
              presets={"a": {}}, tags=["x", "y"])
ok("多变量 for 内嵌单变量 for", out == "a-x;a-y;", repr(out))

print("\n-- 11. 循环体内 if --")
out = _render("{% for k, p in presets.items() %}"
              "{% if k == 'openai' %}YES{% else %}no{% endif %}{% endfor %}",
              presets=d)
ok("多变量循环体内 if/else", out == "noYES", repr(out))

print("\n-- 12. 三变量解构 --")
out = _render("{% for a, b, c in rows %}{{ a }}{{ b }}{{ c }};{% endfor %}",
              rows=[(1, 2, 3), (4, 5, 6)])
ok("三变量解构", out == "123;456;", repr(out))

print("\n-- 13. 解构不匹配时静默跳过，不抛异常 --")
try:
    out = _render("{% for a, b in rows %}{{ a }};{% endfor %}", rows=[(1, 2, 3)])
    ok("长度不匹配不抛异常", True, repr(out))
except Exception as e:
    ok("长度不匹配不抛异常", False, f"{type(e).__name__}: {e}")

print("\n-- 14. 解构遇到非可迭代项不崩 --")
try:
    out = _render("{% for a, b in rows %}{{ a }};{% endfor %}", rows=[1, 2])
    ok("非可迭代项不抛异常", True, repr(out))
except Exception as e:
    ok("非可迭代项不抛异常", False, f"{type(e).__name__}: {e}")

print("\n" + "=" * 58)
print(f"  通过 {len(PASSED)} / {len(PASSED) + len(FAILED)}")
if FAILED:
    print("  失败项：")
    for f in FAILED:
        print("    -", f)
print("=" * 58)
sys.exit(1 if FAILED else 0)
