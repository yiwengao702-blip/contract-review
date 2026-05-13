# 文件位置: contract-review/services/dingtalk.py
# 功能说明: 钉钉群机器人推送通道——审查失败任务对外通知的唯一出口。
#
# 设计要点（每一条都对应一个真实会踩的坑）：
#  ① **未配置 Webhook 时走"模拟推送"**，而不是让流程失败。
#     审核-留痕-驳回这套动作的价值不依赖消息真的发出去；本地演示、联调阶段
#     没有机器人地址也该能完整走通，填上地址即自动切真实推送，代码零改动。
#  ② **钉钉的失败藏在 HTTP 200 里**。errcode != 0 才是真失败（如 310000 关键词不匹配、
#     300001 签名校验失败）。只看 HTTP 状态码会出现"系统显示推送成功，群里没消息"，
#     这是通知类功能最容易被忽略的缺陷，所以本模块以 errcode 判定成败。
#  ③ **关键词与加签两种安全设置都支持**。钉钉要求二选一（或 IP 白名单），
#     选"自定义关键词"时消息里不含该词会被直接拒收——构建消息时强制带上，
#     不把"要不要写关键词"留给调用方记着。
#  ④ **推送用的正文在入队时就生成并存进记录**。审核人预览到的内容与实际发出的
#     必须是同一份；如果推送时才现拼，就可能出现"审核时看到 A、发出去是 B"。
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
from typing import List, Optional, Sequence

import requests

from config import cfg, save_settings, setting_source
from core.models import ExportRecord, PushResult

# 钉钉自定义机器人 Webhook 的固定前缀，用于校验用户填的地址是否写错
_DINGTALK_HOST = "oapi.dingtalk.com"
# 单次请求超时（秒）。通知类调用不该拖住业务请求，超时就当失败并允许重试。
_TIMEOUT = 8


class DingTalkNotifier:
    """钉钉群机器人通道。

    生命周期：构建消息（入队时）→ 人工审核 → 发送（审核通过时）。
    发送本身不抛异常，一律返回 PushResult，由上层决定记录状态与可否重试。
    """

    channel = "dingtalk"
    channel_label = "钉钉群机器人"

    # ------------------------------------------------------------------ #
    # 通道状态
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        """通道能力现状。

        前端"通道状态卡"直接消费这个结构：打开页面就说明白"能不能真发出去"，
        而不是等审核通过、点了推送，才发现原来还在模拟模式。
        """
        webhook = (cfg.DINGTALK_WEBHOOK or "").strip()
        secret = (cfg.DINGTALK_SECRET or "").strip()
        keyword = (cfg.DINGTALK_KEYWORD or "").strip()
        configured = bool(webhook)
        warnings: List[str] = []

        if not configured:
            warnings.append(
                "未配置 DINGTALK_WEBHOOK，当前为**模拟模式**：条件审核、人工确认、留痕记录都能完整走通，"
                "但消息不会真的发到钉钉群。"
            )
        elif _DINGTALK_HOST not in webhook:
            warnings.append(
                f"Webhook 地址里没有出现 {_DINGTALK_HOST}，请确认不是复制到了其他平台的地址。"
            )
        if configured and secret and len(secret) < 32:
            warnings.append(
                "加签密钥长度明显偏短（钉钉生成的是 SEC 开头的长串），请确认没有截断。"
            )

        return {
            "channel": self.channel,
            "label": self.channel_label,
            "configured": configured,
            "mode": "real" if configured else "simulated",
            "mode_label": "真实推送" if configured else "模拟推送",
            "webhook_masked": _mask_webhook(webhook),
            "secret_set": bool(secret),
            "secret_masked": _mask_secret(secret),
            "keyword": keyword,
            "at_mobiles": list(cfg.DINGTALK_AT_MOBILES),
            "secure_by": (
                "加签 + 关键词" if (secret and keyword)
                else "加签" if secret
                else "关键词" if keyword
                else "未设置（机器人若启用了安全设置会被拒收）"
            ),
            "warnings": warnings,
            "hint": (
                "在钉钉群「群设置 → 智能群助手 → 添加机器人 → 自定义」创建后，"
                "把 Webhook 地址与加签密钥填到下方表单保存即可（落盘到 data/settings.json，"
                "重启与重新发布都不会丢）；也可用同名环境变量注入，环境变量优先级更高。"
            ),
        }

    # ------------------------------------------------------------------ #
    # 通道配置（界面可改）
    # ------------------------------------------------------------------ #
    def form_config(self) -> dict:
        """给前端表单用的完整配置（**不回传凭据明文**）。

        为什么不回传明文：界面上的输入框只需要让人确认"填过了/没填过"，
        把 secret 原样送回浏览器等于让凭据随每次页面加载在网络里多跑一趟。
        所以：已设置的值只回显掩码，用户不重新输入就沿用原值（见 update_config）。
        """
        webhook = (cfg.DINGTALK_WEBHOOK or "").strip()
        secret = (cfg.DINGTALK_SECRET or "").strip()
        keyword = (cfg.DINGTALK_KEYWORD or "").strip()
        return {
            "webhook_masked": _mask_webhook(webhook),
            "webhook_set": bool(webhook),
            "secret_masked": _mask_secret(secret),
            "secret_set": bool(secret),
            "keyword": keyword,
            "at_mobiles": list(cfg.DINGTALK_AT_MOBILES),
            # 每个值当前来自哪里（环境变量 / 界面保存 / 内置默认）——
            # 「我明明在界面填了怎么还是模拟模式」几乎都是环境变量压过了界面值，
            # 把来源标出来，用户一眼知道该去改哪边。
            "source": {
                "webhook": setting_source("dingtalk_webhook", "DINGTALK_WEBHOOK"),
                "secret": setting_source("dingtalk_secret", "DINGTALK_SECRET"),
                "keyword": setting_source("dingtalk_keyword", "DINGTALK_KEYWORD"),
                "at_mobiles": "env" if os.environ.get("DINGTALK_AT_MOBILES") else "settings_or_default",
            },
        }

    def update_config(self, webhook: Optional[str] = None, secret: Optional[str] = None,
                      keyword: Optional[str] = None, at_mobiles: Optional[Sequence[str]] = None,
                      operator: str = "未署名") -> dict:
        """保存通道配置：先校验，再落盘，最后同步到内存中的 cfg。

        三个约定：
          · 传 None 表示"这一项没动"，传空串表示"清空"（清空即回到内置默认/模拟模式）；
          · 校验不通过直接抛 ValueError，**不写入任何字段**（避免半套配置）；
          · 先落盘再改内存——反过来的话，落盘失败时内存已生效，重启后又变回去，
            用户会看到"刚才明明好了".
        """
        cur_webhook = (cfg.DINGTALK_WEBHOOK or "").strip()
        cur_secret = (cfg.DINGTALK_SECRET or "").strip()
        cur_keyword = (cfg.DINGTALK_KEYWORD or "").strip()
        cur_at = list(cfg.DINGTALK_AT_MOBILES)

        new_webhook = cur_webhook if webhook is None else str(webhook).strip()
        new_secret = cur_secret if secret is None else str(secret).strip()
        new_keyword = cur_keyword if keyword is None else str(keyword).strip()
        if at_mobiles is None:
            new_at = cur_at
        elif isinstance(at_mobiles, str):
            new_at = [x.strip() for x in at_mobiles.split(",") if x.strip()]
        else:
            new_at = [str(x).strip() for x in at_mobiles if str(x).strip()]

        problems = _validate_channel_config(new_webhook, new_secret, new_keyword, new_at)
        if problems:
            raise ValueError("；".join(problems))

        # 空串 → 删除该键，让取值链回到「环境变量 > 内置默认」而不是留个空值把默认压掉
        save_settings(
            dingtalk_webhook=new_webhook or None,
            dingtalk_secret=new_secret or None,
            dingtalk_keyword=new_keyword or None,
            dingtalk_at_mobiles=new_at or None,
        )

        cfg.DINGTALK_WEBHOOK = new_webhook or None
        cfg.DINGTALK_SECRET = new_secret or None
        cfg.DINGTALK_KEYWORD = new_keyword or "合同审查"
        cfg.DINGTALK_AT_MOBILES = tuple(new_at)

        return {
            "ok": True,
            "status": self.status(),
            "form": self.form_config(),
        }

    # ------------------------------------------------------------------ #
    # 消息构建
    # ------------------------------------------------------------------ #
    def build_message(self, record: ExportRecord) -> str:
        """构建失败任务告警正文（Markdown）。

        入队时调用一次并把结果存进记录：审核人看到的就是将要发出的。
        """
        kind_text = _KIND_TEXT.get(record.failure_kind, record.failure_kind)
        kw = (cfg.DINGTALK_KEYWORD or "").strip()

        # 关键词安全设置要求消息含该词；标题里带一个是最稳的写法
        title_line = f"## 合同审查失败提醒{('（' + kw + '）') if kw else ''}"

        passed = [it for it in record.gate.items if it.passed and not it.skipped]
        gate_line = "、".join(f"{it.label}={it.actual}" for it in passed) or "无启用条件"

        lines = [
            title_line,
            "",
            f"- **失败类型**：{kind_text}",
            f"- **合同名称**：{record.filename}",
            f"- **业务类型**：{record.contract_type}",
            f"- **正文来源**：{_SOURCE_TEXT.get(record.source, record.source)}",
            f"- **任务编号**：{record.task_id}",
            f"- **已重试**：{record.retries} 次",
            "",
            f"**失败原因**：{record.reason or '（未提供）'}",
            "",
            f"**命中条件**：{gate_line}",
            "",
            f"> 该条已通过条件门禁（编号 {record.record_id}），等待责任人确认后外发；"
            f"若判定为无需跟进，可在系统中驳回并记录理由。",
        ]
        text = "\n".join(lines)

        # 需要 @ 的人：钉钉 Markdown 里必须出现 @手机号 才会真正提醒到人
        if cfg.DINGTALK_AT_MOBILES:
            text += "\n\n" + " ".join(f"@{m}" for m in cfg.DINGTALK_AT_MOBILES)
        return text

    def message_title(self, record: ExportRecord) -> str:
        """消息标题（钉钉客户端通知栏展示用）。"""
        kind_text = _KIND_TEXT.get(record.failure_kind, record.failure_kind)
        return f"合同审查失败提醒 · {kind_text} · {record.filename}"

    def build_test_message(self) -> tuple:
        """通道连通性测试消息（标题, 正文）。"""
        kw = (cfg.DINGTALK_KEYWORD or "").strip()
        title = f"合同审查系统 · 通道测试{('（' + kw + '）') if kw else ''}"
        text = "\n".join([
            f"## 合同审查系统 · 通道测试{('（' + kw + '）') if kw else ''}",
            "",
            "- 这是一条由「失败外发审核」页触发的连通性测试消息。",
            f"- 当前通道：{self.channel_label}",
            f"- 当前模式：{'真实推送' if (cfg.DINGTALK_WEBHOOK or '').strip() else '模拟推送（未配置 Webhook）'}",
            "",
            f"> 收到这条消息说明 Webhook{('与加签' if (cfg.DINGTALK_SECRET or '').strip() else '')}配置正确。",
        ])
        return title, text

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    def send_text(self, title: str, text: str) -> PushResult:
        """发送 Markdown 消息。不抛异常，成败都通过 PushResult 返回。"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        webhook = (cfg.DINGTALK_WEBHOOK or "").strip()

        # 未配置通道：模拟成功。流程可继续、留痕可查，只是不真的发出。
        if not webhook:
            return PushResult(
                ok=True, mode="simulated", channel=self.channel, errorcode=0,
                errmsg="simulated", pushed_at=now,
                detail="未配置 DINGTALK_WEBHOOK，按模拟通道记录（未实际发出）。"
                       "填上 Webhook 后同一操作会真实推送到钉钉群。",
            )

        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
            "at": {
                "atMobiles": list(cfg.DINGTALK_AT_MOBILES),
                "isAtAll": False,
            },
        }

        try:
            resp = requests.post(
                self._signed_url(webhook), json=payload, timeout=_TIMEOUT,
                headers={"Content-Type": "application/json"},
            )
        except Exception as e:  # 网络不通 / DNS 失败 / 超时
            return PushResult(
                ok=False, mode="real", channel=self.channel, errmsg=f"{type(e).__name__}: {e}",
                pushed_at=now,
                detail="请求钉钉接口失败（网络或超时），可稍后重试。",
            )

        # 钉钉即使业务失败也返回 HTTP 200，所以必须解析响应体里的 errcode
        try:
            body = resp.json()
        except Exception:
            return PushResult(
                ok=False, mode="real", channel=self.channel, http_status=resp.status_code,
                errmsg=(resp.text or "")[:200], pushed_at=now,
                detail="钉钉返回的不是 JSON，可能是网关或代理拦截，请核对 Webhook 地址。",
            )

        errcode = body.get("errcode")
        errmsg = str(body.get("errmsg") or "")
        ok = errcode == 0
        if ok:
            detail = "推送成功。"
        elif errcode is None:
            # 响应是合法 JSON 却没有 errcode 字段 —— 这根本不是钉钉的应答。
            # 真实场景：公司网关 / SSO 登录页 / 反向代理截了请求，回的是自己的错误体
            # （如 {"detail":"Method Not Allowed"}）。此时若照常拼 "errcode=None"，
            # 运维看到的就是一句废话，无从下手，所以把状态码和响应片段摆出来。
            snippet = (resp.text or "").strip().replace("\n", " ")[:140]
            detail = (f"响应不是钉钉接口的正常应答（HTTP {resp.status_code}，无 errcode 字段），"
                      f"很可能被网关或代理拦截，请核对 Webhook 地址。响应片段：{snippet}")
        else:
            detail = _explain_errcode(errcode, errmsg)
        return PushResult(
            ok=ok, mode="real", channel=self.channel,
            http_status=resp.status_code, errorcode=errcode, errmsg=errmsg, pushed_at=now,
            detail=detail,
        )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _signed_url(webhook: str) -> str:
        """加签：钉钉要求 URL 追加 timestamp 与 sign。

        算法：sign = urlencode(base64(HMAC-SHA256(secret, f"{timestamp}\\n{secret}")))
        未配置 secret 时原样返回（机器人安全设置可能用的是关键词而非加签）。
        """
        secret = (cfg.DINGTALK_SECRET or "").strip()
        if not secret:
            return webhook
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                          digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        sep = "&" if "?" in webhook else "?"
        return f"{webhook}{sep}timestamp={timestamp}&sign={sign}"


# ---------------------------------------------------------------------- #
# 模块级工具
# ---------------------------------------------------------------------- #
_KIND_TEXT = {"blocked": "解析受阻", "rejected": "非合同被拒"}
_SOURCE_TEXT = {"upload": "上传电子文档", "scan": "拍照识别"}


def _mask_webhook(url: str) -> str:
    """脱敏展示 Webhook：access_token 是凭据，不能整串显示在界面上。"""
    if not url:
        return ""
    if "access_token=" in url:
        head, _, tail = url.partition("access_token=")
        token = tail.split("&")[0]
        return f"{head}access_token={token[:6]}***{token[-4:] if len(token) > 12 else ''}"
    return url[:48] + ("…" if len(url) > 48 else "")


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    return f"{secret[:6]}***{secret[-4:]}" if len(secret) > 12 else "***"


def _explain_errcode(errcode: Optional[int], errmsg: str) -> str:
    """把钉钉的错误码翻译成可操作的中文说明。

    这几个码是实际配置机器人时最常撞到的，直接给出下一步动作，
    避免只看到一句 "send failed" 无从下手。
    """
    table = {
        300001: "签名校验失败：请核对 DINGTALK_SECRET 是否与机器人「加签」里的一致。",
        310000: "关键词不匹配：机器人安全设置选了「自定义关键词」，但消息里没包含该词。"
                "请把机器人关键词改为包含当前 DINGTALK_KEYWORD 的值，或反过来调整配置。",
        130101: "被钉钉限流：发送过于频繁，稍后重试即可。",
        400013: "机器人已被停用或删除，请到钉钉群重新添加机器人并更新 Webhook。",
    }
    if errcode in table:
        return table[errcode]
    return f"钉钉返回失败：errcode={errcode}，errmsg={errmsg}"


def _validate_channel_config(webhook: str, secret: str, keyword: str,
                             at_mobiles: Sequence[str]) -> List[str]:
    """校验界面上填的通道配置，返回问题列表（空列表 = 通过）。

    为什么要在写入前拦一道：这些字段拼错时**钉钉不会报错给用户看**，
    而是在推送那一刻返回 errcode——那时人已经离开配置页了。
    例子：Webhook 少复制一段 token，界面显示"已配置"，直到某天真的要发通知才发现发不出去。
    所以把"能提前发现的错误"全部提前，且每条都给出该怎么改。
    """
    problems: List[str] = []

    if webhook:
        if _DINGTALK_HOST not in webhook:
            problems.append(
                f"Webhook 地址不像钉钉机器人地址（应包含 {_DINGTALK_HOST}），"
                "请从「群设置 → 智能群助手 → 机器人 → Webhook」重新复制完整地址"
            )
        elif "access_token=" not in webhook:
            problems.append(
                "Webhook 缺少 access_token 参数，多半是复制时被截断了，请重新复制完整地址"
            )
        elif not webhook.startswith(("http://", "https://")):
            problems.append("Webhook 必须以 http:// 或 https:// 开头")

    if secret:
        # 钉钉加签密钥固定以 SEC 开头；填反了（比如把 access_token 填进来）当场提示，
        # 否则只会在推送时收到 300001 签名校验失败，看不出是填错了字段。
        if not secret.startswith("SEC"):
            problems.append(
                "加签密钥格式不对（钉钉的加签密钥以 SEC 开头），"
                "请核对是否误把 access_token 填到了密钥栏"
            )
        elif len(secret) < 40:
            problems.append("加签密钥长度异常（通常为 64 位字符），请检查是否复制完整")

    # 关键词与加签是钉钉二选一的安全设置；两个都空时消息大概率被拒收，
    # 但 IP 白名单模式下确实两者都不需要，所以只提示不拦截。
    if not keyword and not secret:
        problems.append(
            "关键词与加签密钥都为空：若机器人安全设置用的是「自定义关键词」或「加签」，"
            "消息会被钉钉拒收。请至少填写其中一项（关键词默认「合同审查」）"
        )

    for m in at_mobiles:
        digits = re.sub(r"[^\d]", "", str(m))
        if not (len(digits) == 11 and digits.startswith("1")):
            problems.append(f"@手机号「{m}」格式不对，应为 11 位中国大陆手机号（如 13800000000）")

    return problems


# 模块级单例：通知出口全局唯一，避免多处 new 出各自的状态
dingtalk = DingTalkNotifier()
