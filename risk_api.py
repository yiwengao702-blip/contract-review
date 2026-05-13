# 文件位置: contract-review/risk_api.py
# 功能说明: 电商风控子系统 REST 接口（FastAPI APIRouter，挂载于 /api/risk）。
#          涵盖：事件接入、仿真回溯、案件复核、规则启停、黑白名单维护、态势大盘、
#          用户画像、判定历史、示例事件加载与运行态复位（管理员）。
#          角色约束：建案/复核/名单/规则配置等敏感操作要求调用方声明操作人（演示环境
#          会话存于浏览器 localStorage，由前端把 display_name 带上来，与合同审查保持一致）。
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import cfg
from core.risk_pipeline import risk_engine
from services.list_manager import list_manager
from services.risk_rule_engine import risk_rule_engine

router = APIRouter(prefix="/api/risk", tags=["risk"])


# ---------------------------------------------------------------------- #
# 请求体模型
# ---------------------------------------------------------------------- #
class IngestPayload(BaseModel):
    events: Optional[List[dict]] = None   # 批量接入
    event: Optional[dict] = None          # 单条接入


class SimulatePayload(BaseModel):
    event: dict


class ReviewPayload(BaseModel):
    reviewer: str = ""
    action: str                          # approve / reject / close
    note: str = ""
    disposition: str = ""


class ListAddPayload(BaseModel):
    list_type: str
    dimension: str
    value: str
    reason: str = ""
    added_by: str = "system"


class ListRemovePayload(BaseModel):
    list_type: str
    dimension: str
    value: str


# ---------------------------------------------------------------------- #
# 事件接入
# ---------------------------------------------------------------------- #
@router.post("/events")
def ingest_events(payload: IngestPayload):
    """接入一条或多条电商业务事件，返回逐条判定结果与被跳过的原因。"""
    raws: List[dict] = []
    if payload.events:
        raws.extend(payload.events)
    if payload.event:
        raws.append(payload.event)
    if not raws:
        raise HTTPException(status_code=400, detail="events 与 event 不能同时为空")

    results = []
    skipped = []
    for i, raw in enumerate(raws):
        try:
            results.append(risk_engine.ingest(raw))
        except ValueError as e:
            skipped.append({"index": i, "reason": str(e)})
    return {"results": results, "skipped": skipped, "accepted": len(results)}


@router.post("/simulate")
def simulate_event(payload: SimulatePayload):
    """仿真单条事件：返回完整判定链路（逐条规则命中明细），但不写运行态。"""
    try:
        return risk_engine.simulate(payload.event)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/events")
def recent_events(limit: int = 100):
    """近期接入的事件列表（最新在前）。"""
    evs = risk_engine.events()
    return list(reversed(evs[-limit:]))


# ---------------------------------------------------------------------- #
# 案件
# ---------------------------------------------------------------------- #
@router.get("/cases")
def list_cases(status: str = ""):
    from services.case_manager import case_manager
    return [c.model_dump(mode="json") for c in
            case_manager.list_cases(status or None)]


@router.get("/cases/{case_id}")
def get_case(case_id: str):
    from services.case_manager import case_manager
    case = case_manager.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")
    return case.model_dump(mode="json")


@router.post("/cases/{case_id}/review")
def review_case(case_id: str, payload: ReviewPayload):
    from services.case_manager import case_manager
    if not payload.reviewer:
        raise HTTPException(status_code=400, detail="请填写复核人（用于留痕）")
    if payload.action not in ("approve", "reject", "close"):
        raise HTTPException(status_code=400, detail="action 必须为 approve / reject / close")
    try:
        case = case_manager.review(case_id, payload.reviewer, payload.action,
                                   payload.note, payload.disposition)
    except KeyError:
        raise HTTPException(status_code=404, detail="案件不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return case.model_dump(mode="json")


# ---------------------------------------------------------------------- #
# 规则
# ---------------------------------------------------------------------- #
@router.get("/rules")
def get_rules():
    rules = [r.model_dump(mode="json") for r in risk_rule_engine.list_rules()]
    # 分类聚合供前端分面展示
    cats: dict = {}
    for r in rules:
        c = r.get("category", "其他")
        cats.setdefault(c, {"name": c, "total": 0, "enabled": 0})
        cats[c]["total"] += 1
        if r.get("enabled"):
            cats[c]["enabled"] += 1
    return {"rules": rules, "facets": list(cats.values())}


@router.post("/rules/{rule_id}/toggle")
def toggle_rule(rule_id: str, payload: dict):
    enabled = bool(payload.get("enabled", True))
    if not risk_rule_engine.set_enabled(rule_id, enabled):
        raise HTTPException(status_code=404, detail=f"规则不存在：{rule_id}")
    return {"ok": True, "id": rule_id, "enabled": enabled}


class RuleAddPayload(BaseModel):
    id: str
    name: str
    category: str = "其他"
    scope: List[str] = []
    match: str = "all"
    conditions: List[dict] = []
    score: int = 0
    level: str = "medium"
    action: str = "review"
    enabled: bool = True
    advice: str = ""


@router.post("/rules")
def add_rule(payload: RuleAddPayload):
    """新增风控规则（追加写入 YAML，保留原有规则与注释）。"""
    try:
        rdef = risk_rule_engine.add_rule(payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "rule": rdef.model_dump(mode="json")}


@router.put("/rules/{rule_id}")
def update_rule(rule_id: str, payload: dict):
    """编辑规则的标量字段（name/score/level/action/advice/category）。"""
    allowed = {k: v for k, v in (payload or {}).items()
               if k in ("name", "category", "score", "level", "action", "advice")}
    if not allowed:
        raise HTTPException(status_code=400, detail="无可更新字段")
    if not risk_rule_engine.update_rule(rule_id, allowed):
        raise HTTPException(status_code=404, detail=f"规则不存在：{rule_id}")
    return {"ok": True, "id": rule_id}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: str):
    """删除规则（文本级块删除，保留其余内容）。"""
    if not risk_rule_engine.delete_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"规则不存在：{rule_id}")
    return {"ok": True}


# ---------------------------------------------------------------------- #
# 名单库
# ---------------------------------------------------------------------- #
@router.get("/lists")
def get_lists():
    entries = [e.model_dump(mode="json") for e in list_manager.list_all()]
    black = [e for e in entries if e["list_type"] == "black"]
    white = [e for e in entries if e["list_type"] == "white"]
    return {"black": black, "white": white, "total": len(entries)}


@router.post("/lists")
def add_list(payload: ListAddPayload):
    try:
        e = list_manager.add(payload.list_type, payload.dimension, payload.value,
                             payload.reason, payload.added_by)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    return {"ok": True, "entry": e.model_dump(mode="json")}


@router.delete("/lists")
def remove_list(payload: ListRemovePayload):
    ok = list_manager.remove(payload.list_type, payload.dimension, payload.value)
    if not ok:
        raise HTTPException(status_code=404, detail="名单记录不存在")
    return {"ok": True}


# ---------------------------------------------------------------------- #
# 态势大盘 / 画像 / 历史
# ---------------------------------------------------------------------- #
@router.get("/dashboard")
def dashboard():
    return risk_engine.analytics()


@router.get("/profiles")
def profiles():
    return [p.model_dump(mode="json") for p in risk_engine.list_profiles()]


@router.get("/profiles/{user_id}")
def profile_detail(user_id: str):
    p = risk_engine.get_profile(user_id)
    if not p:
        raise HTTPException(status_code=404, detail="用户画像不存在")
    return p.model_dump(mode="json")


@router.get("/history")
def history(limit: int = 100):
    return list(reversed(risk_engine.history()[-limit:]))


# ---------------------------------------------------------------------- #
# 示例事件（演示用）
# ---------------------------------------------------------------------- #
@router.get("/samples")
def get_samples():
    """返回内置示例事件（含典型作弊对抗场景），供前端一键加载演示。"""
    p = cfg.RISK_SAMPLE_DIR / "events.json"
    if not p.exists():
        return {"events": []}
    data = json.loads(p.read_text(encoding="utf-8")) or {}
    return {"events": data.get("events", [])}


@router.post("/samples/load")
def load_samples():
    """把内置示例事件全部接入引擎（演示用，会建案/落历史）。"""
    p = cfg.RISK_SAMPLE_DIR / "events.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="示例事件文件缺失")
    data = json.loads(p.read_text(encoding="utf-8")) or {}
    raws = data.get("events", [])
    results, skipped = [], []
    for raw in raws:
        try:
            results.append(risk_engine.ingest(raw))
        except ValueError as e:
            skipped.append({"reason": str(e)})
    return {"accepted": len(results), "skipped": len(skipped),
            "blocked": sum(1 for r in results
                           if r.get("decision", {}).get("action") == "block"),
            "review": sum(1 for r in results
                          if r.get("decision", {}).get("action") == "review")}


# ---------------------------------------------------------------------- #
# 运行态复位（管理员）
# ---------------------------------------------------------------------- #
@router.post("/reset")
def reset_engine(payload: dict = None):
    risk_engine.reset()
    return {"ok": True, "message": "运行态已清空（近期事件 / 画像 / 判定历史）"}
