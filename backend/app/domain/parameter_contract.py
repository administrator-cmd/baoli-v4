"""规则参数契约常量：算子参数的取值集合与口径，叶子模块。

单独成文件的原因：工具目录与 manifest 编译器共用这些常量，保证模型参数 Schema 与
编译期校验一致；但 `manifest.py` 导入了 `capabilities`，而 `capabilities` 会导出
工具目录，若 `tool_catalog` 反过来导入 `manifest` 就形成
`manifest → capabilities → tool_catalog → manifest` 的模块级环。
因此这些常量必须住在一个谁都不依赖的叶子模块里。
"""

from __future__ import annotations

from typing import Literal, get_args


ReportContentType = Literal["FIELD_DETAIL", "AUDIT_RESULT", "FIELD_AND_RESULT", "RISK_LIST", "CONCLUSION"]
ReportResultSort = Literal["RISK_LEVEL_DESC", "MATERIAL_ORDER", "MATERIAL_ORDER_RISK_LEVEL_DESC"]
REPORT_CONTENT_TYPES = frozenset(get_args(ReportContentType))
REPORT_RESULT_SORTS = frozenset(get_args(ReportResultSort))
# 同一 report_code 下这些属性属于整份报告，章节属性（名称、内容、顺序等）不共享。
REPORT_SHARED_PROPERTIES = frozenset({"report_name", "material_refs", "formats", "result_sort", "risk_level_order"})

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
