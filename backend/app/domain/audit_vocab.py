"""审核链路的共享词汇：产物来源声明与材料类型状态。

取值同时被编译期与运行期使用，只能有一份定义。本模块不依赖 services，
供 domain、services 与 operators 共同引用。
"""

from typing import Any

from ..common.exceptions import BusinessError


# 字段「产物来源」列唯一受支持的取值：印章识别产物。
SEAL_ARTIFACT_SOURCE = "SEAL"

# 字段「期间口径」「报表口径」的不适用取值，由当前业务是否涉及相应口径决定。
# 必须是显式取值而不是 NULL——统一事实表的「当前版本」唯一键由这几个维度用 CONCAT 拼成，
# MySQL 的 CONCAT 遇 NULL 会整体返回 NULL，唯一键随之失效，同一字段可有多条「当前」事实。
# 它只表示不适用，不等于任何具体口径；计算参数可使用它定位不涉及该口径的事实。
SCOPE_NOT_APPLICABLE = "NOT_APPLICABLE"


def declared_artifact_source(item: dict[str, Any]) -> str | None:
    """字段声明的产物来源；未声明返回 None。不认识的来源明确报错，不当成未声明。"""
    declared = item.get("artifact_source")
    if declared in (None, ""):
        return None
    source = str(declared)
    if source != SEAL_ARTIFACT_SOURCE:
        raise BusinessError(
            "FIELD_ARTIFACT_SOURCE_UNSUPPORTED",
            f"字段“{item.get('name') or item.get('field_key')}”声明的产物来源“{source}”当前不受支持，"
            "系统无法确定该字段应取自哪种处理产物",
            details={"field_key": item.get("field_key"), "field_name": item.get("name")},
        )
    return source


# 材料位类型不可用的两种来源，必须分开：
#   TYPE_REVIEW   类型待复核，尚未由人定案（人工裁决为证据不足，或旧自动识别写入且从未裁决）
#   TYPE_REJECTED 类型已否，人工已明确确认与材料位不符
# 旧值 TYPE_MISMATCH 无法区分两者，一律按待复核读取（它只可能由已下线的自动识别写入），
# 沿用旧名会让从未裁决过的材料位被当成人工否定结论。
MATERIAL_TYPE_REVIEW = "TYPE_REVIEW"
MATERIAL_TYPE_REJECTED = "TYPE_REJECTED"
# 历史状态值：只用于读取旧记录。
LEGACY_TYPE_MISMATCH = "TYPE_MISMATCH"
# 下游「材料类型不可用」判定用的完整集合：两种状态的法律后果相同，但展示文案不同。
MATERIAL_TYPE_UNAVAILABLE_STATUSES = (MATERIAL_TYPE_REVIEW, MATERIAL_TYPE_REJECTED, LEGACY_TYPE_MISMATCH)


def is_type_unavailable(status: str | None) -> bool:
    """材料位的类型是否不可用（待复核或已确认不符）。"""
    return status in MATERIAL_TYPE_UNAVAILABLE_STATUSES


def read_material_type_status(status: str | None) -> str | None:
    """把历史状态值归一成当前语义：旧的 `TYPE_MISMATCH` 是自动识别留下的、从未裁决。"""
    return MATERIAL_TYPE_REVIEW if status == LEGACY_TYPE_MISMATCH else status
