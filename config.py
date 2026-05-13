# 文件位置: contract-review/config.py
# 功能说明: 全局配置（dataclass 单例模式）。所有模块统一从此处读取运行参数，
#          避免硬编码，便于在不同环境（开发/演示/生产）间切换。
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional


def _settings_path() -> Path:
    """界面上保存过的运行期设置存放位置（与 AppConfig.SETTINGS_FILE 同一路径）。"""
    return Path(__file__).resolve().parent / "data" / "settings.json"


def _load_settings() -> dict:
    """读取界面上保存过的运行期设置（目前是钉钉通道凭据）。

    文件不存在或内容损坏时返回空字典——配置读不出来不该让整个服务起不来，
    退回内置默认值即可，界面仍可重新填写并保存。
    """
    p = _settings_path()
    try:
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _setting(key: str, env: str, default=None):
    """按「环境变量 > 界面保存值 > 内置默认」的优先级取一个设置项。

    环境变量放最前是刻意的：运维在平台侧注入的密钥要能压过界面里填的值，
    这样"界面配错了"或"临时换机器人"都可以不动界面直接兜住。
    """
    v = os.environ.get(env)
    if v:
        return v
    v = _load_settings().get(key)
    if v not in (None, ""):
        return v
    return default


def setting_source(key: str, env: str) -> str:
    """一个设置项当前的值来自哪里：env（环境变量）/ settings（界面保存）/ default（内置）。

    为什么要报来源：界面上填了 Webhook 却发现"怎么还在模拟模式"，最常见的原因
    就是平台侧注入的环境变量压过了界面值。把来源标出来，用户一眼能看懂该去改哪边，
    而不是反复保存然后怀疑系统没生效。
    """
    if os.environ.get(env):
        return "env"
    v = _load_settings().get(key)
    if v not in (None, ""):
        return "settings"
    return "default"


def save_settings(**updates) -> Path:
    """把界面上的设置写入 SETTINGS_FILE，与已有内容**合并**，不覆盖其他键。

    合并而非整份覆盖：这些键以后会越来越多（今天只有钉钉凭据），
    整份写会把"这次没提交的项"冲掉，是典型的慢性数据丢失。

    写入用「临时文件 + 原子替换」：直接覆写的话，进程在写到一半时被中断，
    留下的半截 JSON 会让下次启动读不出任何配置（_load_settings 会静默回退成默认值，
    用户看到的是"我明明配过，怎么又没了"）。传 None 的键会被删除。
    """
    p = _settings_path()
    data = _load_settings()
    for k, v in updates.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)   # 同一分区内的 replace 是原子操作
    return p


def _default_host() -> str:
    """默认监听地址：本地回环，托管环境自动放开。

    判定依据是 PORT 环境变量——云平台/沙箱部署时会注入 PORT 并要求服务绑 0.0.0.0
    才能被外部反代访问到；本地开发没有这个变量，就只监听 127.0.0.1，
    不把服务暴露到局域网。这样同一份代码在本地与线上都不用改配置。
    """
    if os.environ.get("HOST"):
        return os.environ["HOST"]
    return "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"


def _default_port() -> int:
    """默认端口：托管环境用注入的 PORT，本地固定 18090。"""
    return int(os.environ.get("PORT") or 18090)


def _default_at_mobiles() -> tuple:
    """默认 @ 手机号列表：环境变量 > 界面保存值（均为逗号分隔）。

    返回 tuple 而非 list 是刻意的——dataclass 里可变默认值会被所有实例共享，
    用元组可以从类型上杜绝"某处改了全局跟着变"的隐性 bug。
    """
    raw = os.environ.get("DINGTALK_AT_MOBILES")
    if not raw:
        saved = _load_settings().get("dingtalk_at_mobiles")
        # 界面上存的是数组，环境变量里是逗号分隔串，两种形态都要吃得下
        if isinstance(saved, (list, tuple)):
            return tuple(str(x).strip() for x in saved if str(x).strip())
        raw = saved or ""
    return tuple(x.strip() for x in str(raw).split(",") if x.strip())


# 失败任务外发的默认触发条件（字段含义见 services/failure_gate.py 的逐条判定实现）。
# 默认只纳入「解析受阻」：那才是"审查这一步真的失败了"；
# 「非合同被拒」是闸门的正常工作结果（用户传错文件），默认不打扰钉钉群，
# 需要时在前端把 kinds 加上 rejected 即可。
#
# 这个常量是"出厂默认"，与 cfg.EXPORT_CONDITION（运行时可改）分开：
# 界面上那个「恢复默认」按钮要恢复到出厂值，而不是恢复成"上一次保存的值"。
DEFAULT_EXPORT_CONDITION: dict = {
    "kinds": ["blocked"],   # 纳入的失败类型：blocked（解析受阻）/ rejected（非合同被拒）
    "min_retries": 0,       # 已重试次数下限（0 = 不限，首次失败即可入队）
    "extensions": [],       # 文件后缀白名单（空 = 不限），如 [".pdf", ".docx"]
    "sources": [],          # 正文来源（空 = 不限）：upload（上传）/ scan（拍照识别）
    "reason_keywords": [],  # 失败原因关键词（空 = 不限），如 ["加密", "扫描件", "OCR"]
    "dedupe": True,         # 同一任务已推送过则不再重复入队
}


@dataclass
class AppConfig:
    """应用全局配置单例。

    采用 dataclass + lru_cache 实现进程内单例：
    任意模块调用 AppConfig.get() 拿到的都是同一份实例，
    修改其一处即可全局生效。
    """

    # ---- 基础路径 ----
    # 项目根目录：本文件上级目录
    BASE_DIR: Path = field(default_factory=lambda: Path(__file__).resolve().parent)
    # 上传与示例合同目录
    UPLOAD_DIR: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "data" / "uploads")
    SAMPLE_DIR: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "data" / "samples")
    RULES_DIR: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "data" / "rules")
    # 电商风控子系统专属目录：规则库 / 示例事件 / 名单库 / 判定历史落盘
    RISK_RULES_DIR: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "data" / "risk_rules")
    RISK_SAMPLE_DIR: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "data" / "risk_samples")
    RISK_STATE_DIR: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "data" / "risk_state")
    STATIC_DIR: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "static")
    # OCR 语言包目录：**随代码一起发布**的目录，见下方 TESSDATA_BUNDLED_DIR 说明。
    # 刻意不放进 __post_init__ 的建目录列表——目录不存在时应当"不参与探测"，
    # 而不是被建出一个空目录去截胡系统自带的语言包。
    TESSDATA_BUNDLED_DIR: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "data" / "tessdata")

    # 界面可改的运行期设置落盘位置（目前只有钉钉通道凭据）。
    # 为什么需要落盘：线上站点是只读文件系统里的代码副本，用户改不了 config.py，
    # 也只该改一次——总不能让运维每次部署都重新填 Webhook。存成 JSON 好处是
    # 读写可见、可人工核对，且不与代码混在一处（升级代码不会覆盖配置）。
    SETTINGS_FILE: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "data" / "settings.json")

    # 法律依据库落盘位置。与 SETTINGS_FILE 同理：用户上传/维护的法律条目要能跨重启留存，
    # 又不能写进代码目录（升级即被覆盖）。单独一个文件是为了职责清晰——
    # 通道凭据是「运维配置」，法律库是「业务数据」，混在一起会让备份与审阅都变困难。
    LEGAL_FILE: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "data" / "rules" / "legal_basis.json")

    # ---- 服务网络 ----
    # HOST / PORT 均支持环境变量覆盖（见上方 _default_host / _default_port）：
    # 本地默认 127.0.0.1:18090；部署到云平台/沙箱时由注入的 PORT 自动改为 0.0.0.0:$PORT。
    HOST: str = field(default_factory=_default_host)
    PORT: int = field(default_factory=_default_port)
    # SSE 流式审查是否开启（逐条推送风险发现）
    ENABLE_SSE: bool = True

    # ---- 文档解析 ----
    # 单文件大小上限（字节），默认 20MB
    MAX_UPLOAD_BYTES: int = 20 * 1024 * 1024

    # ---- 拍照 / 图片识别（services/ocr_service.py 的可插拔适配层） ----
    # 说明：图片与扫描件是否走 OCR，不由开关决定，而是由下方各引擎的"可用性探测"决定
    #       （ocr_service.probe()）：任一引擎可用就自动用它，全不可用则降级到人工录入。
    #       这样避免了"开关打开但引擎没装"这种自相矛盾的状态。
    # 引擎按优先级依次尝试：tesseract 本地引擎 -> 多模态大模型(vlm) -> 云 OCR(cloud) -> 人工录入兜底。
    # tesseract 需 pip install pytesseract + 安装 Tesseract-OCR 程序（含 chi_sim 中文包）；
    # vlm 复用下方 LLM_* 配置（需 LLM_PROVIDER=openai 且模型支持图片输入）；
    # cloud 走通用 HTTP 接口，需填以下两项，留空则该引擎不参与尝试。
    OCR_API_URL: Optional[str] = None
    OCR_API_KEY: Optional[str] = None
    # 一次拍照识别最多接受的页数（一份纸质合同常见 2~10 页，20 页足够且能挡住误选整个相册）
    MAX_SCAN_PAGES: int = 20
    # ---- tesseract 引擎的显式路径（可选） ----
    # 留空时按 PATH → 常见安装目录顺序自动探测；下列场景必须显式指定：
    #   ① Tesseract 装完但当前进程 PATH 未刷新（Windows 安装包不会改写已打开的终端）；
    #   ② 同一台机器装了多个版本，需要固定用某一个。
    TESSERACT_CMD: Optional[str] = None
    # 语言包目录：留空用引擎自带的 tessdata（Windows 上通常是
    # C:\Program Files\Tesseract-OCR\tessdata）。需要显式指定的典型场景是
    # **该目录不可写**（Program Files 需要管理员权限），于是把语言包放到用户目录下另行维护：
    # 中文包 chi_sim 往往不在安装包默认选项里，得单独下载，而下载后却写不进 Program Files。
    # 指定后引擎会带 --tessdata-dir 参数读取，目录内需包含用到的语言（chi_sim + eng）。
    TESSDATA_DIR: Optional[str] = None

    # ---- 智能审查（大模型） ----
    # LLM 供应商：mock（离线启发式，默认，无需密钥）/ openai（兼容 OpenAI 接口）。
    # 选 openai 但未配置 LLM_API_KEY / LLM_BASE_URL 时，适配器会自动降级回 mock，
    # 不会让一次审查因 LLM 未就绪而整体失败。
    LLM_PROVIDER: str = "mock"
    # 以下三项支持「环境变量 > 界面保存值 > 内置默认」优先级（与钉钉通道一致），
    # 这样平台侧注入密钥可压过界面填写，临时换模型也不动界面。
    LLM_API_KEY: Optional[str] = field(
        default_factory=lambda: _setting("llm_api_key", "LLM_API_KEY", None))
    LLM_BASE_URL: Optional[str] = field(
        default_factory=lambda: _setting("llm_base_url", "LLM_BASE_URL", None))
    LLM_MODEL: str = field(
        default_factory=lambda: _setting("llm_model", "LLM_MODEL", "gpt-4o-mini"))
    # 单次请求超时（秒）与生成温度：风控类审查追求稳定可复现，温度压低。
    LLM_TIMEOUT: float = 30.0
    LLM_TEMPERATURE: float = 0.1

    # ---- 审批系统评论区回写 ----
    # 真实回写：把审查意见 POST 到审批系统的评论接口；未配置则走内存模拟回写。
    # APPROVAL_API_BASE 支持环境变量 / 界面保存值（管理员在「合规规则与模板」页填写）。
    APPROVAL_API_BASE: Optional[str] = field(
        default_factory=lambda: _setting("approval_api_base", "APPROVAL_API_BASE", None))
    # 回写接口鉴权令牌（可选）。scheme 决定如何携带：
    #   bearer        -> Authorization: Bearer <token>
    #   token-header  -> 自定义请求头 <APPROVAL_AUTH_HEADER>: <token>
    #   none          -> 不携带（内网免鉴权场景）
    APPROVAL_API_TOKEN: Optional[str] = field(
        default_factory=lambda: _setting("approval_api_token", "APPROVAL_API_TOKEN", None))
    APPROVAL_AUTH_SCHEME: str = field(
        default_factory=lambda: _setting("approval_auth_scheme", "APPROVAL_AUTH_SCHEME", "bearer") or "bearer")
    APPROVAL_AUTH_HEADER: str = field(
        default_factory=lambda: _setting("approval_auth_header", "APPROVAL_AUTH_HEADER", "X-Approval-Token") or "X-Approval-Token")
    # 评论接口路径（拼在 APPROVAL_API_BASE 之后），不同审批系统路径不同，故可配。
    APPROVAL_COMMENT_PATH: str = field(
        default_factory=lambda: _setting("approval_comment_path", "APPROVAL_COMMENT_PATH", "api/approval/comment") or "api/approval/comment")
    # 回写请求体里标记「审批单/审批实例」的字段名（不同系统叫 instance_id / process_id / task_id）。
    APPROVAL_INSTANCE_FIELD: str = "instance_id"
    # 回写请求超时（秒）与失败重试次数（网络抖动时自动重试，减少误判为「回写失败」）。
    APPROVAL_TIMEOUT: float = 10.0
    APPROVAL_MAX_RETRIES: int = 3

    # ---- 失败任务外发钉钉 ----
    # 场景：审查失败（解析受阻 / 非合同被拒）的任务，需要让法务与业务方知道，
    #       但"每个失败都群发"会变成噪音——失败里既有真问题（合同是加密扫描件），
    #       也有正常业务流转（用户随手传了个技术文档被闸门拦下）。
    #       所以链路是：失败 → 条件门禁筛一遍 → 剩下的进人工审核队列 → 确认后才外发。
    #
    # 通道：钉钉群自定义机器人 Webhook。留空则走"模拟推送"——流程、留痕、
    #      审核动作全部可跑通，只是不真的发出，便于本地演示与联调；
    #      填上真实 Webhook 即自动切为真实推送，无需改代码。
    #      DINGTALK_WEBHOOK / DINGTALK_SECRET 支持环境变量注入，
    #      便于部署时在平台侧配置而不把密钥写进代码。
    DINGTALK_WEBHOOK: Optional[str] = field(
        default_factory=lambda: _setting("dingtalk_webhook", "DINGTALK_WEBHOOK", None))
    # 加签密钥（机器人安全设置选"加签"时必填；钉钉要求 timestamp + HMAC-SHA256）
    DINGTALK_SECRET: Optional[str] = field(
        default_factory=lambda: _setting("dingtalk_secret", "DINGTALK_SECRET", None))
    # 机器人安全设置选"自定义关键词"时，消息必须包含该词，否则钉钉直接拒收（errcode 310000）。
    # 默认给"合同审查"，消息标题与正文里都会带上，保证两种安全设置下都能发出。
    DINGTALK_KEYWORD: str = field(
        default_factory=lambda: _setting("dingtalk_keyword", "DINGTALK_KEYWORD", "合同审查"))
    # 需要 @ 的手机号（可选），用于让责任人对失败任务即时可见。
    # 环境变量或界面里都用逗号分隔（13800000000,13900000000）。
    DINGTALK_AT_MOBILES: tuple = field(default_factory=_default_at_mobiles)
    # 审查结束产生失败态时是否自动跑一遍门禁并入队（关掉则只能手动"扫描失败任务"）
    EXPORT_ON_RUN: bool = True
    # 外发触发条件（可在前端"失败外发审核"页修改，改内存即生效，重启回到出厂默认）
    EXPORT_CONDITION: dict = field(default_factory=lambda: dict(DEFAULT_EXPORT_CONDITION))
    # 外发记录保留上限（超出丢弃最旧的已处理记录，防止演示时内存无限增长）
    MAX_EXPORT_RECORDS: int = 200

    def __post_init__(self) -> None:
        # 确保关键目录存在
        for d in (self.UPLOAD_DIR, self.SAMPLE_DIR, self.RULES_DIR, self.STATIC_DIR,
                  self.RISK_RULES_DIR, self.RISK_SAMPLE_DIR, self.RISK_STATE_DIR):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    @lru_cache(maxsize=1)
    def get(cls) -> "AppConfig":
        """获取全局唯一配置实例（单例）。"""
        return cls()


# 便捷别名，模块内可直接 from config import cfg
cfg = AppConfig.get()
