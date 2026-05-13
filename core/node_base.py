# 文件位置: contract-review/core/node_base.py
# 功能说明: 审查流水线节点基类（NodeBase）。
#          采用 __init_subclass__ 实现子类的自动注册，新增审查节点时
#          只需继承 NodeBase 并声明 node_name，即被登记进全局注册表，
#          无需手动维护工厂分支。
from __future__ import annotations

from typing import ClassVar, Dict, Type


class NodeBase:
    """审查流水线节点抽象基类。

    子类约定：
      - 声明类属性 node_name（字符串，唯一），作为注册键；
      - 实现 run(context) 方法，接收并就地更新审查上下文 dict。
    """

    # 自动注册表：node_name -> 子类
    _registry: ClassVar[Dict[str, Type["NodeBase"]]] = {}

    # 子类必须提供的节点名
    node_name: str = ""

    def __init_subclass__(cls, **kwargs) -> None:
        """子类定义时自动将其登记到 _registry。"""
        super().__init_subclass__(**kwargs)
        name = getattr(cls, "node_name", "")
        if name:
            NodeBase._registry[name] = cls

    @classmethod
    def get_node(cls, name: str) -> Type["NodeBase"]:
        """按节点名取出已注册的节点类。"""
        if name not in cls._registry:
            raise KeyError(f"未注册的审查节点: {name}，已注册: {list(cls._registry)}")
        return cls._registry[name]

    @classmethod
    def list_nodes(cls):
        """返回当前所有已注册节点名。"""
        return list(cls._registry.keys())

    def run(self, context: dict) -> dict:
        """执行节点逻辑，默认原样返回上下文（子类需重写）。

        Args:
            context: 审查上下文，包含解析结果、规则、发现列表等。
        Returns:
            更新后的上下文。
        """
        raise NotImplementedError(f"节点 {self.node_name or type(self).__name__} 未实现 run()")
