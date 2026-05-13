# 文件位置: contract-review/services/list_manager.py
# 功能说明: 黑白名单库管理（user / device / ip 三个维度）。
#          名单命中具有最高优先级：黑名单直接拦截、白名单免审放行，压过规则引擎结论。
#          名单持久化于 data/risk_state/lists.json（原子替换写入，避免半截文件）。
from __future__ import annotations

import threading
import time
from typing import List, Optional

from config import cfg
from core.risk_models import ListEntry, RiskEvent


class ListManager:
    """黑白名单库（线程安全，内存为主、落盘为副）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = cfg.RISK_STATE_DIR / "lists.json"
        self._entries: List[ListEntry] = self._load()

    def _load(self) -> List[ListEntry]:
        if not self._path.exists():
            return []
        try:
            import json
            data = json.loads(self._path.read_text(encoding="utf-8")) or []
            return [ListEntry(**e) for e in data if isinstance(e, dict)]
        except Exception:
            return []

    def _save(self) -> None:
        import json
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps([e.model_dump() for e in self._entries],
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def list_all(self) -> List[ListEntry]:
        with self._lock:
            return [e for e in self._entries]

    def add(self, list_type: str, dimension: str, value: str,
            reason: str = "", added_by: str = "system") -> ListEntry:
        if list_type not in ("black", "white"):
            raise ValueError("list_type 必须为 black 或 white")
        if dimension not in ("user", "device", "ip"):
            raise ValueError("dimension 必须为 user / device / ip")
        value = (value or "").strip()
        if not value:
            raise ValueError("value 不能为空")
        with self._lock:
            # 去重：同类型同维度同值不重复加入
            for e in self._entries:
                if e.list_type == list_type and e.dimension == dimension and e.value == value:
                    e.reason = reason or e.reason
                    e.added_by = added_by
                    e.added_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    self._save()
                    return e
            entry = ListEntry(
                list_type=list_type, dimension=dimension, value=value,
                reason=reason, added_by=added_by,
                added_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            self._entries.append(entry)
            self._save()
            return entry

    def remove(self, list_type: str, dimension: str, value: str) -> bool:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries
                             if not (e.list_type == list_type and e.dimension == dimension
                                     and e.value == value)]
            removed = len(self._entries) != before
            if removed:
                self._save()
            return removed

    def contains(self, list_type: str, dimension: str, value: str) -> bool:
        if not value:
            return False
        with self._lock:
            return any(e.list_type == list_type and e.dimension == dimension
                       and e.value == value for e in self._entries)

    def check_event(self, event: RiskEvent) -> Optional[str]:
        """对事件的三维度（user/device/ip）做名单判定，返回 'black' / 'white' / None。"""
        uid = event.user_id
        dev = event.device
        did = dev.device_id if dev else None
        ip = dev.ip if dev else None
        # 白名单优先返回（叫免审）：任一维度在白名单即可
        if self.contains("white", "user", uid):
            return "white"
        if did and self.contains("white", "device", did):
            return "white"
        if ip and self.contains("white", "ip", ip):
            return "white"
        # 黑名单：任一维度命中即拦截
        if self.contains("black", "user", uid):
            return "black"
        if did and self.contains("black", "device", did):
            return "black"
        if ip and self.contains("black", "ip", ip):
            return "black"
        return None


# 模块级单例
list_manager = ListManager()
