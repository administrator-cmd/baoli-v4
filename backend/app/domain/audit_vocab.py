"""审核链路的共享词汇：产物来源声明、编号与材料类型状态。

这里的每个取值都同时被「编译期」与「运行期」使用，因此必须只有一份定义：

- `SEAL_ARTIFACT_SOURCE` / `declared_artifact_source`：字段声明的产物来源。
  字段抽取与主体核对都按它确定取值来源；两处各写一份会让「声明了印章来源的字段」
  在一侧被当成普通字段，从错误的证据源取值，而这一偏差不会出现在任何结果里。
- `public_no`：对外编号（任务号、证据号、复核号……），见 `domain/identity`。
- 材料位的类型状态：`TYPE_REVIEW` 与 `TYPE_REJECTED` 是两种来源不同的结论，
  下游法律后果相同但业务含义必须分开；具体口径见下方 `MATERIAL_TYPE_*` 常量处。

本模块不依赖 services，供 domain、services 与 operators 共同引用。
"""

from typing import Any

from ..common.exceptions import BusinessError


# 字段「产物来源」列唯一受支持的取值：印章识别产物。
SEAL_ARTIFACT_SOURCE = "SEAL"

# 字段的「期间口径」「报表口径」的**不适用**取值。
#
# 合同场景没有「本期 / 上期」「合并 / 母公司」这些概念，03_字段 的这两列留空即表达
# 「这个字段没有期间或报表口径」。它必须是一个**显式取值**，不能是 NULL：
# 统一事实表的「当前版本」唯一键由 (字段编号, 材料, 期间, 报表口径, 主体范围) 用
# CONCAT 拼成，而 MySQL 的 CONCAT 遇到 NULL 会整体返回 NULL——唯一键随之失效，
# 同一个字段可以出现多条「当前」事实。
#
# 它只表示「不适用」，**不等于**任何具体口径：计算参数不能拿它定位事实，
# 发布编译会为此明确报错（见 manifest.required_field_scope 的调用处）。
SCOPE_NOT_APPLICABLE = "NOT_APPLICABLE"


def declared_artifact_source(item: dict[str, Any]) -> str | None:
    """字段声明的产物来源；未声明返回 None。

    声明了本系统不认识的来源时明确报错：把无法落实的声明当成“未声明”，字段会从
    错误的证据源取值，而这一偏差不会出现在任何结果或日志里。
    """
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


# 材料位的「类型不可用」状态，两种来源必须区分开：
#
#   TYPE_REVIEW   类型待复核：**尚未由人定案**。来源是人工裁决的“证据不足”，
#                 或 2026-09-16 之前由自动识别写入、至今从未由人裁决的旧值。
#   TYPE_REJECTED 类型已否：**人工已明确确认与材料位不符**，是确定的人工结论。
#
# 借用旧值 `TYPE_MISMATCH` 表达其中任何一种都会丢掉“这条到底是不是人定的”这一事实：
# 旧记录里的 `TYPE_MISMATCH` 无法区分两者，沿用旧名会让从未裁决过的材料位被当成人工否定结论。
# 因此旧的 `TYPE_MISMATCH` 一律按**待复核**读取（它只可能由已下线的自动识别写入），
# 写入侧只产生上面两个新值。
MATERIAL_TYPE_REVIEW = "TYPE_REVIEW"
MATERIAL_TYPE_REJECTED = "TYPE_REJECTED"
# 历史状态值：只用于读取旧记录，不再作为新的写入值。
LEGACY_TYPE_MISMATCH = "TYPE_MISMATCH"
# 下游「依赖的材料类型不可用」判定用的完整集合：法律后果相同（规则按证据不足处理），
# 但业务含义与展示文案不同，所以状态本身保持两个。
MATERIAL_TYPE_UNAVAILABLE_STATUSES = (MATERIAL_TYPE_REVIEW, MATERIAL_TYPE_REJECTED, LEGACY_TYPE_MISMATCH)


def is_type_unavailable(status: str | None) -> bool:
    """材料位的类型是否不可用（待复核或已确认不符）。"""
    return status in MATERIAL_TYPE_UNAVAILABLE_STATUSES


def read_material_type_status(status: str | None) -> str | None:
    """把历史状态值归一成当前语义：旧的 `TYPE_MISMATCH` 是自动识别留下的、从未裁决。"""
    return MATERIAL_TYPE_REVIEW if status == LEGACY_TYPE_MISMATCH else status
