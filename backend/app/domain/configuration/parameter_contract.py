"""报告展示与规则算子标签的取值常量，不约束规则业务合理性。"""


from __future__ import annotations

from typing import Literal, get_args

from app.domain.materials import file_quality

ReportContentType = Literal["FIELD_DETAIL", "AUDIT_RESULT", "FIELD_AND_RESULT", "RISK_LIST", "CONCLUSION"]
ReportResultSort = Literal["RISK_LEVEL_DESC", "MATERIAL_ORDER", "MATERIAL_ORDER_RISK_LEVEL_DESC"]
REPORT_CONTENT_TYPES = frozenset(get_args(ReportContentType))
REPORT_RESULT_SORTS = frozenset(get_args(ReportResultSort))
# 同一 report_code 下这些属性属于整份报告，章节属性（名称、内容、顺序等）不共享。
REPORT_SHARED_PROPERTIES = frozenset({"report_name", "material_refs", "formats", "result_sort", "risk_level_order"})

# 文件处理产物类型：材料质量检查据此声明依赖哪个产物。
DOCUMENT_ARTIFACT_TYPES = {"MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "OCR", "SEAL"}

# 规则算子与工具共用的业务标签；退出模型工具调用后仍用于规则配置、编译和历史结果展示。
OPERATOR_LABELS: dict[str, str] = {
    "MATERIAL_EXISTS": "材料是否齐全",
    "FIELD_EQUAL": "字段是否一致",
    "FIELD_NOT_EQUAL": "字段应当不同",
    "DATE_COMPARE": "日期先后比较",
    "DECIMAL_COMPARE": "数值比较",
    "DECIMAL_SUM_COMPARE": "数值加总比较",
    "DECIMAL_CN_EQUAL": "大小写金额一致性",
    "FIELD_NOT_EMPTY": "字段非空",
    "CLAUSE_ABSENT": "条款不应存在",
    "RISK_TRIGGER": "按场景条件判断风险",
    **{item: file_quality.label_of(item) for item in sorted(file_quality.KNOWN_QUALITY_ITEMS)},
}
