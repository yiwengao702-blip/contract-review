# 文件位置: contract-review/tests/risk_regression.py
# 功能说明: 电商风控子系统端到端回归自检（进程内，不依赖 HTTP 服务）。
#          覆盖：事件解析、特征计算、规则评分与决策仲裁、名单优先级、自动建案与复核闭环、
#          仿真不污染运行态、态势大盘统计。可直接 `python tests/risk_regression.py` 运行。
import json
import os
import sys

# 让脚本在 tests/ 下也能 import 项目根模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.risk_models import DecisionAction, EventType, RiskLevel
from core.risk_pipeline import risk_engine
from services.event_parser import parse_event, parse_events
from services.list_manager import list_manager
from services.case_manager import case_manager

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def reset_state():
    risk_engine.reset()
    # 清空名单（保留持久文件前的测试隔离：仅清内存，不影响落盘其他数据）
    for e in list_manager.list_all():
        list_manager.remove(e.list_type, e.dimension, e.value)


# ---------------------------------------------------------------------- #
print("【1】事件解析")
reset_state()
ev = parse_event({"event_type": "register", "user_id": "U1", "timestamp": "2026-09-21 10:00:00"})
check("合法事件可解析", ev.event_type == EventType.REGISTER and ev.user_id == "U1")
bad = parse_events([{"event_type": "unknown", "user_id": "X"},
                    {"event_type": "login", "user_id": "Y"}])
check("未知类型被跳过", len(bad[0]) == 1 and bad[1][0]["reason"])
try:
    parse_event({"event_type": "login"})
    check("缺 user_id 抛错", False)
except ValueError:
    check("缺 user_id 抛错", True)

# ---------------------------------------------------------------------- #
print("【2】特征计算（设备农场聚集度）")
reset_state()
# 同一设备 DEV-X 上注册 3 个账号，第 3 个的 device_bind_count 应 >= 3
for i, u in enumerate(["UA", "UB", "UC"], 1):
    risk_engine.ingest({
        "event_type": "register", "user_id": u, "timestamp": "2026-09-21 10:0%d:00" % i,
        "device": {"device_id": "DEV-X", "ip": "1.1.1.%d" % i},
    })
# 重新模拟 UC 的特征：用 simulate 取特征
sim = risk_engine.simulate({
    "event_type": "register", "user_id": "UC", "timestamp": "2026-09-21 10:09:00",
    "device": {"device_id": "DEV-X", "ip": "1.1.1.9"},
})
check("设备聚集度(device_bind_count)>=3", sim["feature"]["device_bind_count"] >= 3)

# ---------------------------------------------------------------------- #
print("【3】规则评分与决策仲裁（示例事件集）")
reset_state()
sample_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "risk_samples", "events.json")
with open(sample_path, encoding="utf-8") as f:
    sample = json.load(f)["events"]
results = [risk_engine.ingest(e) for e in sample]
decisions = {r["event"]["event_id"]: r["decision"]["action"] for r in results}
# 设备农场 + 秒拨应被拦截
check("秒拨注册被拦截(E-FARM-01)", decisions["E-FARM-01"] == "block")
check("模拟器下单被拦截(E-EMU-01)", decisions["E-EMU-01"] == "block")
# 正常老账号全链路放行
check("正常用户注册放行(E-NORMAL-01)", decisions["E-NORMAL-01"] == "pass")
check("正常用户支付放行(E-NORMAL-04)", decisions["E-NORMAL-04"] == "pass")
# 暴力登录第 5 次起转人审
check("暴力登录转人审(E-BRUTE-05)", decisions["E-BRUTE-05"] == "review")

# ---------------------------------------------------------------------- #
print("【4】名单优先级（黑名单拦截 / 白名单放行）")
reset_state()
list_manager.add("black", "user", "BADGUY", "测试黑名单")
r_black = risk_engine.ingest({"event_type": "login", "user_id": "BADGUY",
                              "device": {"device_id": "D1", "ip": "2.2.2.2"}})
check("黑名单命中直接拦截", r_black["decision"]["action"] == "block" and r_black["list_hit"] == "black")
list_manager.remove("black", "user", "BADGUY")
list_manager.add("white", "user", "GOODGUY", "内部账号")
# 该用户即使触发高危规则也应放行
r_white = risk_engine.ingest({"event_type": "marketing", "user_id": "GOODGUY",
                              "device": {"device_id": "D2", "ip": "3.3.3.3", "is_ip_rotation": True},
                              "payload": {"activity_id": "X"}})
check("白名单命中免审放行", r_white["decision"]["action"] == "pass" and r_white["list_hit"] == "white")
list_manager.remove("white", "user", "GOODGUY")
list_manager.remove("black", "user", "BADGUY")

# ---------------------------------------------------------------------- #
print("【5】自动建案与复核闭环")
reset_state()
r = risk_engine.ingest({"event_type": "login", "user_id": "RISK1",
                        "device": {"device_id": "DR", "ip": "4.4.4.4", "is_ip_rotation": True}})
check("中高风险自动建案", r["case_id"] is not None)
case = case_manager.get_case(r["case_id"])
check("案件初始为待复核", case.status.value == "pending")
case_manager.review(r["case_id"], "周明远", "approve", "设备已封禁", "已加入黑名单")
case2 = case_manager.get_case(r["case_id"])
check("复核后状态变更", case2.status.value == "approved" and case2.reviewer == "周明远")

# ---------------------------------------------------------------------- #
print("【6】仿真不污染运行态")
reset_state()
before = len(risk_engine.history())
risk_engine.simulate({"event_type": "order", "user_id": "SIM", "device": {"device_id": "DS", "ip": "5.5.5.5"}})
after = len(risk_engine.history())
check("仿真不写判定历史", before == after)

# ---------------------------------------------------------------------- #
print("【7】态势大盘统计")
reset_state()
for e in sample:
    risk_engine.ingest(e)
an = risk_engine.analytics()
check("大盘统计总数=示例数", an["total"] == len(sample))
check("拦截率=拦截数/总数", abs(an["intercept_rate"] - round(an["block"] / an["total"] * 100, 1)) < 0.01)

# ---------------------------------------------------------------------- #
print(f"\n结果：通过 {PASS} / 失败 {FAIL}")
sys.exit(1 if FAIL else 0)
