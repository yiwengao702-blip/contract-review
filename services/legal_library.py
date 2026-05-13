# 文件位置: contract-review/services/legal_library.py
# 功能说明: 法律依据库。解决三件事：
#           ① 法务能在规则页**搜到**审查引用的法律（内置条目由规则 YAML 的 legal_basis 派生，
#              不另存一份，避免"规则改了法律依据、法律库还是旧的"这种双份事实不一致）；
#           ② 支持**补充上传**企业内规/地方性规定（内置库覆盖不到的部分）；
#           ③ 支持**勾选本次用哪些法律审查**——选中后，只有援引这些法律的规则参与审查。
#
# 设计取舍：
#   - 内置条目派生自 review_rules.yaml，叠加层（自定义条目 / 启用态 / 备注 / 当前勾选）
#     单独落盘到 data/rules/legal_basis.json。两层合并后对外呈现。
#     若把内置条目也写进 JSON，规则文件一改就会与 JSON 分叉，且无法自动同步。
#   - 法律条目以「法律名称」为唯一键（去掉书名号）。规则 legal_basis 是自由文本，
#     按名称做包含匹配，而不是要求规则里额外维护一个 law_id 字段——
#     这样现有 13 条规则零改动即可接入法律筛选。
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from config import cfg

# 内置类别兜底：从法律名称推断领域，便于在页面上分组浏览
_CATEGORY_RULES = [
    (("民法典", "合同"), "合同债权"),
    (("劳动", "劳动合同", "社会保险", "工伤保险"), "劳动用工"),
    (("知识", "著作权", "专利", "商标", "计算机软件"), "知识产权"),
    (("招标", "投标", "政府采购"), "招标采购"),
    (("公司", "企业", "合伙"), "主体资格"),
    (("反垄断", "不正当竞争", "价格"), "市场竞争"),
    (("数据安全", "个人信息", "网络安全", "密码"), "数据与信息安全"),
    (("仲裁", "诉讼", "民事"), "争议解决"),
    (("税", "发票", "增值税"), "财税票据"),
]

# 《xxx》第x条 → 书名号内的法律名称
_BOOK_RE = re.compile(r"《([^》]+)》")


def _norm_name(name: str) -> str:
    """法律名称规范化：去书名号、去首尾空白。用作条目的唯一键。"""
    return (name or "").strip().strip("《》").strip()


def _guess_category(name: str) -> str:
    for keys, cat in _CATEGORY_RULES:
        if any(k in name for k in keys):
            return cat
    return "其他"


class LegalLibrary:
    """法律依据库：内置（派生自规则）+ 自定义（用户上传）合并视图。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._overlay: dict = self._load_overlay()

    # ------------------------------------------------------------------ #
    # 叠加层（落盘）
    # ------------------------------------------------------------------ #
    def _load_overlay(self) -> dict:
        """读取叠加层。文件缺失或损坏时返回空叠加，不让解析错误拖垮整页。"""
        path = Path(cfg.LEGAL_FILE)
        if not path.exists():
            return {"custom": [], "overrides": {}, "selected": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("叠加层格式不是对象")
        except Exception:
            return {"custom": [], "overrides": {}, "selected": []}
        data.setdefault("custom", [])
        data.setdefault("overrides", {})
        data.setdefault("selected", [])
        return data

    def _save_overlay(self) -> None:
        path = Path(cfg.LEGAL_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写临时文件再替换，避免写一半进程被杀导致文件损坏
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._overlay, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    # ------------------------------------------------------------------ #
    # 内置条目：从规则文件派生
    # ------------------------------------------------------------------ #
    def _builtin(self) -> List[dict]:
        """扫描规则文件的 legal_basis，聚合成法律条目。

        同一部法律被多条规则引用时合并为一条，并记录引用它的规则编号，
        —— 页面上「被 N 条规则引用」就是这个数，也是勾选后的实际影响范围。
        """
        path = cfg.RULES_DIR / "review_rules.yaml"
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        rules = data.get("rules") or []

        acc: Dict[str, dict] = {}
        for r in rules:
            basis = str(r.get("legal_basis") or "")
            if not basis:
                continue
            names = _BOOK_RE.findall(basis)
            # 规则里没写书名号时（如"根据民法典规定"），退化为用整段文本兜底成一个条目，
            # 保证它仍能在页面上被看到、被勾选，而不是静默消失。
            if not names:
                names = [basis[:20]]
            for raw in names:
                key = _norm_name(raw)
                if not key:
                    continue
                item = acc.setdefault(key, {
                    "key": key,
                    "name": key,
                    "category": _guess_category(key),
                    "articles": [],
                    "source": "builtin",
                    "used_by": [],
                })
                # 抽取条款号（"第五百零九条" / "第 12 条"），用于展示援引的具体条文
                for m in re.finditer(r"第\s*[\d零一二三四五六七八九十百千]+\s*条", basis):
                    art = re.sub(r"\s+", "", m.group(0))
                    if art not in item["articles"]:
                        item["articles"].append(art)
                rid = str(r.get("id") or "")
                if rid and rid not in item["used_by"]:
                    item["used_by"].append(rid)

        out = sorted(acc.values(), key=lambda x: (-len(x["used_by"]), x["name"]))
        return out

    # ------------------------------------------------------------------ #
    # 合并视图
    # ------------------------------------------------------------------ #
    def list_laws(self, keyword: str = "") -> List[dict]:
        """返回内置 + 自定义合并后的条目列表。"""
        with self._lock:
            overrides = self._overlay.get("overrides") or {}
            items: List[dict] = []

            for b in self._builtin():
                ov = overrides.get(b["key"]) or {}
                items.append({
                    **b,
                    "enabled": bool(ov.get("enabled", True)),
                    "summary": ov.get("summary", ""),
                    "note": ov.get("note", ""),
                })

            for c in self._overlay.get("custom") or []:
                ov = overrides.get(c["key"]) or {}
                items.append({
                    "key": c.get("key", ""),
                    "name": c.get("name", ""),
                    "category": c.get("category") or _guess_category(c.get("name", "")),
                    "articles": c.get("articles") or [],
                    "source": "custom",
                    "used_by": [],
                    "enabled": bool(ov.get("enabled", True)),
                    "summary": c.get("summary", "") or ov.get("summary", ""),
                    "note": ov.get("note", ""),
                    "doc_no": c.get("doc_no", ""),
                })

            kw = (keyword or "").strip().lower()
            if kw:
                items = [
                    x for x in items
                    if kw in x["name"].lower()
                    or kw in (x.get("summary") or "").lower()
                    or kw in (x.get("doc_no") or "").lower()
                    or kw in " ".join(x.get("articles") or []).lower()
                    or kw in (x.get("category") or "").lower()
                ]
            return items

    def selection(self) -> List[str]:
        """当前勾选用于审查的法律名称列表。空列表 = 不限制（全部法律参与）。"""
        with self._lock:
            return list(self._overlay.get("selected") or [])

    def stats(self, keyword: str = "") -> dict:
        items = self.list_laws(keyword)
        sel = set(self.selection())
        return {
            "total": len(items),
            "builtin": sum(1 for x in items if x["source"] == "builtin"),
            "custom": sum(1 for x in items if x["source"] == "custom"),
            "enabled": sum(1 for x in items if x.get("enabled", True)),
            "selected": sum(1 for x in items if x["key"] in sel),
            "selected_keys": sorted(sel),
        }

    # ------------------------------------------------------------------ #
    # 写操作
    # ------------------------------------------------------------------ #
    def add_custom(self, name: str, category: str = "", summary: str = "",
                   doc_no: str = "", articles: Optional[List[str]] = None) -> dict:
        """补充上传一部法律/规定。同名（规范化后）已存在则拒绝，避免重复条目。"""
        key = _norm_name(name)
        if not key:
            raise ValueError("法律名称不能为空")
        with self._lock:
            for c in self._overlay.get("custom") or []:
                if c.get("key") == key:
                    raise ValueError(f"「{key}」已存在，无需重复添加")
            for b in self._builtin():
                if b["key"] == key:
                    raise ValueError(
                        f"「{key}」已在内置库（被 {len(b['used_by'])} 条规则引用），无需重复添加")
            item = {
                "key": key,
                "name": key,
                "category": category or _guess_category(key),
                "summary": (summary or "").strip(),
                "doc_no": (doc_no or "").strip(),
                "articles": [a.strip() for a in (articles or []) if a.strip()],
            }
            self._overlay.setdefault("custom", []).append(item)
            self._overlay.setdefault("overrides", {}).setdefault(key, {"enabled": True})
            # 新增即默认纳入审查范围：用户专门补充的法律，多半就是为了用它审查
            sel = self._overlay.setdefault("selected", [])
            if key not in sel:
                sel.append(key)
            self._save_overlay()
            return item

    def set_enabled(self, key: str, enabled: bool) -> dict:
        """启停一部法律。停用后援引它的规则不再参与审查。"""
        with self._lock:
            self._overlay.setdefault("overrides", {}).setdefault(key, {})["enabled"] = bool(enabled)
            self._save_overlay()
            return {"key": key, "enabled": bool(enabled)}

    def set_selection(self, keys: List[str]) -> List[str]:
        """设置参与审查的法律。空列表表示不限制（全部法律参与）。"""
        with self._lock:
            known = {x["key"] for x in self.list_laws()}
            clean = [k for k in (keys or []) if k in known]
            self._overlay["selected"] = clean
            self._save_overlay()
            return clean

    def reload(self) -> None:
        """重新读取叠加层（规则文件改动后内置条目会自动跟着变，不需重载）。"""
        with self._lock:
            self._overlay = self._load_overlay()


# 模块级单例：法律库全局唯一，避免多处各自持有一份勾选状态
legal_library = LegalLibrary()
