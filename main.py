# 文件位置: contract-review/main.py
# 功能说明: FastAPI 服务入口。
#          提供合同审查的 REST 接口与 SSE 流式审查通道，并托管前端静态页面。
#          主要端点：
#            GET  /                        前端首页
#            POST /api/tasks/upload        上传合同（自动建任务并触发审查）
#            GET  /api/tasks               任务列表
#            GET  /api/tasks/{id}          任务详情
#            POST /api/tasks/{id}/run      执行审查（同步）
#            GET  /api/tasks/{id}/stream   SSE 流式审查进度
#            POST /api/tasks/{id}/retry    阻塞任务重试
#            POST /api/tasks/{id}/writeback 回写审批系统评论区
#            GET  /api/files/{id}          下载原始上传文件
#            GET  /api/ocr/probe           探测本机可用的 OCR 引擎
#            POST /api/ocr/recognize       拍照/图片 OCR 识别（支持多张照片，逐页返回）
#            POST /api/ocr/submit          提交校对后的逐页正文，建立审查任务
#            GET  /api/export/config       失败外发：通道状态 + 生效条件 + 选项元数据
#            POST /api/export/condition    失败外发：更新触发条件
#            GET  /api/export/queue        失败外发：审核队列、统计与审计日志
#            POST /api/export/scan         失败外发：手动扫描失败任务入队
#            POST /api/export/{id}/approve 失败外发：审核通过并推送钉钉
#            POST /api/export/{id}/reject  失败外发：驳回（必须写理由）
#            POST /api/export/{id}/retry   失败外发：推送失败后重试
#            POST /api/export/test         失败外发：发送通道连通性测试消息
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import List

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import cfg
from core.review_pipeline import ReviewPipeline
from services.audit_center import AuditCenter
from services.dingtalk import dingtalk
from services.approval_writeback import approval_writeback
from services.export_queue import export_queue
from services.legal_library import legal_library
from services.failure_gate import FAILURE_KINDS, SOURCES
from services.ocr_service import (
    IMAGE_SUFFIXES,
    PAGE_SEPARATOR,
    PageOCR,
    assemble_batch,
    ocr_service,
)
# 电商业务事件风控子系统接口（挂载于 /api/risk）
from risk_api import router as risk_router

app = FastAPI(title="合同审批审查系统", version="1.0.0")
# 合同审查 + 电商风控同属一个项目，统一挂载在同一服务下
app.include_router(risk_router)
pipeline = ReviewPipeline()


# ---------------------------------------------------------------------- #
# 演示种子数据：启动时注入一批虚拟审查任务，让大盘统计卡/筛选器/列表开箱即有内容
# ---------------------------------------------------------------------- #
def _seed_demo_tasks() -> None:
    """向流水线注入虚拟审查任务（仅内存，重启即按此重建）。

    设计约束：
    - 覆盖不同业务类型 / 金额量级 / 风险等级，统计卡四项都有非零数据可看；
    - 完成态任务的 updated_at 取近几小时，保证「本周审结」按周统计恒有值；
    - 只种 completed / pending，绝不种 blocked——失败外发扫描只认 blocked，
      种了会污染导出审核队列，干扰自检断言。
    """
    from core.models import (
        ContractMeta, ReviewReport, ReviewTask, RiskFinding,
        RiskLevel, TaskStatus, WritebackStatus,
    )

    def _ago(hours: float) -> str:
        """生成「几小时前」的时间串：种子数据统一落在近两天内。"""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - hours * 3600))

    def _finding(fid: str, level: RiskLevel, category: str, quote: str,
                 reason: str, basis: str, rec: str = "") -> RiskFinding:
        """构造一条风险发现（规则来源，演示用）。"""
        return RiskFinding(finding_id=fid, level=level, category=category, quote=quote,
                           reason=reason, legal_basis=basis, recommendation=rec, source="rule")

    def _task(idx: int, filename: str, ctype: str, hours: float, level: RiskLevel,
              summary: str, findings: list, amount=None, party_a="", party_b="",
              status: TaskStatus = TaskStatus.COMPLETED) -> ReviewTask:
        """构造一条虚拟任务：完成态带报告与风险清单，pending 态不带报告。"""
        now = _ago(hours)
        t = ReviewTask(
            task_id=f"TDEMO{idx:04d}",
            filename=filename,
            contract_type=ctype,
            status=status,
            writeback_status=WritebackStatus.NOT_WRITTEN,
            created_at=now,
            updated_at=now,
            contract_no=f"HT-2026-{idx:04d}",
            applicant_dept=["采购部", "技术部", "行政部", "市场部"][idx % 4],
            applicant=["李思齐", "周明远", "王芳", "赵磊"][idx % 4],
        )
        if status is TaskStatus.COMPLETED:
            t.meta = ContractMeta(party_a=party_a, party_b=party_b, amount=amount,
                                  currency="元", duration="12 个月")
            t.report = ReviewReport(contract_name=filename.rsplit(".", 1)[0],
                                    contract_type=ctype, overall_level=level,
                                    summary=summary, findings=findings)
        return t

    seeds = [
        _task(1, "城市轨道交通设备采购合同.docx", "采购", 2.5, RiskLevel.MEDIUM,
              "付款节奏与交付节奏基本匹配，但预付款比例偏高且缺乏预付款担保，建议补充。",
              [_finding("F1-1", RiskLevel.MEDIUM, "付款条件",
                        "合同签订后 10 日内支付预付款 40%",
                        "预付款比例达 40% 且未约定担保，资金占用与回收风险偏高",
                        "《民法典》第五百八十六条", "预付款降至 30% 并要求等额银行保函"),
               _finding("F1-2", RiskLevel.LOW, "违约责任",
                        "逾期交付的，每日按合同总额 0.05% 支付违约金",
                        "违约金上限未约定封顶值", "《民法典》第五百八十五条",
                        "补充「违约金累计不超过合同总额 10%」")],
              amount=12800000, party_a="中恒建设集团有限公司", party_b="华轨装备制造股份有限公司"),
        _task(2, "软件开发服务合同.docx", "服务", 6.0, RiskLevel.HIGH,
              "成果知识产权归属未作约定，且验收标准模糊，属本单最高风险项。",
              [_finding("F2-1", RiskLevel.HIGH, "知识产权",
                        "开发成果的知识产权归属双方另行协商",
                        "知识产权归属悬而未决，交付后易衍生权属纠纷",
                        "《民法典》第八百四十三条", "明确约定开发成果知识产权归甲方所有"),
               _finding("F2-2", RiskLevel.MEDIUM, "保密义务",
                        "双方对合作中知悉的信息负有保密义务",
                        "保密义务未约定期限与违约责任", "《民法典》第五百零一条",
                        "约定保密期限为合同终止后 5 年并单列违约金"),
               _finding("F2-3", RiskLevel.LOW, "条款完整性",
                        "缺少必备条款：验收标准",
                        "验收标准未量化，付款节点难以对齐", "",
                        "补充按模块验收清单与验收期")],
              amount=3560000, party_a="蓝湾数据科技有限公司", party_b="云梯软件开发有限公司"),
        _task(3, "厂房租赁合同.pdf", "租赁", 26.0, RiskLevel.LOW,
              "租赁要素齐备，仅免责任条款表述笼统，整体风险可控。",
              [_finding("F3-1", RiskLevel.LOW, "免责条款",
                        "因不可抗力造成损失的，双方互不承担责任",
                        "免责范围未列举具体情形，执行口径易起争议",
                        "《民法典》第一百八十条", "列举地震/洪水等具体免责任情形")],
              amount=920000, party_a="启润实业发展有限公司", party_b="恒力精密制造有限公司"),
        _task(4, "市场营销推广服务协议.docx", "服务", 49.0, RiskLevel.HIGH,
              "违约金比例明显过高（35%），且约定管辖对己方不利，需法务重点跟进。",
              [_finding("F4-1", RiskLevel.HIGH, "违约责任",
                        "违约金比例为合同总额的 35%",
                        "违约金畸高，司法实践中可能被调整，且对等性缺失",
                        "《民法典》第五百八十五条", "降至 20% 以内并设置双向对等"),
               _finding("F4-2", RiskLevel.MEDIUM, "争议管辖",
                        "争议由乙方所在地人民法院管辖",
                        "管辖地对己方不利", "《民事诉讼法》第三十五条",
                        "改为原告所在地或合同签订地管辖")],
              amount=2400000, party_a="星野传媒集团有限公司", party_b="卓航广告有限公司"),
        _task(5, "生产设备维保框架合同.docx", "采购", 53.0, RiskLevel.MEDIUM,
              "账期 120 天偏长，且相对方资质核验项缺失，建议签约前补齐。",
              [_finding("F5-1", RiskLevel.MEDIUM, "付款条件",
                        "验收合格后 120 日内一次性支付",
                        "账期超 90 天，占压供应商资金，易引发履约摩擦",
                        "《保障中小企业款项支付条例》第八条", "账期缩短至 60 日"),
               _finding("F5-2", RiskLevel.MEDIUM, "主体资质",
                        "缺失字段：credit_code",
                        "乙方统一社会信用代码未披露，主体核验不完整",
                        "《公司法》第三十二条", "签约前核验并写入合同首部")],
              amount=6880000, party_a="北方重工集团有限公司", party_b="迅捷机电设备工程有限公司"),
        _task(6, "跨省物流运输合同.pdf", "物流", 70.0, RiskLevel.LOW,
              "运输要素完整，货物保险与赔偿上限已约定，风险可控。",
              [_finding("F6-1", RiskLevel.LOW, "保险条款",
                        "货物灭失按每公斤 30 元限额赔偿",
                        "赔偿限额低于货物实际价值，建议按声明价值投保",
                        "《民法典》第八百三十三条", "改为按声明价值全额赔付")],
              amount=1350000, party_a="中远联运物流有限公司", party_b="顺捷货运代理有限公司"),
        # 一条待审任务：让「待审任务」统计卡有数；pending 不带报告，列表综合风险显示 —
        _task(7, "数据服务框架协议.docx", "服务", 0.5, RiskLevel.LOW,
              "", [], status=TaskStatus.PENDING),
    ]
    for t in seeds:
        pipeline._tasks[t.task_id] = t


_seed_demo_tasks()
# 审计中心：台账 / 相对方画像 / 风险洞察 / 黑名单。
# 传入 list_tasks 而非任务副本——它必须从流水线读当前任务，否则统计会停在启动那一刻。
audit_center = AuditCenter(task_provider=pipeline.list_tasks)

# 挂载静态资源
app.mount("/static", StaticFiles(directory=str(cfg.STATIC_DIR)), name="static")


# ---------------------------------------------------------------------- #
# 首页与静态资源
# ---------------------------------------------------------------------- #
# 入口 HTML 一律 no-cache（每次回源校验，命中 304 也几乎零成本）。
# 原因：托管环境前面有一层边缘网关，源站若不给 Cache-Control，网关会按自己的
# 默认 TTL 缓存裸路径 `/`。于是"重新发布后页面还是旧的"——静态资源因为带
# ETag 会走 RefreshHit 回源校验所以是新的，唯独 HTML 被当成长期缓存。
# HTML 是入口文档，它变更意味着整站功能变了，绝不能让它停留在旧副本上。
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.middleware("http")
async def no_stale_cache(request, call_next):
    """给所有未显式声明 Cache-Control 的响应补上 no-cache。

    为什么必须统一加，而不只是给 HTML 加：静态资源同样会被边缘网关按默认 TTL
    缓存。只加 HTML 的结果是线上出现「新 HTML + 旧 JS」的混合态——页面里有新增的
    容器节点，脚本却还是旧版、从不填充它，功能静默失效且不报错，比白屏更难排查。
    这类「资源版本不一致」只能靠让每个入口都回源校验来根治。

    no-cache 不是不缓存，而是每次回源校验；命中 304 时几乎零开销，
    对本项目的静态资源体量来说代价可忽略，换来的是「发布即生效」。
    """
    resp = await call_next(request)
    if not any(k.lower() == "cache-control" for k in resp.headers):
        resp.headers["Cache-Control"] = _NO_CACHE["Cache-Control"]
    return resp


# ---------------------------------------------------------------------- #
# [融合] 接入 sakura_base 共享底座的鉴权，替代原先散落的 SESSIONS 字典 + auth_guard。
# 此前 /api/login 虽会下发 token，但所有业务接口从未校验它（P0 级越权）；
# Phase0 已就地补洞，现进一步收敛为底座单源实现，四项目共用同一份鉴权代码。
try:
    from sakura_base.auth import sessions as sa_sessions
    from sakura_base.auth.middleware import PathAuthMiddleware
except ImportError as _imp_err:  # 提示先安装共享底座
    raise RuntimeError(
        "contract-review 现已依赖 sakura_base 共享底座，请先安装：\n"
        "  pip install -e <sakura_platform>/packages/sakura_base[web]\n"
        f"原始错误：{_imp_err}"
    ) from _imp_err

# 共享会话存储（令牌模式）：替代原先散落的 SESSIONS 字典
_session_store = sa_sessions.SessionStore()


# 对所有 /api/*（登录接口、静态资源、页面路由除外）强制校验 Bearer 令牌；
# 令牌在 do_login 时经 _session_store.issue 签发，/api/logout 或重启后失效。
app.add_middleware(
    PathAuthMiddleware,
    protected_prefixes=("/api/",),
    public_paths={"/", "/index.html", "/login", "/login.html", "/api/login", "/api/register", "/static/", "/risk", "/risk.html"},
    sessions=_session_store,  # 与 do_login 签发使用同一存储，否则令牌校验对不上
)


@app.get("/")
def index():
    return FileResponse(str(cfg.STATIC_DIR / "index.html"), headers=_NO_CACHE)


@app.get("/index.html")
def index_alias():
    """与 `/` 完全等价的入口别名（同一份 HTML）。

    存在的唯一理由是**边缘缓存的过渡窗口**：网关已经缓存了旧版 `/` 且
    客户端无法强制回源（实测 `Cache-Control: no-cache` 请求头、HEAD、POST
    都打不穿），只能等它自己的 TTL 到期。而 HTML 的变更意味着整站功能变了，
    这段时间里分享出去的裸链接会一直停在旧版本——对方看到的页面缺功能，
    比报错更难排查。

    换一个从未被缓存过的路径，就绕开了这个窗口：新 cache key 首次必然回源，
    拿到 `no-cache, must-revalidate` 之后每次都会回源校验，不会再变旧。
    `/` 自身在本轮缓存过期后也会恢复（源站已带头），此别名长期保留也无副作用。
    """
    return FileResponse(str(cfg.STATIC_DIR / "index.html"), headers=_NO_CACHE)


# ---------------------------------------------------------------------- #
# 电商业务事件风控子系统前端入口
# ---------------------------------------------------------------------- #
@app.get("/risk")
def risk_page():
    """电商风控工作台（态势大盘 / 审核工作台 / 规则配置 / 名单库 / 仿真测试）。"""
    return FileResponse(str(cfg.STATIC_DIR / "risk.html"), headers=_NO_CACHE)


@app.get("/risk.html")
def risk_page_alias():
    """与 /risk 等价的入口别名（绕开边缘缓存过渡窗口，同 /index.html 思路）。"""
    return FileResponse(str(cfg.STATIC_DIR / "risk.html"), headers=_NO_CACHE)


# ---------------------------------------------------------------------- #
# 登录（本地演示环境：三种角色权限不同）
# ---------------------------------------------------------------------- #
# 演示账号表：登录邮箱（姓名全拼） -> 密码 / 角色 / 角色名 / 姓名
# 说明：以企业邮箱作为登录账号更贴近真实企业系统；邮箱大小写不敏感（统一转小写匹配）。
#       密码取姓名全拼，与邮箱前缀一致，便于演示时记忆（如 chenjingzhi@gmail.com / chenjingzhi）。
#       真实生产应接入企业统一身份（SSO / LDAP / OAuth），此处为离线演示用固定账号。
DEMO_ACCOUNTS = {
    "chenjingzhi@gmail.com":  {"password": "chenjingzhi",  "role": "admin", "role_name": "平台管理员",   "display_name": "陈敬之"},
    "zhoumingyuan@gmail.com": {"password": "zhoumingyuan", "role": "legal", "role_name": "法务合规经理", "display_name": "周明远"},
    "lisiqi@gmail.com":       {"password": "lisiqi",       "role": "biz",   "role_name": "采购业务经办", "display_name": "李思齐"},
}

# 自助注册用户持久化文件（演示环境用，明文存储仅作离线演示；生产应接入企业统一身份并哈希口令）。
# 与 DEMO_ACCOUNTS 合并构成完整账号表；注册账号默认最小权限角色「采购业务经办」。
REGISTERED_USERS_FILE = Path(__file__).resolve().parent / "data" / "users.json"
# 用可重入锁：do_register 在持有锁的上下文内调用 _save_registered_user，需允许同一线程重复加锁，
# 否则会自死锁（普通 threading.Lock 不可重入）导致注册请求永久挂起。
_REGISTERED_LOCK = threading.RLock()


def _load_registered_users() -> dict:
    if not REGISTERED_USERS_FILE.exists():
        return {}
    try:
        with open(REGISTERED_USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_registered_user(account: str, info: dict) -> None:
    with _REGISTERED_LOCK:
        users = _load_registered_users()
        users[account] = info
        REGISTERED_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(REGISTERED_USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)


def find_user(account: str) -> dict | None:
    """在演示账号表与已注册用户表中查找账号（邮箱统一小写匹配）。"""
    account = str(account or "").strip().lower()
    if account in DEMO_ACCOUNTS:
        return DEMO_ACCOUNTS[account]
    return _load_registered_users().get(account)


@app.get("/login")
def login_page():
    """登录页（左侧品牌区 + 右侧登录卡）。"""
    return FileResponse(str(cfg.STATIC_DIR / "login.html"), headers=_NO_CACHE)


@app.post("/api/login")
def do_login(payload: dict):
    """校验演示账号并返回会话信息。

    token 为演示用随机串；前端写入 localStorage 作为会话凭证。
    """
    # 邮箱账号统一小写再比对，避免用户输入 ChenJingZhi@Gmail.com 这类大小写差异导致登录失败
    account = str(payload.get("account") or "").strip().lower()
    # 密码去掉首尾空白：演示时从演示账号行复制粘贴容易带空格
    password = str(payload.get("password") or "").strip()
    user = find_user(account)
    if not user or user["password"] != password:
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    # [融合] 经共享会话存储签发令牌，替代原手写 SESSIONS 字典
    sess = _session_store.issue(account, user["role"], ttl=3600)
    token = sess.token
    return {
        "ok": True,
        "token": token,
        "account": account,
        "role": user["role"],
        "role_name": user["role_name"],
        "display_name": user["display_name"],
    }


@app.post("/api/logout")
def do_logout(request: Request):
    """吊销当前会话令牌（前端同时清除 localStorage 中的会话）。"""
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
    _session_store.revoke(token or "")
    return {"ok": True}


# 角色白名单：自助注册仅允许在三个已知角色中选择，杜绝越权自定义角色
ROLE_NAMES = {"admin": "平台管理员", "legal": "法务合规经理", "biz": "采购业务经办"}


@app.post("/api/register")
def do_register(payload: dict):
    """自助注册：新建账号并写入持久化用户表，注册成功即签发会话自动登录。

    角色由注册表单的「角色下拉框」选择，后端按白名单校验：非法或缺失一律回落为
    最小权限的「采购业务经办」。演示环境明文存储口令，生产应接企业统一身份并哈希。
    """
    account = str(payload.get("account") or "").strip().lower()
    password = str(payload.get("password") or "")
    display_name = str(payload.get("display_name") or "").strip()
    # 角色取白名单内的值，越界则回落 biz（最小权限），避免前端篡改越权
    role = str(payload.get("role") or "biz").strip().lower()
    if role not in ROLE_NAMES:
        role = "biz"

    # 基础校验：邮箱格式、密码强度、姓名非空
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", account):
        raise HTTPException(status_code=400, detail="请输入有效的企业邮箱")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    if not display_name:
        raise HTTPException(status_code=400, detail="请填写姓名")

    with _REGISTERED_LOCK:
        # 双重检查：演示账号与已注册用户均不允许重复注册
        if account in DEMO_ACCOUNTS or account in _load_registered_users():
            raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录")
        info = {
            "password": password,
            "role": role,
            "role_name": ROLE_NAMES[role],
            "display_name": display_name,
        }
        _save_registered_user(account, info)

    # 注册成功直接签发令牌，前端可像登录一样写入会话并进入主应用
    sess = _session_store.issue(account, info["role"], ttl=3600)
    token = sess.token
    return {
        "ok": True,
        "token": token,
        "account": account,
        "role": info["role"],
        "role_name": info["role_name"],
        "display_name": info["display_name"],
    }


@app.get("/api/files/{task_id}")
def get_file(task_id: str):
    task = pipeline.get_task(task_id)
    fp = cfg.UPLOAD_DIR / f"{task_id}{Path(task.filename).suffix}"
    if not fp.exists():
        raise HTTPException(status_code=404, detail="原始文件不存在")
    return FileResponse(str(fp), filename=task.filename)


# ---------------------------------------------------------------------- #
# 合规规则与模板（规则库配置页）
# ---------------------------------------------------------------------- #
# 「业务类型覆盖」分面的展示顺序；与前端上传弹窗的业务类型枚举保持一致。
BIZ_TYPE_FACETS = ["采购", "销售", "服务", "劳动", "租赁", "其他"]


def _read_yaml(path: Path) -> dict:
    """读取 YAML 配置；文件缺失时返回空字典而非抛异常。"""
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_rules() -> list:
    return _read_yaml(cfg.RULES_DIR / "review_rules.yaml").get("rules", []) or []


def _load_clauses() -> dict:
    return _read_yaml(cfg.RULES_DIR / "clause_library.yaml").get("clauses", {}) or {}


def _load_qualifications() -> list:
    return _read_yaml(cfg.RULES_DIR / "party_qualification.yaml").get("checks", []) or []


def _build_rule_facets(rules: list) -> dict:
    """按审查维度与业务类型聚合规则数量，供左侧两张分面卡展示。

    维度分面：按规则在 YAML 中首次出现的顺序排列（保持与规则表一致的观感），
              每条维度记录 高/中/低 风险条数与总条数；
    业务类型分面：统计 scope 中**显式列出**该类型的规则条数（"全部类型"规则不计入，
              因为点击某业务类型时应回答"这类合同有没有专属规则"）。
    """
    order: list = []
    dims: dict = {}
    for r in rules:
        name = r.get("category") or "其他"
        if name not in dims:
            dims[name] = {"name": name, "high": 0, "medium": 0, "low": 0, "total": 0}
            order.append(name)
        d = dims[name]
        lv = r.get("level")
        if lv in ("high", "medium", "low"):
            d[lv] += 1
        d["total"] += 1

    biz = [{"name": t, "total": sum(1 for r in rules if t in (r.get("scope") or []))}
           for t in BIZ_TYPE_FACETS]
    return {"dimensions": [dims[k] for k in order], "biz_types": biz}


def _set_rule_enabled(rule_id: str, enabled: bool) -> bool:
    """改写 YAML 中指定规则的 enabled 开关，返回是否命中。

    两个刻意的实现选择：
      ① 用「文本级精确替换」而非 yaml.safe_dump 整体回写 ——
         review_rules.yaml 顶部有整段字段说明注释、每条规则内部也有解释性注释，
         整体 dump 会把注释与字段顺序全部抹掉，规则库因此变得不可维护；
      ② 用字节读写而非 write_text ——
         Python 文本模式在 Windows 上会把 \\n 全部翻译成 \\r\\n，
         导致点一次开关就把整个文件的换行风格改掉（git 出现全文件 diff）。
    """
    path = cfg.RULES_DIR / "review_rules.yaml"
    text = path.read_bytes().decode("utf-8")
    pattern = re.compile(
        r"(?m)(^  - id: " + re.escape(rule_id) + r"\b[\s\S]*?\n    enabled: )(true|false)"
    )
    new_text, n = pattern.subn(
        lambda m: m.group(1) + ("true" if enabled else "false"), text, count=1
    )
    if n == 0:
        return False
    path.write_bytes(new_text.encode("utf-8"))
    # 规则变更即时生效：重载引擎，后续审查任务立刻按新开关执行
    pipeline.rule_engine.reload()
    return True


@app.get("/api/rules/config")
def get_rules_config():
    """规则库配置页全量数据（三个标签页 + 左侧分面 + 计数）。"""
    rules = _load_rules()
    clauses = _load_clauses()
    quals = _load_qualifications()

    clause_list = []
    for key, val in clauses.items():
        if isinstance(val, dict):
            clause_list.append({
                "key": key,
                "title": val.get("title") or key,
                "category": val.get("category") or "其他",
                "text": (str(val.get("text") or "")).strip(),
            })
        else:
            clause_list.append({"key": key, "title": key, "category": "其他",
                                "text": str(val).strip()})

    return {
        "rules": rules,
        "clauses": clause_list,
        "qualifications": quals,
        "facets": _build_rule_facets(rules),
        "counts": {
            "rules": len(rules),
            "clauses": len(clause_list),
            "qualifications": len(quals),
        },
    }


@app.get("/api/rules")
def get_rules():
    """兼容旧版调用：返回审查规则清单与标准示范条款库。"""
    return {"rules": _load_rules(), "clauses": _load_clauses()}


@app.post("/api/rules/{rule_id}/toggle")
def toggle_rule(rule_id: str, payload: dict):
    """切换单条规则的启用开关（写回 YAML 并重载规则引擎）。"""
    enabled = bool(payload.get("enabled", True))
    if not _set_rule_enabled(rule_id, enabled):
        raise HTTPException(status_code=404, detail=f"规则不存在：{rule_id}")
    return {"ok": True, "id": rule_id, "enabled": enabled}


@app.post("/api/rules/clauses")
def add_clause(payload: dict):
    """新增标准示范条款，追加写入 clause_library.yaml（保留原文件注释）。"""
    title = str(payload.get("title") or "").strip()
    category = str(payload.get("category") or "其他").strip()
    text = str(payload.get("text") or "").strip()
    if not title or not text:
        raise HTTPException(status_code=400, detail="条款名称与示范条款正文不能为空")

    key = str(payload.get("key") or "").strip() or ("custom_" + secrets.token_hex(4))
    if key in _load_clauses():
        raise HTTPException(status_code=400, detail=f"条款键已存在：{key}")

    # 以 YAML 序列化保证转义正确，再整体缩进 2 空格续写到 clauses: 之下
    block = yaml.safe_dump(
        {key: {"title": title, "category": category, "text": text}},
        allow_unicode=True, default_flow_style=False, sort_keys=False,
    )
    indented = "\n".join(("  " + ln) if ln.strip() else ln
                         for ln in block.rstrip("\n").split("\n"))
    path = cfg.RULES_DIR / "clause_library.yaml"
    # 同样走字节写入：避免文本模式把 \n 改写成 \r\n，破坏文件原有换行风格
    with open(path, "ab") as f:
        f.write(("\n" + indented + "\n").encode("utf-8"))

    # 重载条款库，新条款立刻可被风险建议推荐使用
    pipeline.clause_recommend.reload()
    return {"ok": True, "key": key, "title": title, "category": category}


@app.post("/api/rules/qualifications")
def add_qualification(payload: dict):
    """新增主体资质核验项，追加写入 party_qualification.yaml（保留原文件注释与顺序）。"""
    name = str(payload.get("name") or "").strip()
    category = str(payload.get("category") or "主体资质").strip()
    level = str(payload.get("level") or "medium").strip()
    basis = str(payload.get("basis") or "").strip()
    source = str(payload.get("source") or "").strip()
    if not name or not basis:
        raise HTTPException(status_code=400, detail="核验项名称与核验要点不能为空")

    # 自动生成下一个 PQ-00X 编号：避免与既有条目撞号
    existing = _load_qualifications()
    max_n = 0
    for q in existing:
        m = re.match(r"PQ-(\d+)", str(q.get("id") or ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    qid = payload.get("id") or ("PQ-%03d" % (max_n + 1))
    if any(str(q.get("id")) == qid for q in existing):
        raise HTTPException(status_code=400, detail=f"资质编号已存在：{qid}")

    # 以 YAML 序列化单个列表项，再整体缩进 2 空格续写到 checks: 之下
    block = yaml.safe_dump(
        [{"id": qid, "name": name, "category": category, "level": level,
          "enabled": True, "basis": basis, "source": source}],
        allow_unicode=True, default_flow_style=False, sort_keys=False,
    )
    indented = "\n".join(("  " + ln) if ln.strip() else ln
                         for ln in block.rstrip("\n").split("\n"))
    path = cfg.RULES_DIR / "party_qualification.yaml"
    with open(path, "ab") as f:
        f.write(("\n" + indented + "\n").encode("utf-8"))

    # 重载规则引擎：新资质核验项随 RULE-ENT-001 在后续审查中即时生效
    pipeline.rule_engine.reload()
    return {"ok": True, "id": qid, "name": name, "category": category, "level": level}


# ---------------------------------------------------------------------- #
# 拍照 / 图片识别（OCR）
# ---------------------------------------------------------------------- #
@app.get("/api/ocr/probe")
def ocr_probe(refresh: bool = False):
    """汇报各 OCR 引擎的可用性与修复指引。

    前端在打开拍照弹窗时就调用本接口，把"这台机器能不能自动识别"提前告诉用户，
    而不是等识别失败才发现没有引擎可用。

    refresh=1 表示忽略进程内缓存强制重新探测（界面上的「重新检测」按钮）。
    为什么需要它：引擎常常是"服务已经开着的时候"才装好的（下载安装包要几十 MB），
    若探测结果被永久缓存，用户装完会一直看到"未安装"，只能重启服务才能生效。
    探测本身只起一个子进程（约百毫秒），因此代价可以忽略，而"装完却说没装"的代价很大。
    """
    return ocr_service.probe(force=refresh)


@app.post("/api/ocr/recognize")
def ocr_recognize(file: List[UploadFile] = File(...)):
    """对上传的一到多张照片执行 OCR，返回逐页结果 + 按页码合并的正文。

    接口收的是"多张照片"而不是"多份合同"：纸质合同常有多页，一次拍/选齐再统一识别，
    比一页一页单独识别更接近真实用法。字段名仍是 file（可重复），
    因此只传一张时响应形状与旧版完全一致，老调用方无感。

    每页都会返回 attempts 逐条说明各引擎"为什么没试/试了为什么没出来"，
    让"识别不出来"从一句无从下手的失败，变成可判断、可操作的原因。
    """
    if len(file) > cfg.MAX_SCAN_PAGES:
        raise HTTPException(
            status_code=413,
            detail=f"一次最多识别 {cfg.MAX_SCAN_PAGES} 页，当前选择了 {len(file)} 张",
        )

    page_items: List[PageOCR] = []
    for idx, upload in enumerate(file, start=1):
        suffix = Path(upload.filename or "photo.jpg").suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx} 张不是受支持的图片格式：仅支持 {', '.join(sorted(IMAGE_SUFFIXES))}",
            )

        data = upload.file.read()
        if len(data) > cfg.MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"第 {idx} 张超出大小上限（{cfg.MAX_UPLOAD_BYTES // 1024 // 1024}MB）",
            )

        # 落盘中转：OCR 引擎与图片体检都按文件路径工作；识别完立即删除，不留垃圾
        tmp_path = cfg.UPLOAD_DIR / f"_ocr_{secrets.token_hex(6)}{suffix}"
        tmp_path.write_bytes(data)
        try:
            result = ocr_service.recognize(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        page_items.append(PageOCR(index=idx, filename=upload.filename or f"第{idx}页",
                                  bytes=len(data), result=result))

    return assemble_batch(page_items)


@app.post("/api/ocr/submit")
def ocr_submit(payload: dict):
    """提交人工校对后的识别正文，建立审查任务并立即执行。

    多页合并放在服务端做（而不是让前端拼字符串）：分隔符是"页码能否被正确还原"的
    关键约定，只应有一处定义，否则前后端一旦不一致，风险卡片就会指错页码。
    请求体支持两种形状：
      · pages: ["第1页正文", "第2页正文", ...]  多页（空白页自动剔除，不参与审查）
      · text:  "整篇正文"                       单页 / 兼容旧调用

    本接口刻意走「写成 .txt 再交给解析器」这条路，而不是新写一条审查入口：
    这样拍照识别与上传文档共享同一套闸门、规则引擎与报告装配，
    不会因为多了一个入口就出现两套行为不一致的审查逻辑。
    """
    pages = payload.get("pages")
    if isinstance(pages, list):
        texts = [str(p).strip() for p in pages]
        # 空白页留位不剔页：纸质合同确实会有空白页/封面页，强行要求填写反而逼用户造内容；
        # 但也不能直接丢掉 —— 丢掉会让后面几页的页码整体前移，用户照报告里的
        # 「第 N 页」去翻自己拍的第 N 张照片时会对不上。保留分页符占位后，
        # 空页不贡献任何文字，却保住了它占的页次。
        text = PAGE_SEPARATOR.join(texts)
    else:
        text = str(payload.get("text") or "").strip()

    if not text.strip():
        raise HTTPException(status_code=400, detail="识别正文为空，无法提交审查")

    filename = str(payload.get("filename") or "拍照识别合同").strip()
    if isinstance(pages, list) and len(pages) > 1:
        # 文件名带上页数，便于在大盘里区分"拍照多页"与普通上传
        filename = f"{filename}·{len(pages)}页"
    contract_type = str(payload.get("contract_type") or "未分类")

    # source="scan"：标记正文来自拍照识别，规则引擎据此给"字段缺失"类告警加不确定性说明
    task = pipeline.create_task(filename=filename, contract_type=contract_type, source="scan")
    save_path = cfg.UPLOAD_DIR / f"{task.task_id}.txt"
    save_path.write_bytes(text.encode("utf-8"))
    pipeline.run(task.task_id, str(save_path))
    return task.model_dump(mode="json")


# ---------------------------------------------------------------------- #
# 任务接口
# ---------------------------------------------------------------------- #
@app.get("/api/tasks")
def list_tasks():
    return [t.model_dump(mode="json") for t in pipeline.list_tasks()]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    try:
        return pipeline.get_task(task_id).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在")


@app.post("/api/tasks/upload")
def upload_task(
    file: UploadFile = File(...),
    contract_type: str = Form("未分类"),
    auto_run: bool = Form(True),
    approval_instance_id: str = Form(""),
    contract_no: str = Form(""),
    applicant_dept: str = Form(""),
    applicant: str = Form(""),
):
    # 保存上传文件
    suffix = Path(file.filename or "file.bin").suffix
    task = pipeline.create_task(
        filename=file.filename or "未命名合同",
        contract_type=contract_type,
        approval_instance_id=(approval_instance_id or None),
        contract_no=(contract_no or None),
        applicant_dept=(applicant_dept or None),
        applicant=(applicant or None),
    )
    save_path = cfg.UPLOAD_DIR / f"{task.task_id}{suffix}"
    with open(save_path, "wb") as f:
        f.write(file.file.read())

    if auto_run:
        pipeline.run(task.task_id, str(save_path))
    return task.model_dump(mode="json")


@app.post("/api/tasks/{task_id}/run")
def run_task(task_id: str, file: UploadFile | None = File(None)):
    task = pipeline.get_task(task_id)
    # 若重新上传文件则覆盖
    if file is not None and file.filename:
        suffix = Path(file.filename).suffix
        save_path = cfg.UPLOAD_DIR / f"{task_id}{suffix}"
        with open(save_path, "wb") as f:
            f.write(file.file.read())
    else:
        # 使用已有文件
        existing = list(cfg.UPLOAD_DIR.glob(f"{task_id}.*"))
        if not existing:
            raise HTTPException(status_code=400, detail="无可用文件，请先上传")
        save_path = existing[0]
    result = pipeline.run(task_id, str(save_path))
    return result.model_dump(mode="json")


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str, file: UploadFile = File(...)):
    suffix = Path(file.filename or "file.bin").suffix
    save_path = cfg.UPLOAD_DIR / f"{task_id}{suffix}"
    with open(save_path, "wb") as f:
        f.write(file.file.read())
    result = pipeline.retry(task_id, str(save_path))
    return result.model_dump(mode="json")


@app.post("/api/tasks/{task_id}/reaffirm")
def reaffirm_task(task_id: str, payload: dict):
    """人工重申：闸门判定为「非合同 / 无法判定」时，由责任人签字放行进入条款审查。

    与 retry 的区别：retry 是「换一份文件重来」，reaffirm 是「文件不变，
    但人判断它就是合同（例如识别质量差导致的误判）」。因此这里不接收文件，
    直接复用任务原有的落盘原件；重申凭据（操作人 + 理由）与重申前的闸门
    判定结果一并写入任务，供报告与看板留痕。

    为什么必须留痕：闸门放行是绕过系统判定的动作。若不记录「谁、什么时候、
    为什么」放行，一次误放行在事后完全无法追溯，闸门也就失去意义。
    """
    operator = str(payload.get("operator") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if not operator:
        raise HTTPException(status_code=400, detail="请填写重申人（用于留痕）")
    if not reason:
        raise HTTPException(status_code=400, detail="请填写重申理由（用于留痕）")

    try:
        result = pipeline.reaffirm(task_id, operator=operator, reason=reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result.model_dump(mode="json")


@app.post("/api/tasks/{task_id}/writeback")
def writeback_task(task_id: str, payload: dict | None = None):
    data = payload or {}
    try:
        return pipeline.write_back(task_id, approval_instance_id=(data.get("approval_instance_id") or None))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------- #
# 审批系统对接配置（管理员）
#
# 与钉钉通道同构：界面可配、落盘 data/settings.json、环境变量优先级更高。
# 目的是让系统管理员能「配置审批流对接参数」而无需改代码或重启。
# ---------------------------------------------------------------------- #
@app.get("/api/approval/status")
def approval_status():
    """回写通道能力现状（管理员配置页渲染用）。"""
    return approval_writeback.status()


@app.post("/api/approval/channel")
def approval_update_channel(payload: dict):
    """保存审批系统对接配置（接口地址 / 令牌 / 路径 / 鉴权方案）。

    约定（同钉钉）：字段缺省（未出现在 payload）表示"这一项没动"，
    传空串表示"清空"，清空即回到模拟模式。
    """
    data = payload or {}

    def pick(key: str):
        return data[key] if key in data else None

    base = pick("base")
    token = pick("token")
    path = pick("comment_path")
    scheme = pick("auth_scheme")
    header = pick("auth_header")
    operator = str(data.get("operator") or "未署名")

    # 校验：选了真实接口但地址不像 http(s) 地址
    if base is not None and base.strip() and not base.strip().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="接口地址必须以 http:// 或 https:// 开头")

    try:
        save_settings(
            approval_api_base=(base.strip() if base else None),
            approval_api_token=(token.strip() if token else None),
            approval_comment_path=(path.strip() if path else None),
            approval_auth_scheme=(scheme.strip() if scheme else None),
            approval_auth_header=(header.strip() if header else None),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存审批对接配置失败：{exc}")

    # 同步到内存 cfg，使后续回写立即生效
    cfg.APPROVAL_API_BASE = (base.strip() if base else None) or None
    cfg.APPROVAL_API_TOKEN = (token.strip() if token else None) or None
    if path is not None:
        cfg.APPROVAL_COMMENT_PATH = path.strip() or "api/approval/comment"
    if scheme is not None:
        cfg.APPROVAL_AUTH_SCHEME = scheme.strip() or "bearer"
    if header is not None:
        cfg.APPROVAL_AUTH_HEADER = header.strip() or "X-Approval-Token"

    return {"ok": True, "operator": operator, "status": approval_writeback.status()}


# ---------------------------------------------------------------------- #
# 失败外发审核（钉钉）
#
# 链路：审查失败 → 条件门禁 → 待审核队列 → 责任人确认 → 推送钉钉 → 留痕
# 之所以让前端把操作人（display_name）带上来，是因为本项目的会话只存在于浏览器端
# （演示环境用 localStorage，无服务端 session）；审核留痕要记名，所以由调用方声明。
# ---------------------------------------------------------------------- #
@app.get("/api/export/config")
def export_config():
    """通道状态 + 生效条件 + 可选项元数据（前端一次拿齐渲染所需）。"""
    return {
        "channel": dingtalk.status(),
        "condition": export_queue.current_condition(),
        "options": {
            "kinds": [
                {"value": key, "label": meta["label"], "hint": meta["hint"]}
                for key, meta in FAILURE_KINDS.items()
            ],
            "sources": [{"value": key, "label": label} for key, label in SOURCES.items()],
        },
    }


@app.post("/api/export/channel")
def export_update_channel(payload: dict):
    """保存钉钉通道配置（Webhook / 加签密钥 / 关键词 / @手机号）。

    为什么做成界面可配而不是只读 config.py：线上站点是只读的容器，
    根本没有"改配置文件"这条路。凭据落盘到 data/settings.json，
    重启与重新发布都不会丢；同名环境变量优先级更高（运维可覆盖界面值）。

    约定：字段缺省（未出现在 payload 里）表示"这一项没动"，
    传空串表示"清空"，清空即回到模拟模式。
    """
    data = payload or {}

    def pick(key: str):
        return data[key] if key in data else None

    try:
        return dingtalk.update_config(
            webhook=pick("webhook"),
            secret=pick("secret"),
            keyword=pick("keyword"),
            at_mobiles=pick("at_mobiles"),
            operator=str(data.get("operator") or "未署名"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/export/condition")
def export_update_condition(payload: dict):
    """更新外发触发条件（只影响后续扫描，不改动已有记录的判定结果）。"""
    try:
        return export_queue.update_condition(payload)
    except Exception as e:  # 条件结构异常也不能让页面挂掉，退回 400 说明原因
        raise HTTPException(status_code=400, detail=f"条件格式有误：{e}")


@app.get("/api/export/queue")
def export_queue_list(status: str = ""):
    """审核队列：记录列表（可按状态过滤）+ 统计 + 最近审计日志。"""
    return {
        "records": export_queue.list_records(status or None),
        "stats": export_queue.stats(),
        "audit": export_queue.audit_log(20),
    }


@app.post("/api/export/scan")
def export_scan():
    """手动扫描全部任务：把遗漏的失败任务补进队列。

    自动入队已在审查失败时触发，这里用于"条件改了想重新筛一遍"或
    "服务重启后队列清空（内存存储）需要重建"的场景。
    """
    return export_queue.scan(pipeline.list_tasks())


@app.post("/api/export/{record_id}/approve")
def export_approve(record_id: str, payload: dict | None = None):
    """审核通过 → 推送钉钉。"""
    data = payload or {}
    try:
        rec = export_queue.approve(record_id, str(data.get("operator") or ""), str(data.get("note") or ""))
    except KeyError:
        raise HTTPException(status_code=404, detail="外发记录不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return rec.model_dump(mode="json")


@app.post("/api/export/{record_id}/reject")
def export_reject(record_id: str, payload: dict):
    """驳回：不外发，必须写明理由。"""
    try:
        rec = export_queue.reject(
            record_id,
            str(payload.get("operator") or ""),
            str(payload.get("note") or ""),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="外发记录不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return rec.model_dump(mode="json")


@app.post("/api/export/{record_id}/retry")
def export_retry(record_id: str, payload: dict | None = None):
    """推送失败后重试（正文沿用审核时冻结的那份）。"""
    data = payload or {}
    try:
        rec = export_queue.retry_push(record_id, str(data.get("operator") or ""))
    except KeyError:
        raise HTTPException(status_code=404, detail="外发记录不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return rec.model_dump(mode="json")


@app.post("/api/export/test")
def export_test(payload: dict | None = None):
    """发送通道连通性测试消息，用于确认 Webhook / 加签 / 关键词配置正确。"""
    data = payload or {}
    return export_queue.test_channel(str(data.get("operator") or ""))


# ---------------------------------------------------------------------- #
# SSE 流式审查通道
# ---------------------------------------------------------------------- #
@app.get("/api/tasks/{task_id}/stream")
def stream_task(task_id: str):
    """SSE：后台执行审查，实时推送状态与最终任务快照。"""
    try:
        task = pipeline.get_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在")

    existing = list(cfg.UPLOAD_DIR.glob(f"{task_id}.*"))
    if not existing:
        raise HTTPException(status_code=400, detail="无可用文件，请先上传")

    def event_gen():
        save_path = str(existing[0])
        # 后台线程执行审查
        done = threading.Event()
        err = {}

        def worker():
            try:
                pipeline.run(task_id, save_path)
            except Exception as e:  # pragma: no cover
                err["e"] = str(e)
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True).start()

        last_status = None
        # 轮询状态并推送增量事件
        while not done.is_set():
            cur = pipeline.get_task(task_id)
            if cur.status != last_status:
                yield _sse("status", {"status": cur.status.value, "error": cur.error})
                last_status = cur.status
            time.sleep(0.2)
        # 收尾事件
        final = pipeline.get_task(task_id)
        if "e" in err:
            yield _sse("error", {"message": err["e"]})
        else:
            yield _sse("status", {"status": final.status.value, "error": final.error})
            yield _sse("done", final.model_dump(mode="json"))

    return StreamingResponse(event_gen(), media_type="text/event-stream")


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=cfg.HOST, port=cfg.PORT)
