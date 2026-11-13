"""规则执行方式：与业务无关的执行动作，由解析提案写入并随 manifest 冻结。"""

from typing import Literal, get_args

ExecutionMode = Literal[
    "MATERIAL_CHECK",
    "VALUE_COMPARE",
    "CROSS_SCOPE_COMPARE",
    "AGGREGATE",
    "DERIVE_RISK",
    "EXTRACT",
    "CALCULATE",
    "REPORT",
    "UNKNOWN",
]

EXECUTION_MODES: tuple[str, ...] = get_args(ExecutionMode)

MODE_UNKNOWN = "UNKNOWN"
MODE_MATERIAL_CHECK = "MATERIAL_CHECK"
MODE_VALUE_COMPARE = "VALUE_COMPARE"
MODE_CROSS_SCOPE_COMPARE = "CROSS_SCOPE_COMPARE"
MODE_AGGREGATE = "AGGREGATE"
MODE_DERIVE_RISK = "DERIVE_RISK"
MODE_EXTRACT = "EXTRACT"
MODE_CALCULATE = "CALCULATE"
MODE_REPORT = "REPORT"

EXECUTION_MODE_LABELS: dict[str, str] = {
    MODE_MATERIAL_CHECK: "材料检查",
    MODE_VALUE_COMPARE: "字段比较",
    MODE_CROSS_SCOPE_COMPARE: "跨范围比较",
    MODE_AGGREGATE: "组合汇总",
    MODE_DERIVE_RISK: "风险推导",
    MODE_EXTRACT: "字段抽取",
    MODE_CALCULATE: "计算",
    MODE_REPORT: "报告生成",
    MODE_UNKNOWN: "未判定",
}

# 依赖仍是硬约束；这里只在同层可执行项之间给出稳定顺序。
EXECUTION_MODE_ORDER: dict[str, int] = {
    MODE_EXTRACT: 0,
    MODE_CALCULATE: 1,
    MODE_MATERIAL_CHECK: 2,
    MODE_VALUE_COMPARE: 3,
    MODE_CROSS_SCOPE_COMPARE: 4,
    MODE_AGGREGATE: 5,
    MODE_DERIVE_RISK: 6,
    MODE_REPORT: 7,
    MODE_UNKNOWN: 8,
}

# 只按来源规则结果执行、不读取业务字段/材料的动作。
SOURCE_ONLY_MODES = frozenset({MODE_AGGREGATE, MODE_DERIVE_RISK})
# 材料级检查：给材料与产物，不给字段事实。
MATERIAL_ONLY_MODES = frozenset({MODE_MATERIAL_CHECK})


def execution_mode_label(mode: str | None) -> str:
    return EXECUTION_MODE_LABELS.get(str(mode or MODE_UNKNOWN), str(mode or MODE_UNKNOWN))
