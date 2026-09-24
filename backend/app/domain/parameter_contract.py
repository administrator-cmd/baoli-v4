"""规则参数契约常量：算子参数的取值集合与口径，**叶子模块**。

为什么单独成文件
----------------
这些常量原先定义在 `domain/manifest.py`。工具目录（`domain/tool_catalog.py`）
要为每个算子生成参数 JSON Schema，Schema 的枚举取值必须来自这里——不能各写一份，
否则模型看到的可选值与编译期接受的取值会漂移。

但 `manifest.py` 导入了 `capabilities`，而 `capabilities` 会导出工具目录。
若 `tool_catalog` 反过来导入 `manifest`，就形成
`manifest → capabilities → tool_catalog → manifest` 的模块级环。
因此这些常量必须住在一个**谁都不依赖**的叶子模块里。

同样的处理在本仓库已有先例：`stable_key` 移到 `domain/identity.py`、
`quality_checks` 并入 `domain/file_quality.py`，都是为了断开 domain 内部的环。

消费方不需要改动：`manifest.py` 继续从本模块导入并转发这些名字，
既有的 `from .manifest import DATE_COMPARE_OPERATORS` 一类引用照常可用。
"""

from __future__ import annotations

# 文件处理产物类型：材料质量检查据此声明依赖哪个产物。
DOCUMENT_ARTIFACT_TYPES = {"MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "OCR", "SEAL"}

# 日期比较方式。GT/LT 是严格比较，只用于日期；数值比较不允许它们。
DATE_COMPARE_OPERATORS = {"EQ", "GTE", "LTE", "GT", "LT"}
# 日期差值只以天为单位：填写规范规定「日期差值以天为单位」，不接受月、年这类不等长单位。
DATE_THRESHOLD_UNIT = "天"

# 业务配置状态：停用行不参与执行。它是业务口径，不是系统能力声明或运行结果。
CONFIG_STATUS_DISABLED = "DISABLED"
# 待确认：非停用但业务口径尚未确认。它能保存为草稿，但不能进入已发布配置。
CONFIG_STATUS_PENDING = "PENDING"

# 数值比较方式。ABS_DIFF_LTE 的语义是「等于且绝对差不超过阈值」。
DECIMAL_COMPARE_OPERATORS = {"EQ", "GTE", "LTE", "ABS_DIFF_LTE"}
# 加总比较只支持数值的等于/不大于/不小于：加法不能用于日期、文本或条款。
DECIMAL_SUM_COMPARE_OPERATORS = {"EQ", "GTE", "LTE"}

# 风险等级。旧格式（v1/v2）沿用 PM 原「高/中/低/待确认」，节点版（v3）用
# 「最高/一级/二级/三级」。两套标签都是已确认的业务取值，必须同时保留：
# 只接受旧集合会直接拒绝节点版风险行，「一级」这类真实等级根本发布不了。
LEGACY_RISK_LEVELS = ("高", "中", "低", "待确认")
NODE_RISK_LEVELS = ("最高", "一级", "二级", "三级")
RISK_LEVELS = set(LEGACY_RISK_LEVELS) | set(NODE_RISK_LEVELS)
# 等级排序口径（由高到低）。仅用于同等级内排序展示，不做等级之间的业务映射：
# PM 原「高/低」与四级标签的对应关系仍未确认，系统不代填。
LEGACY_RISK_LEVEL_ORDER = {"高": 0, "中": 1, "低": 2, "待确认": 3}
NODE_RISK_LEVEL_ORDER = {"最高": 0, "一级": 1, "二级": 2, "三级": 3}
