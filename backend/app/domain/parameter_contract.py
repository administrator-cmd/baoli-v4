"""规则参数契约常量：算子参数的取值集合与口径，**叶子模块**。

为什么单独成文件
----------------
工具目录与 manifest 编译器共用这些常量，保证模型参数 Schema 与编译期校验一致。

但 `manifest.py` 导入了 `capabilities`，而 `capabilities` 会导出工具目录。
若 `tool_catalog` 反过来导入 `manifest`，就形成
`manifest → capabilities → tool_catalog → manifest` 的模块级环。
因此这些常量必须住在一个**谁都不依赖**的叶子模块里。

本模块保持为不依赖其它领域模块的叶子模块，避免形成模块级环。
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
