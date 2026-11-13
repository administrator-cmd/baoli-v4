"""工具运行上下文：任务、规则、材料范围和数据库会话。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolExecutionContext:
    task_id: int | None = None
    rule_instance_id: str | None = None
    scene_code: str | None = None
    node_code: str | None = None
    material_refs: tuple[str, ...] = ()
    database_session: Any = field(default=None, compare=False, hash=False, repr=False)

    @property
    def scoped_materials(self) -> frozenset[str]:
        return frozenset(item for item in self.material_refs if item)
