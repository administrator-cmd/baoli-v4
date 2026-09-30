"""审核执行的分层实现。

各模块的职责与依赖方向见 `docs/后端分层约定.md`；依赖顺序（只允许向下）：

    commands → flow → reviews → {reporting, views, quality, rules} → facts → state → evidence → artifacts → shared

对外只暴露命令入口与只读视图，调用方不再依赖执行内部细节。

**导出必须保持惰性**（PEP 562 `__getattr__`）：这里急切 `import commands` 会让
「只要导入本包的任意子模块就先执行 commands」，从而把 `commands → task_service`
这条边强加给所有子模块，形成包级环
（`task_service → audit.flow → audit 包 → commands → task_service`）。
惰性导出后 `from .audit import flow` 只加载 flow，环随之消失。
"""

from typing import Any

__all__ = [
    "correct_field_value",
    "evidence_view",
    "execute_current_node",
    "reconcile_ocr",
    "recover_interrupted_executions",
    "report_view",
    "risks_view",
    "submit_manual_reviews",
]

# 公开名 → (子模块, 该模块内的名字)
_EXPORTS = {
    "correct_field_value": ("commands", "correct_field_value"),
    "execute_current_node": ("commands", "execute_current_node"),
    "reconcile_ocr": ("commands", "reconcile_ocr"),
    "recover_interrupted_executions": ("commands", "recover_interrupted_executions"),
    "submit_manual_reviews": ("commands", "submit_manual_reviews"),
    "evidence_view": ("views", "evidence_view"),
    "report_view": ("views", "report_view"),
    "risks_view": ("views", "risks_view"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f".{target[0]}", __name__)
    return getattr(module, target[1])
