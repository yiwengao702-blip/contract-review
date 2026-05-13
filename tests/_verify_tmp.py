# 临时验证脚本：顶栏用户区商务化 + 退出确认（验证完即删）
# 关键原则：对比度用的 alpha / 色值一律从 HTTP 下发的 CSS 里**解析**出来，
#           绝不把期望值写在脚本里——否则脚本只会证实我的假设，而不是验证真实产物。
import re
import subprocess
import sys

import requests

BASE = "http://127.0.0.1:18090"
html = requests.get(BASE + "/", timeout=8).text
css = requests.get(BASE + "/static/style.css", timeout=8).text
js = requests.get(BASE + "/static/app.js", timeout=8).text


# ---------------- WCAG 对比度 ----------------
def srgb(c):
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def lum(rgb):
    r, g, b = (srgb(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg):
    l1, l2 = lum(fg), lum(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def over(fg, a, bg):
    return tuple(round(f * a + b * (1 - a)) for f, b in zip(fg, bg))


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ---------------- 从 CSS 产物里解析实际取值 ----------------
def block(sel):
    """取选择器的规则体（取最左侧匹配，故 :hover 不会误伤）"""
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else ""


def prop_rgba_alpha(blk, prop):
    """从 'prop: rgba(255,255,255,X)' 里抽 X。

    必须加 (?<![-\\w]) 词边界：属性名是彼此的子串——'border-color' 里含 'color'，
    不加断言时搜 color 会命中 border-color，把边框的 alpha 当成文字的 alpha。
    """
    m = re.search(r"(?<![-\w])" + re.escape(prop)
                  + r"\s*:[^;]*?rgba\(255,\s*255,\s*255,\s*([\d.]+)\)", blk)
    return float(m.group(1)) if m else None


def root_var(name):
    m = re.search(re.escape(name) + r"\s*:\s*(#[0-9a-fA-F]{6})", css)
    return hexrgb(m.group(1)) if m else None


BAR_L = root_var("--primary")
BAR_R = root_var("--primary-2")
W = (255, 255, 255)
print(f"=== 0. 从 CSS 解析出的顶栏渐变两端：{BAR_L} → {BAR_R} ===\n")

print("=== 1. 前端结构（HTTP 实测，非本地文件）===")
checks = [
    ("退出确认弹窗容器", 'id="logoutModal"' in html),
    ("身份卡片", 'class="confirm-card"' in html),
    ("确认/取消按钮", 'id="logoutOkBtn"' in html and 'id="logoutCancelBtn"' in html),
    ("JS 点退出改为打开确认框",
     'getElementById("logoutBtn").onclick = openLogoutModal' in js),
    ("JS 确认框填入当前账号",
     'logoutName").textContent' in js and 'logoutRole").textContent' in js),
    ("JS 取消按钮关闭", 'logoutCancelBtn").onclick = closeLogoutModal' in js),
    ("JS 遮罩点击关闭", 'e.target.id === "logoutModal"' in js),
    ("JS Esc 关闭", 'e.key === "Escape"' in js and 'closeLogoutModal()' in js),
    ("JS 清会话只出现在确认回调里",
     js.index('logoutOkBtn").onclick') < js.index('removeItem(SESSION_KEY)')),
    ("JS 清会话后跳回登录页", 'location.replace("/login")' in js),
    ("CSS 退出按钮透明底",
     "background: transparent" in block(".topbar .btn.ghost")),
    ("CSS 全局 .btn.ghost 仍白底（弹窗取消按钮不受影响）",
     ".btn.ghost { background: #fff" in css),
    ("CSS 窄屏隐藏姓名/角色", ".user-meta { display: none; }" in css),
]
for name, ok in checks:
    print(f"  {'✓' if ok else '✗'} {name}")

# ---------------- 对比度：全部取值来自 CSS ----------------
btn = block(".topbar .btn.ghost")
btn_fg_a = prop_rgba_alpha(btn, "color")
btn_bd_a = prop_rgba_alpha(btn, "border-color")
role_a = prop_rgba_alpha(block(".user-role"), "color")
ava_bg_a = prop_rgba_alpha(block(".user-avatar"), "background")
ava_bd_a = prop_rgba_alpha(block(".user-avatar"), "box-shadow")
box_bd_a = prop_rgba_alpha(block(".user-box"), "border-left")
print(f"\n  解析到的 alpha：按钮文字={btn_fg_a} 按钮边框={btn_bd_a} "
      f"角色名={role_a} 头像底={ava_bg_a} 头像边={ava_bd_a} 分隔线={box_bd_a}")

print("\n=== 2. 对比度终表（取值均解析自下发 CSS；正文 4.5:1 / UI 边界 3:1）===")
rows = [
    ("退出按钮文字", "改前：白底 + 淡蓝字 #e8eefb", hexrgb("e8eefb"), (255, 255, 255), 4.5),
    ("退出按钮文字", f"改后：深蓝底 + 白字 α={btn_fg_a}", over(W, btn_fg_a, BAR_R), BAR_R, 4.5),
    ("退出按钮文字", "改后（渐变左端更亮）", over(W, btn_fg_a, BAR_L), BAR_L, 4.5),
    ("退出按钮边框", f"改后：α={btn_bd_a}", over(W, btn_bd_a, BAR_R), BAR_R, 3.0),
    ("用户名", "改后：#fff", W, BAR_R, 4.5),
    ("角色名", "改前：#b9c9df", hexrgb("b9c9df"), BAR_R, 4.5),
    ("角色名", f"改后：α={role_a}", over(W, role_a, BAR_R), BAR_R, 4.5),
    ("头像首字", "改后：白字 / 半透明白底", W, over(W, ava_bg_a, BAR_R), 4.5),
    ("头像边框", f"改后：α={ava_bd_a}", over(W, ava_bd_a, BAR_R), BAR_R, 3.0),
]
print(f"  {'元素':14}{'取值':32}{'对比度':>10}{'阈值':>8}  判定")
bad = 0
for el, desc, fg, bg, thr in rows:
    r = ratio(fg, bg)
    ok = r >= thr
    bad += 0 if ok else 1
    print(f"  {el:14}{desc:32}{r:8.2f}:1{thr:7.1f}  {'✓ 达标' if ok else '✗ 不达标'}")
sep = ratio(over(W, box_bd_a, BAR_R), BAR_R)
print(f"\n  用户区分隔线 α={box_bd_a} = {sep:.2f}:1 —— 纯装饰性分组线，不承载信息，"
      f"按 WCAG 1.4.11 豁免")
print(f"  文字/边界不达标项：{bad} 项")

print("\n=== 3. 双自检 ===")
a = subprocess.run([sys.executable, "tests/regression.py"], capture_output=True)
oa = (a.stdout or b"").decode("utf-8", "ignore")
print("  regression.py   :", "全通过" if "全部通过" in oa else "失败")
if "全部通过" not in oa:
    print(oa[-900:])
b = subprocess.run([r"C:\Users\Lenovo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe",
                    "tests/render_check.mjs"], capture_output=True)
ob = (b.stdout or b"").decode("utf-8", "ignore")
print("  render_check.mjs:", "全通过" if "全部通过" in ob else "失败")
if "全部通过" not in ob:
    print(ob[-1200:])
else:
    sec = ob.split("=== 退出登录确认 ===")[-1].split("=" * 20)[0]
    n = sum(1 for line in sec.splitlines() if "✓" in line)
    print(f"  其中「退出确认」断言 {n} 项全过")

print("\n=== 4. 结构与语法 ===")
nc = re.sub(r"/\*[\s\S]*?\*/", "", css)
print("  CSS 花括号 %d/%d 平衡=%s" % (nc.count("{"), nc.count("}"), nc.count("{") == nc.count("}")))
print("  HTML <div> %d/%d" % (len(re.findall(r"<div\b", html)), len(re.findall(r"</div>", html))))
stuck = [l for l in css.splitlines() if l.count("{") > 1 and l.strip().startswith(".")]
print("  同一行粘连多条规则:", stuck or "无")
node = r"C:\Users\Lenovo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
for f in ["static/app.js", "static/login.js", "tests/render_check.mjs"]:
    r = subprocess.run([node, "--check", f], capture_output=True)
    print(f"  {f:26} {'OK' if r.returncode == 0 else 'FAIL'}")
for f in ["main.py", "config.py", "services/ocr_service.py", "tests/regression.py"]:
    r = subprocess.run([sys.executable, "-m", "py_compile", f], capture_output=True)
    print(f"  {f:26} {'OK' if r.returncode == 0 else 'FAIL'}")
