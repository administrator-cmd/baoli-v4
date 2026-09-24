"""统一规则库中材料、字段、检查、风险与报告 Sheet 的确定性解析。

设计要点（与填写规范逐条对应）：

- 每个工作表只有一张连续表：第一行表头，之后一行一个业务对象。解析只按表头名称建立
  列映射，不依赖列位置，列顺序变化不改变含义。
- 「是否生效」填「否」的行保留原始来源，不形成可执行候选。Excel 里没有「配置状态」列。
- 跨表引用支持业务名称或编号；名称在当前规则库内必须唯一，解析后统一转换为内部编号。
  交叉校验和报告按材料作用域解析字段，内部校验的一行可用于多个材料。
  解析不出时明确报错，不使用向量相似度或名称猜测。
- 引用完整性在整份规则库范围内检查：未知引用、跨材料错误引用、停用依赖都指向
  Sheet / 行 / 列 / 原值。
- 本模块不做任何模型调用：格式识别、表头映射、编号、引用、清单、加总式与显式参数
  全部由代码确定性完成。RAG 不参与场景导入。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from openpyxl.utils.cell import coordinate_from_string, get_column_letter

from ..errors import BusinessError
from . import file_quality, unified_profile
from .audit_vocab import SCOPE_NOT_APPLICABLE
from .manifest import (
    APPLICABILITY_LABELS,
    label_resolver,
    validate_rule_parameters,
)
from .proposals import coverage_totals
from .rule_expression import (
    CellLocation,
    LIST_SEPARATOR,
    check_business_name,
    parse_field_expression,
    parse_non_negative_decimal,
    parse_positive_int,
    split_list,
)
from .workbook import WorkbookIR


# 业务配置状态。它不是 PM 原状态，也不是系统运行/发布状态。
# Excel 里只有「是否生效」（是/否），因此本模块只产出 CLEAR 与 DISABLED；
# PENDING（待确认）是系统内部状态，不再由 Excel 填写。
STATUS_CLEAR = "CLEAR"
STATUS_DISABLED = "DISABLED"

# 01_材料位 只声明材料位本身：材料编号、材料名称、是否必传、是否生效、备注。
# 8018 的材料类型**不在这里声明**：用户在界面上看不到 8018 的类型目录，让他在配置表里
# 预先声明一件他不可能知道答案的事没有意义。类型改为上传文件时从真实目录中选择，
# 随本次上传保存（见 services/task_service._verified_material_type）。
CHECK_VALUES = ("是", "否")

# 04/05 的检查方式分组。
TEXT_COMPARISON_OPERATORS = {"FIELD_EQUAL", "DECIMAL_CN_EQUAL"}
DATE_COMPARISON_OPERATORS = {"DATE_LTE", "DATE_GTE"}
DECIMAL_COMPARISON_OPERATORS = {"DECIMAL_EQ", "DECIMAL_LTE", "DECIMAL_GTE"}
EXISTENCE_OPERATORS = {"NOT_EMPTY", "CLAUSE_ABSENT"}
COMPARISON_OPERATORS = (
    TEXT_COMPARISON_OPERATORS
    | DATE_COMPARISON_OPERATORS
    | DECIMAL_COMPARISON_OPERATORS
    | EXISTENCE_OPERATORS
)

# 检查方式 → 规则算子。比较方向进入 parameters.operator，与既有执行端契约一致：
# DATE_COMPARE 用 EQ/GTE/LTE/GT/LT；DECIMAL_COMPARE 用 EQ/GTE/LTE/ABS_DIFF_LTE。
OPERATOR_TO_ENGINE: dict[str, str] = {
    "FIELD_EQUAL": "FIELD_EQUAL",
    "DECIMAL_CN_EQUAL": "DECIMAL_CN_EQUAL",
    "DATE_LTE": "DATE_COMPARE",
    "DATE_GTE": "DATE_COMPARE",
    "DECIMAL_EQ": "DECIMAL_COMPARE",
    "DECIMAL_LTE": "DECIMAL_COMPARE",
    "DECIMAL_GTE": "DECIMAL_COMPARE",
    "NOT_EMPTY": "FIELD_NOT_EMPTY",
    # 条款存在性检查是独立的确定性算子：它判断条款本身是否存在，
    # 不能映射成 LLM_VALIDATE（那要求 instruction 参数，节点版没有该列）。
    "CLAUSE_ABSENT": "CLAUSE_ABSENT",
}
COMPARISON_DIRECTION: dict[str, str] = {
    "DATE_LTE": "LTE",
    "DATE_GTE": "GTE",
    "DECIMAL_EQ": "EQ",
    "DECIMAL_LTE": "LTE",
    "DECIMAL_GTE": "GTE",
}

# 检查方式与字段类型的允许组合。项目与字段类型不匹配属于配置错误，
# 不能交给模型改写，也不能在运行时才发现。
# 检查字段 → 对照内容 的类型组合。
#   单类型集合：两侧都必须属于该类型（文本一致、日期先后、金额比较都要求同型）。
#   二元组组成的集合：显式的左右侧组合，用于「大写金额 ↔ 数字金额」这类真实存在的跨型核对。
#     DECIMAL_CN_EQUAL 的左右两侧类型本来就不同：把小写金额与合同金额（大写）放在一起比较，
#     要求两侧都是大写金额是错的。这里按真实两侧类型放行，换算口径仍保留待确认。
OperatorTypeRule = frozenset[str] | frozenset[tuple[str, str]]

OPERATOR_ALLOWED_TYPES: dict[str, OperatorTypeRule] = {
    "FIELD_EQUAL": frozenset({"STRING"}),
    # 允许「(小写金额, 大写金额)」两种出现次序；两侧不允许同为同一种金额类型。
    "DECIMAL_CN_EQUAL": frozenset(
        {("DECIMAL", "DECIMAL_CN"), ("DECIMAL_CN", "DECIMAL")}
    ),
    "DATE_LTE": frozenset({"DATE"}),
    "DATE_GTE": frozenset({"DATE"}),
    "DECIMAL_EQ": frozenset({"DECIMAL"}),
    "DECIMAL_LTE": frozenset({"DECIMAL"}),
    "DECIMAL_GTE": frozenset({"DECIMAL"}),
    "NOT_EMPTY": frozenset({"STRING", "DECIMAL", "DECIMAL_CN", "DATE", "BOOLEAN", "CLAUSE"}),
    "CLAUSE_ABSENT": frozenset({"CLAUSE"}),
}

# 02_文件处理规则：检查项目、产物依赖、合格要求与实现状态的事实定义在 file_quality，
# 这里只做引用，不再各写一份——两份名单漂移会让「能不能执行」在校验与执行之间不一致。
# 判定实现同在 domain/file_quality.py。
QUALITY_ITEM_REQUIREMENTS = file_quality.QUALITY_ITEM_REQUIREMENTS
QUALITY_ITEM_ARTIFACT = file_quality.QUALITY_ITEM_ARTIFACT
QUALITY_ITEM_LABELS = file_quality.QUALITY_ITEM_LABELS
# 「合格要求」的中文口径与检查项目一样是配置事实，只维护一份，供本模块报错与执行端展示共用。
REQUIREMENT_LABELS = file_quality.QUALITY_REQUIREMENT_LABELS

# 10_报告模板：输出格式。
REPORT_FORMAT_CODES = {"PDF": "PDF", "WORD": "Word", "EXCEL": "Excel"}
# 内容类型 → 章节数据来源。
CONTENT_TYPE_LABELS = {
    "FIELD_DETAIL": "字段明细",
    "AUDIT_RESULT": "审核结果",
    "FIELD_AND_RESULT": "字段及审核结果",
    "RISK_LIST": "风险清单",
    "CONCLUSION": "审核结论",
}
FIELD_LIST_CONTENT_TYPES = {"FIELD_DETAIL", "FIELD_AND_RESULT"}
RESULT_CONTENT_TYPES = {"AUDIT_RESULT", "FIELD_AND_RESULT", "CONCLUSION"}
RESULT_SORT_LABELS = {
    "RISK_LEVEL_DESC": "风险等级降序",
    "MATERIAL_ORDER": "材料顺序",
    "MATERIAL_ORDER_RISK_LEVEL_DESC": "材料顺序；风险等级降序",
}

# 风险成立条件。
RISK_CONDITION_ANY = "ANY_ABNORMAL"
RISK_CONDITION_ALL = "ALL_ABNORMAL"
RISK_CONDITION_LABELS = {
    RISK_CONDITION_ANY: "任一关联检查不合格",
    RISK_CONDITION_ALL: "全部关联检查不合格",
}

# 取值校对：既有 manifest 的风险等级集合只有「高/中/低/待确认」，本格式的检查等级与
# 风险等级使用「最高/一级/二级/三级」。PM 原「高/低」没有明确映射，因此不能凭代码
# 猜测层级关系，也不能把不认识的等级塞进执行契约。系统显式保留原文，并把需要业务
# 确认的映射作为待确认问题提出（见各解析函数里的 ConversionNote）：用户明确作答后
# 才写入执行参数。


@dataclass
class ConversionNote:
    """一条必须留给业务处理的待确认事项，或一条明确的排除说明。

    `resolve` 说明作答如何落到候选对象上，使这些待确认项能通过现有导入裁决入口
    真正被处理（确认口径、指定单位币种、明确停用），而不是只留一句说明：

      target_type / target_ref：候选对象类型与引用；
      property：被裁决的属性（config_status / unit / currency / parameters）；
      mode：REPLACE=作答值就是该属性的完整取值；SET_PARAMETER=作答值并入 base_parameters；
      input_kind：SELECT / TEXT / DECIMAL，决定页面渲染哪种作答控件。
    """

    code: str
    message: str
    sheet_key: str
    sheet_name: str
    row_no: int
    cell: str | None
    column: str
    value: str | None
    question_title: str
    question_options: list[dict[str, Any]] = field(default_factory=list)
    resolve: dict[str, Any] | None = None
    input_kind: str = "SELECT"

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "sheet_key": self.sheet_key,
            "sheet_name": self.sheet_name,
            "row_no": self.row_no,
            "cell": self.cell,
            "column": self.column,
            "value": self.value,
            "question_title": self.question_title,
            "question_options": self.question_options,
            "resolve": self.resolve,
            "input_kind": self.input_kind,
        }


@dataclass
class RowOutcome:
    """一行业务记录的处置结果，供逐行对账使用。"""

    row_no: int
    disposition: str  # CANDIDATE / DISABLED / EXCLUDED / QUESTION
    reason: str | None
    candidate_refs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class SheetReader:
    """按表头名称读取一张业务工作表。

    表头与 profile 的列名必须一一对应：未知列、重复列、缺必需列都在这里明确报错，
    不由模型猜列，也不按列位置解释含义。
    """

    def __init__(
        self,
        *,
        sheet_key: str,
        sheet_name: str,
        role_definition: dict[str, Any],
        header_row: int,
        header_values: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> None:
        self.sheet_key = sheet_key
        self.sheet_name = sheet_name
        self.code_prefix = str(role_definition["code_prefix"])
        self.header_row = header_row
        self.rows = rows
        self.column_keys = unified_profile.map_sheet_columns(
            role_definition,
            header_values,
            sheet_key=sheet_key,
            sheet_name=sheet_name,
            header_row=header_row,
            required_keys={column["key"] for column in role_definition["columns"] if not column.get("optional_column")},
        )
        self.column_names = {column["key"]: column["name"] for column in role_definition["columns"]}
        self.row_required: dict[str, bool] = {
            column["key"]: column.get("row_required", False) for column in role_definition["columns"]
        }
        self.value_labels = {
            column["key"]: label_resolver(column)
            for column in role_definition["columns"]
        }

    def cell(self, row: dict[str, Any], key: str) -> tuple[str | None, str | None]:
        """返回（单元格地址, 原格文本）。列未映射或单元格为空时文本为 None。"""
        header = self.column_keys.get(key)
        if not header:
            return None, None
        column, _ = coordinate_from_string(header)
        address = f"{column}{row['row_no']}"
        raw = row["cells"].get(address)
        if raw in (None, ""):
            return address, None
        return address, str(raw).strip()

    def location(self, row: dict[str, Any], key: str, *, value: str | None = None) -> CellLocation:
        address = None
        header = self.column_keys.get(key)
        if header:
            column, _ = coordinate_from_string(header)
            address = f"{column}{row['row_no']}"
        return CellLocation(
            sheet_key=self.sheet_key,
            sheet_name=self.sheet_name,
            row_no=row["row_no"],
            cell=address,
            column_label=self.column_names.get(key, key),
            value=value,
        )

    def text(self, row: dict[str, Any], key: str) -> str | None:
        return self.cell(row, key)[1]

    def required_text(self, row: dict[str, Any], key: str) -> str:
        _, text = self.cell(row, key)
        if text is None:
            location = self.location(row, key)
            raise BusinessError(
                "RULE_CELL_EMPTY",
                f"工作表「{self.sheet_name}」第 {row['row_no']} 行的「{self.column_names[key]}」为空，"
                "该列是必填列，请补齐后重新上传",
                details=location.detail(),
            )
        return text

    def required_code(self, row: dict[str, Any], key: str) -> str:
        value = self.required_text(row, key)
        if not value.startswith(self.code_prefix) or len(value) == len(self.code_prefix):
            raise BusinessError(
                "RULE_CODE_PREFIX_INVALID",
                f"工作表「{self.sheet_name}」第 {row['row_no']} 行的「{self.column_names[key]}」"
                f"必须以“{self.code_prefix}”开头且包含编号内容",
                details=self.location(row, key, value=value).detail(expected_prefix=self.code_prefix),
            )
        return value

    def enum(self, row: dict[str, Any], key: str, *, required: bool = True) -> str | None:
        _, text = self.cell(row, key)
        if text is None:
            if not required:
                return None
            location = self.location(row, key)
            raise BusinessError(
                "RULE_CELL_EMPTY",
                f"工作表「{self.sheet_name}」第 {row['row_no']} 行的「{self.column_names[key]}」为空，"
                "请按模板下拉取值填写后重新上传",
                details=location.detail(),
            )
        resolved = self.value_labels.get(key, {}).get(text)
        if resolved is None:
            resolved = text if text in (self.value_labels.get(key, {}) or {}) else None
        if resolved is None:
            location = self.location(row, key, value=text)
            # 提示里给**中文标签**：模板下拉里就是这些，业务看到的是它们。
            # `value_labels` 的方向是「内部取值 → 中文标签」，因此取 values。
            labels = self.value_labels.get(key) or {}
            allowed = sorted(str(item) for item in (labels.values() or labels.keys()))
            raise BusinessError(
                "RULE_ENUM_VALUE_INVALID",
                f"工作表「{self.sheet_name}」第 {row['row_no']} 行的「{self.column_names[key]}」"
                f"填写的“{text}”不在模板允许取值内（允许：{'、'.join(allowed)}），请使用下拉选项填写",
                details=location.detail(allowed=allowed),
            )
        return resolved

    def boolean(self, row: dict[str, Any], key: str, *, required: bool = True) -> bool | None:
        _, text = self.cell(row, key)
        if text is None:
            if not required:
                return None
            location = self.location(row, key)
            raise BusinessError(
                "RULE_CELL_EMPTY",
                f"工作表「{self.sheet_name}」第 {row['row_no']} 行的「{self.column_names[key]}」为空，"
                "请填写“是”或“否”后重新上传",
                details=location.detail(),
            )
        if text in {"是", "true", "True", "TRUE"}:
            return True
        if text in {"否", "false", "False", "FALSE"}:
            return False
        location = self.location(row, key, value=text)
        raise BusinessError(
            "RULE_BOOLEAN_VALUE_INVALID",
            f"工作表「{self.sheet_name}」第 {row['row_no']} 行的「{self.column_names[key]}」"
            f"填写的“{text}”不是“是/否”，请修正后重新上传",
            details=location.detail(),
        )

    def business_rows(self) -> list[dict[str, Any]]:
        """表头之后的全部非空业务行（完整读取，不用界面预览截断范围）。"""
        return [row for row in self.rows if row["row_no"] > self.header_row]


@dataclass
class SheetConversion:
    reader: SheetReader
    proposals: dict[str, Any]
    outcomes: list[RowOutcome]
    notes: list[ConversionNote]


def _candidate_ref(prefix: str, sheet_key: str, row_no: int) -> str:
    """候选引用由工作表、真实行号和来源编号确定性生成，不依赖模型、不依赖列顺序。"""
    return f"{prefix}:{sheet_key}:r{row_no}"


def _source_ref(reader: SheetReader, row: dict[str, Any]) -> dict[str, Any]:
    texts: dict[str, str] = {}
    for key, header in reader.column_keys.items():
        column, _ = coordinate_from_string(header)
        address = f"{column}{row['row_no']}"
        raw = row["cells"].get(address)
        if raw not in (None, ""):
            texts[address] = str(raw)
    return {
        "sheet_key": reader.sheet_key,
        "row_no": row["row_no"],
        "cells": sorted(texts),
        "texts": texts,
    }


def _blank_row(row: dict[str, Any], reader: SheetReader) -> bool:
    for header in reader.column_keys.values():
        column, _ = coordinate_from_string(header)
        if row["cells"].get(f"{column}{row['row_no']}") not in (None, ""):
            return False
    return True


def _row_config_status(
    reader: SheetReader, row: dict[str, Any], *, required: bool = False
) -> str | None:
    """该行「是否生效」→ 内部配置状态（已明确 / 停用）。

    Excel 里只有「是否生效」一列（是 / 否），**没有**「配置状态」：业务在表里不表达
    「系统配没配好」，只表达「这一行参不参与审核」。因此这里只会得到 CLEAR 或 DISABLED，
    不再产生 PENDING（待确认是系统内部状态，不由 Excel 填写）。

    用于在读取其余必填列之前判断「停用」：停用行不参与本次规则库，
    它的半成品口径列不该让整份规则库解析失败。取值非法仍然明确报错。
    """
    enabled = reader.boolean(row, "enabled", required=required)
    if enabled is None:
        return None
    return STATUS_CLEAR if enabled else STATUS_DISABLED


def _retain_disabled_row(reader: SheetReader, row: dict[str, Any], outcomes: list[RowOutcome]) -> bool:
    if _row_config_status(reader, row) != STATUS_DISABLED:
        return False
    outcomes.append(RowOutcome(row["row_no"], "DISABLED", "该行已停用，原始填写内容保留在场景草稿中"))
    return True


def _disabled_row_payload(reader: SheetReader, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": _source_ref(reader, row),
        "sheet_name": reader.sheet_name,
        "source_id": reader.text(row, "source_id") or reader.text(row, "report_code"),
        "columns": [
            {
                "key": key,
                "label": label,
                "value": reader.text(row, key) or "",
                "cell": reader.location(row, key).cell,
            }
            for key, label in reader.column_names.items()
        ],
    }


def _disabled_rows_of_conversion(conversion: SheetConversion) -> list[dict[str, Any]]:
    disabled = {outcome.row_no for outcome in conversion.outcomes if outcome.disposition == "DISABLED"}
    return [
        _disabled_row_payload(conversion.reader, row)
        for row in conversion.reader.business_rows()
        if row["row_no"] in disabled
    ]


def convert_disabled_config_row(
    source_row: dict[str, Any], *, profile_version: str,
    material_by_code: dict[str, dict[str, Any]], field_by_code: dict[str, dict[str, Any]],
    field_catalog: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> SheetConversion:
    """将编辑后的停用配置行交回对应的原始 Sheet 解析器。"""
    reader = _disabled_source_reader(source_row, profile_version=profile_version)
    role = source_row["sheet_role"]
    if field_catalog is None:
        field_catalog = _build_field_catalog(list(field_by_code.values()), list(material_by_code.values()))
    if role == "MATERIAL_LIST":
        return _convert_materials(reader)
    if role == "FIELD_EXTRACTION":
        return _convert_fields(reader, material_by_code=material_by_code)
    if role == "REPORT_TEMPLATE":
        return _convert_reports(
            reader, material_by_code=material_by_code, field_by_code=field_by_code,
            field_catalog=field_catalog,
        )
    if role == "MATERIAL_QUALITY":
        return _convert_material_quality(reader, material_by_code=material_by_code)
    if role == "RISK":
        return _convert_risks(reader)
    if role == "INTERNAL_VALIDATE":
        return _convert_internal_validate(
            reader, material_by_code=material_by_code, field_catalog=field_catalog, field_by_code=field_by_code
        )
    return _convert_cross_validate(
        reader, material_by_code=material_by_code, field_catalog=field_catalog, field_by_code=field_by_code
    )


def disabled_check_risk_codes(source_row: dict[str, Any]) -> list[str]:
    """只读取关联编号以计算依赖闭包；选中的行仍由原解析器完整校验。"""
    if source_row["sheet_role"] not in {"MATERIAL_QUALITY", "INTERNAL_VALIDATE", "CROSS_VALIDATE"}:
        raise BusinessError("DISABLED_SOURCE_ROW_ROLE_UNSUPPORTED", "该来源行不是检查规则", status_code=409)
    column = next((item for item in source_row["columns"] if item["key"] == "risk_refs"), None)
    if column is None:
        raise BusinessError("DISABLED_SOURCE_ROW_PROFILE_CHANGED", "停用检查行缺少关联风险列，不能计算依赖")
    value = column["value"]
    source = source_row["source"]
    return split_list(
        value,
        location=CellLocation(
            sheet_key=source["sheet_key"],
            sheet_name=source_row["sheet_name"],
            row_no=source["row_no"],
            cell=column.get("cell"),
            column_label=column["label"],
            value=value,
        ),
    )


def _disabled_source_reader(source_row: dict[str, Any], *, profile_version: str) -> SheetReader:
    profile = unified_profile.load_unified_profile()
    if profile_version != profile["profile_version"]:
        raise BusinessError(
            "DISABLED_SOURCE_ROW_PROFILE_CHANGED",
            "停用来源行使用的模板协议与当前协议不同，不能按新列定义启用",
            status_code=409,
            details={"draft_profile_version": profile_version, "current_profile_version": profile["profile_version"]},
        )
    role = source_row["sheet_role"]
    if role not in {
        "MATERIAL_LIST", "FIELD_EXTRACTION", "MATERIAL_QUALITY", "INTERNAL_VALIDATE",
        "CROSS_VALIDATE", "RISK", "REPORT_TEMPLATE",
    }:
        raise BusinessError(
            "DISABLED_SOURCE_ROW_ROLE_UNSUPPORTED",
            "该工作表的停用行尚无直接启用转换路径",
            status_code=409,
            details={"sheet_role": role},
        )
    definition = unified_profile.sheet_definition(profile, role)
    columns = source_row["columns"]
    if [column["key"] for column in columns] != [column["key"] for column in definition["columns"]]:
        raise BusinessError(
            "DISABLED_SOURCE_ROW_PROFILE_CHANGED",
            "停用来源行的列结构与当前模板协议不同，不能启用",
            status_code=409,
            details={"row_key": source_row["row_key"], "sheet_role": role},
        )
    row_no = source_row["source"]["row_no"]
    if row_no <= 1:
        raise BusinessError(
            "DISABLED_SOURCE_ROW_INVALID",
            "停用来源行的行号无效，不能按工作表记录解析",
            status_code=409,
            details={"row_key": source_row["row_key"], "row_no": row_no},
        )
    header = {
        f"{get_column_letter(index)}1": column["name"]
        for index, column in enumerate(definition["columns"], start=1)
    }
    cells = {
        f"{get_column_letter(index)}{row_no}": column["value"]
        for index, column in enumerate(columns, start=1)
        if column["value"] != ""
    }
    return SheetReader(
        sheet_key=source_row["source"]["sheet_key"],
        sheet_name=source_row["sheet_name"],
        role_definition=definition,
        header_row=1,
        header_values=header,
        rows=[{"row_no": row_no, "cells": cells}],
    )


def _partial_row(row: dict[str, Any], reader: SheetReader) -> list[str]:
    """返回该行实际填写的必填列名；只有一个必填列有内容时视为分组标题或说明行。"""
    filled: list[str] = []
    for key, required in reader.row_required.items():
        if not required:
            continue
        _, text = reader.cell(row, key)
        if text is not None:
            filled.append(reader.column_names[key])
    return filled


def _assert_row_required_filled(reader: SheetReader, row: dict[str, Any]) -> None:
    """主动填写的业务行必须把「行必填列」填齐。

    适用于**已经确定要参与本次规则库**的行（未停用、且不是空白/分组行）。
    这些列是业务口径本身（是否必抽、配置状态这类），缺了它们系统只能静默取默认值——
    而「默认必抽」「默认已明确」都不是业务说过的话。因此明确报出 Sheet、行、列，
    让业务回去补齐，而不是带一个猜测出来的默认值进入已发布配置。
    """
    missing: list[dict[str, Any]] = []
    for key, required in reader.row_required.items():
        if not required:
            continue
        _, text = reader.cell(row, key)
        if text is None:
            location = reader.location(row, key)
            missing.append(
                {
                    "column": reader.column_names[key],
                    "cell": location.cell,
                }
            )
    if missing:
        names = "、".join(f"「{item['column']}」" for item in missing)
        cells = "、".join(item["cell"] for item in missing)
        raise BusinessError(
            "RULE_ROW_REQUIRED_CELL_EMPTY",
            f"工作表「{reader.sheet_name}」第 {row['row_no']} 行缺少必填列 {names}"
            f"（单元格 {cells}）：该行已填写业务内容，不能留空这些口径列",
            details={
                "sheet_key": reader.sheet_key,
                "sheet_name": reader.sheet_name,
                "row_no": row["row_no"],
                "missing": missing,
            },
        )


# 01_材料位


def _convert_materials(reader: SheetReader) -> SheetConversion:
    proposals: dict[str, Any] = {"materials": [], "fields": [], "rules": [], "report_sections": [], "questions": []}
    outcomes: list[RowOutcome] = []
    notes: list[ConversionNote] = []
    seen_names: dict[str, int] = {}
    seen_ids: dict[str, int] = {}
    for row in reader.business_rows():
        if _blank_row(row, reader):
            outcomes.append(RowOutcome(row["row_no"], "EXCLUDED", "该行在所有业务列上都没有内容"))
            continue
        if _retain_disabled_row(reader, row, outcomes):
            continue
        filled = _partial_row(row, reader)
        if len(filled) < 2:
            outcomes.append(
                RowOutcome(row["row_no"], "EXCLUDED", f"该行只填写了「{filled[0] if filled else '无'}」，未形成完整业务记录")
            )
            continue
        source_id = reader.required_code(row, "source_id")
        name_location = reader.location(row, "name", value=reader.text(row, "name"))
        name = check_business_name(reader.required_text(row, "name"), location=name_location)
        if name in seen_names:
            raise BusinessError(
                "MATERIAL_NAME_DUPLICATED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的材料名称“{name}”与第 {seen_names[name]} 行重复，"
                "材料名称必须唯一，请改名后重新上传",
                details=name_location.detail(),
            )
        if source_id in seen_ids:
            raise BusinessError(
                "MATERIAL_ID_DUPLICATED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的材料编号“{source_id}”与第 {seen_ids[source_id]} 行重复，"
                "编号必须唯一，请修正后重新上传",
                details=reader.location(row, "source_id", value=source_id).detail(),
            )
        seen_names[name] = row["row_no"]
        seen_ids[source_id] = row["row_no"]
        status = _row_config_status(reader, row)
        _assert_row_required_filled(reader, row)
        required = reader.boolean(row, "required")
        remark = reader.text(row, "remark") or ""
        ref = _candidate_ref("mat", reader.sheet_key, row["row_no"])
        proposals["materials"].append(
            {
                "candidate_ref": ref,
                "source_id": source_id,
                "name": name,
                "required": bool(required),
                "description": remark,
                "config_status": status,
                "source": _source_ref(reader, row),
            }
        )

        outcomes.append(RowOutcome(row["row_no"], "CANDIDATE", None, [ref]))
    return SheetConversion(reader, proposals, outcomes, notes)


def _reference_index(items: list[dict[str, Any]], *, kind: str) -> dict[str, dict[str, Any]]:
    """为业务对象建立「编号 + 名称」索引，名称重复时不允许导入继续猜。"""
    index: dict[str, dict[str, Any]] = {}
    for item in items:
        for value in (item.get("source_id"), item.get("name")):
            key = str(value or "").strip()
            if not key:
                continue
            existing = index.get(key)
            if existing is not None and existing["candidate_ref"] != item["candidate_ref"]:
                raise BusinessError(
                    f"{kind.upper()}_REFERENCE_AMBIGUOUS",
                    f"{kind}引用值“{key}”同时对应多个对象，请修改冲突的名称或编号",
                    details={"value": key, "kind": kind},
                )
            index[key] = item
    return index


# 03_字段


def _convert_fields(
    reader: SheetReader,
    *,
    material_by_code: dict[str, dict[str, Any]],
) -> SheetConversion:
    proposals: dict[str, Any] = {"materials": [], "fields": [], "rules": [], "report_sections": [], "questions": []}
    outcomes: list[RowOutcome] = []
    notes: list[ConversionNote] = []
    seen_scoped: dict[tuple[str, str], int] = {}
    seen_ids: dict[str, int] = {}
    for row in reader.business_rows():
        if _blank_row(row, reader):
            outcomes.append(RowOutcome(row["row_no"], "EXCLUDED", "该行在所有业务列上都没有内容"))
            continue
        if _retain_disabled_row(reader, row, outcomes):
            continue
        filled = _partial_row(row, reader)
        if len(filled) < 2:
            outcomes.append(
                RowOutcome(row["row_no"], "EXCLUDED", f"该行只填写了「{filled[0] if filled else '无'}」，未形成完整业务记录")
            )
            continue
        source_id = reader.required_code(row, "source_id")
        material_code = reader.required_text(row, "material_ref")
        material = material_by_code.get(material_code)
        if material is None:
            location = reader.location(row, "material_ref", value=material_code)
            raise BusinessError(
                "FIELD_MATERIAL_UNKNOWN",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「所属材料」“{material_code}”"
                "不在 01_材料位 中，请核对材料名称或编号后重新上传",
                details=location.detail(known=sorted(material_by_code)),
            )
        name_location = reader.location(row, "name", value=reader.text(row, "name"))
        name = check_business_name(reader.required_text(row, "name"), location=name_location)
        scoped = (material["candidate_ref"], name)
        if scoped in seen_scoped:
            raise BusinessError(
                "FIELD_NAME_DUPLICATED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行在材料“{material['name']}”下重复定义了字段“{name}”"
                f"（与第 {seen_scoped[scoped]} 行重复），同一材料内字段名称必须唯一",
                details=name_location.detail(),
            )
        if source_id in seen_ids:
            raise BusinessError(
                "FIELD_ID_DUPLICATED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的字段编号“{source_id}”与第 {seen_ids[source_id]} 行重复",
                details=reader.location(row, "source_id", value=source_id).detail(),
            )
        seen_scoped[scoped] = row["row_no"]
        seen_ids[source_id] = row["row_no"]
        status = _row_config_status(reader, row)
        _assert_row_required_filled(reader, row)
        value_type = reader.enum(row, "value_type")
        unit = reader.text(row, "unit")
        currency = reader.text(row, "currency")
        required = reader.boolean(row, "required", required=False)
        instruction = reader.text(row, "instruction")
        # 产物来源是可选列：没有该列时字段按常规字段抽取处理，不推断产物归属。
        artifact_source = reader.enum(row, "artifact_source", required=False)
        # 取数期间与报表口径：**由字段自己声明**。计算参数不再重复声明这两项，
        # 因此字段这一行就是「这个数取的是哪一期、哪个口径」的唯一来源。
        #
        # 这两列可以留空——合同场景本来就没有期间与合并/母公司的概念。
        # 留空落成**显式的「不适用」**，不是 NULL：统一事实表的当前版本唯一键
        # 由这几个口径 CONCAT 而成，NULL 会让唯一键整体失效（见 audit_vocab）。
        period_scope = reader.enum(row, "period_scope", required=False) or SCOPE_NOT_APPLICABLE
        statement_scope = reader.enum(row, "statement_scope", required=False) or SCOPE_NOT_APPLICABLE
        ref = _candidate_ref("fld", reader.sheet_key, row["row_no"])
        proposals["fields"].append(
            {
                "candidate_ref": ref,
                "source_id": source_id,
                "name": name,
                "value_type": value_type,
                "unit": unit,
                "currency": currency,
                "material_ref": material["candidate_ref"],
                "required": bool(required),
                "instruction": instruction or "",
                "artifact_source": artifact_source,
                # 期间与报表口径随字段进入候选，发布期写进编译产物，
                # 执行期按它建立字段事实的定位键。
                "period_scope": period_scope,
                "statement_scope": statement_scope,
                "config_status": status,
                "source": _source_ref(reader, row),
            }
        )
        if value_type in {"DECIMAL", "DECIMAL_CN"} and not unit:
            notes.append(
                ConversionNote(
                    code="MONEY_UNIT_MISSING",
                    message=(
                        f"字段「{material['name']}·{name}」是{('大写金额' if value_type == 'DECIMAL_CN' else '金额')}，"
                        "但没有填写归一化单位。系统不默认人民币或元：缺少单位时该字段的数值比较与加总不可执行。"
                    ),
                    sheet_key=reader.sheet_key,
                    sheet_name=reader.sheet_name,
                    row_no=row["row_no"],
                    cell=reader.location(row, "unit").cell,
                    column=reader.column_names["unit"],
                    value=None,
                    question_title=f"字段「{material['name']}·{name}」缺少金额单位，请填写归一化单位",
                    question_options=[],
                    input_kind="TEXT",
                    resolve={"mode": "REPLACE", "target_type": "FIELD", "target_ref": ref, "property": "unit"},
                )
            )
        if value_type in {"DECIMAL", "DECIMAL_CN"} and not currency:
            notes.append(
                ConversionNote(
                    code="MONEY_CURRENCY_MISSING",
                    message=(
                        f"字段「{material['name']}·{name}」是{('大写金额' if value_type == 'DECIMAL_CN' else '金额')}，"
                        "但没有填写币种。系统不默认人民币，币种未知时不做数值比较或求和。"
                    ),
                    sheet_key=reader.sheet_key,
                    sheet_name=reader.sheet_name,
                    row_no=row["row_no"],
                    cell=reader.location(row, "currency").cell,
                    column=reader.column_names["currency"],
                    value=None,
                    question_title=f"字段「{material['name']}·{name}」缺少币种，请填写明确币种或“按材料原文”",
                    question_options=[],
                    input_kind="TEXT",
                    resolve={"mode": "REPLACE", "target_type": "FIELD", "target_ref": ref, "property": "currency"},
                )
            )

        outcomes.append(RowOutcome(row["row_no"], "CANDIDATE", None, [ref]))
    return SheetConversion(reader, proposals, outcomes, notes)


# 字段引用解析（04/05 共用）


def _build_field_catalog(
    field_proposals: list[dict[str, Any]],
    material_proposals: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """(材料编号, 字段名称) → 字段候选。字段引用必须带材料作用域。"""
    material_codes = {item["candidate_ref"]: item["source_id"] for item in material_proposals}
    catalog: dict[tuple[str, str], dict[str, Any]] = {}
    for field in field_proposals:
        material_code = material_codes.get(field["material_ref"])
        if material_code is None or not field.get("name"):
            continue
        catalog[(material_code, field["name"])] = field
    return catalog


def _resolve_field_names(
    names: list[str],
    *,
    material: dict[str, Any],
    catalog: dict[tuple[str, str], dict[str, Any]],
    reader: SheetReader,
    row: dict[str, Any],
    column_key: str,
    original: str,
    field_by_code: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """把字段名称解析成规则输入候选。

    输出使用契约字段名 `field_ref` / `material_ref`：`RuleInputProposal` 与
    manifest 编译都按这两个键读取，写成 `field_key` / `material_key` 会在
    Pydantic 严格校验下直接失败（或用额外字段掩盖错误）。
    """
    resolved: list[dict[str, Any]] = []
    for name in names:
        field = catalog.get((material["source_id"], name))
        coded = field_by_code.get(name) if field_by_code is not None else None
        if coded is not None and field is not None and coded["candidate_ref"] != field["candidate_ref"]:
            raise BusinessError(
                "RULE_FIELD_REF_AMBIGUOUS",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的字段引用“{name}”同时是字段编号和其他字段名称，无法确定目标",
                details=reader.location(row, column_key, value=name).detail(),
            )
        if coded is not None and coded["material_ref"] == material["candidate_ref"]:
            field = coded
        if field is None:
            location = reader.location(row, column_key, value=original)
            raise BusinessError(
                "RULE_FIELD_REF_UNKNOWN",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「{reader.column_names[column_key]}」引用的字段"
                f"“{name}”在材料“{material['name']}”下不存在，请核对 03_字段 的字段编号或字段名称后重新上传",
                details=location.detail(material_code=material["source_id"], material_name=material["name"]),
            )
        resolved.append({"field_ref": field["candidate_ref"], "material_ref": material["candidate_ref"]})
    return resolved


def _resolve_field_codes(
    codes: list[str],
    *,
    material: dict[str, Any],
    field_by_code: dict[str, dict[str, Any]],
    reader: SheetReader,
    row: dict[str, Any],
    column_key: str,
    field_catalog: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for code in codes:
        field = field_by_code.get(code)
        named = field_catalog.get((material["source_id"], code)) if field_catalog is not None else None
        if field is not None and named is not None and field["candidate_ref"] != named["candidate_ref"]:
            raise BusinessError(
                "RULE_FIELD_REF_AMBIGUOUS",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的字段引用“{code}”同时是字段编号和其他字段名称，无法确定目标",
                details=reader.location(row, column_key, value=code).detail(),
            )
        if field is None:
            field = named
        if field is None:
            raise BusinessError(
                "RULE_FIELD_REF_UNKNOWN",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的字段编号或名称“{code}”不在 03_字段 中",
                details=reader.location(row, column_key, value=code).detail(),
            )
        if field["material_ref"] != material["candidate_ref"]:
            raise BusinessError(
                "RULE_FIELD_MATERIAL_MISMATCH",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的字段“{code}”"
                f"不属于材料“{material['source_id']}”",
                details=reader.location(row, column_key, value=code).detail(
                    material_code=material["source_id"],
                ),
            )
        resolved.append({"field_ref": field["candidate_ref"], "material_ref": material["candidate_ref"]})
    return resolved


def _field_for_reference(
    value: str,
    *,
    material: dict[str, Any],
    field_by_code: dict[str, dict[str, Any]],
    field_catalog: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """按字段编号或当前材料下的字段名称取字段对象。"""
    field = field_by_code.get(value) or field_catalog.get((material["source_id"], value))
    if field is None or field["material_ref"] != material["candidate_ref"]:
        raise KeyError(value)
    return field


# 04_文件内部校验规则 / 05_文件交叉校验规则


def _comparison_parameters(
    *,
    operator: str,
    direction: str | None,
    threshold: Decimal | None,
    threshold_unit: str | None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    if direction is not None:
        parameters["operator"] = direction
    if threshold is not None:
        # 差值以十进制字符串保存：不使用浮点，避免 0.1+0.2 这类精度损失进入执行。
        parameters["threshold"] = format(threshold, "f")
    if threshold_unit:
        parameters["threshold_unit"] = threshold_unit
    return parameters


def _engine_of(
    *,
    operator: str,
    addend_count: int,
) -> str:
    """把业务的「合格要求」映射成执行算子。

    对照内容是加总式（两个及以上加数）且比较方式是数值的等于/不大于/不小于时，
    使用加总比较算子：左侧一个字段与右侧加数之和比较，右侧加数全部保留。
    单加数仍然走一对一比较，语义与旧格式一致。
    """
    if operator in DECIMAL_COMPARISON_OPERATORS and addend_count > 1:
        return "DECIMAL_SUM_COMPARE"
    return OPERATOR_TO_ENGINE[operator]


def _check_operator_shape(
    *,
    reader: SheetReader,
    row: dict[str, Any],
    operator: str,
    check_level: str | None,
    right_names: list[str],
    original_right: str | None,
    threshold: Decimal | None,
    threshold_unit: str | None,
) -> None:
    """检查方式、对照内容与允许差值三者必须互相匹配，不匹配属于配置错误。"""
    right_column = "right_field_ref"
    location = reader.location(row, right_column, value=original_right)
    if operator in TEXT_COMPARISON_OPERATORS | DATE_COMPARISON_OPERATORS | DECIMAL_COMPARISON_OPERATORS:
        if not right_names:
            raise BusinessError(
                "RULE_COMPARISON_TARGET_REQUIRED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「合格要求」是"
                f"“{reader.column_names['operator']}”，但「对照内容」为空：比较规则必须写明对照的字段",
                details=location.detail(),
            )
    if operator in DECIMAL_COMPARISON_OPERATORS and len(right_names) > 1:
        # 加总只用于数值比较；加数的单位与币种必须在编译期一致，不能到运行时才发现。
        if threshold is None:
            raise BusinessError(
                "RULE_THRESHOLD_REQUIRED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行对多个加数求和，必须填写非负的「允许差值」",
                details=reader.location(row, "threshold").detail(),
            )
    if operator in DATE_COMPARISON_OPERATORS and len(right_names) > 1:
        raise BusinessError(
            "RULE_ADDEND_UNSUPPORTED",
            f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「对照内容」填了多个字段："
            "加法只用于数值的等于、不大于、不小于，日期比较只能一对一",
            details=location.detail(),
        )
    if operator == "CLAUSE_ABSENT" and right_names:
        raise BusinessError(
            "RULE_CLAUSE_TARGET_UNEXPECTED",
            f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「合格要求」是“条款不应存在”，"
            "「对照内容」应留空（判断的是条款本身是否存在，不是与另一个字段比较）",
            details=location.detail(),
        )
    if operator in {"NOT_EMPTY", "CLAUSE_ABSENT"}:
        if threshold is not None:
            raise BusinessError(
                "RULE_THRESHOLD_UNEXPECTED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行是存在性/条款判断，“允许差值”应留空",
                details=reader.location(row, "threshold").detail(),
            )
        if threshold_unit:
            raise BusinessError(
                "RULE_THRESHOLD_UNIT_UNEXPECTED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行是存在性/条款判断，“差值单位”应留空",
                details=reader.location(row, "threshold_unit").detail(),
            )
        return
    if operator in {"FIELD_EQUAL", "DECIMAL_CN_EQUAL"}:
        if threshold is not None and threshold != 0:
            raise BusinessError(
                "RULE_THRESHOLD_INVALID",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「允许差值」为 {threshold}："
                "文本一致与大小写金额比较不做数值差额，该列只能是 0 或留空",
                details=reader.location(row, "threshold").detail(),
            )
        if threshold_unit:
            raise BusinessError(
                "RULE_THRESHOLD_UNIT_UNEXPECTED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行是文本/大小写金额一致性比较，“差值单位”应留空",
                details=reader.location(row, "threshold_unit").detail(),
            )
        return
    # 日期与数值比较：允许差值是必填的非负数值，0 表示不允许偏差。
    if threshold is None:
        raise BusinessError(
            "RULE_THRESHOLD_REQUIRED",
            f"工作表「{reader.sheet_name}」第 {row['row_no']} 行是数值/日期比较，必须填写非负的「允许差值」"
            "（0 表示不允许偏差）",
            details=reader.location(row, "threshold").detail(),
        )
    if operator in DATE_COMPARISON_OPERATORS:
        unit = (threshold_unit or "").strip()
        if not unit:
            raise BusinessError(
                "RULE_THRESHOLD_UNIT_REQUIRED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的日期比较必须填写「差值单位」，日期差值以天为单位",
                details=reader.location(row, "threshold_unit").detail(),
            )
    if not check_level:
        raise BusinessError(
            "RULE_CHECK_LEVEL_REQUIRED",
            f"工作表「{reader.sheet_name}」第 {row['row_no']} 行缺少「检查等级」，请补齐后重新上传",
            details=reader.location(row, "check_level").detail(),
        )


def _resolve_check_level(
    *,
    reader: SheetReader,
    row: dict[str, Any],
    rule_name: str,
    rule_ref: str,
    check_level: str | None,
    notes: list[ConversionNote],
) -> str | None:
    """检查等级是业务配置事实，必须保留原文。

    等级顺序「最高>一级>二级>三级」由填写规范给出，但不与既有执行契约的
    「高/中/低/待确认」自动映射：PM 原「高/低」没有明确映射，代码不代填。
    已识别的四级写入执行参数；「高/低」与缺失保留为待确认，不参与发布。
    """
    if check_level in {"最高", "一级", "二级", "三级"}:
        return check_level
    value = check_level or None
    notes.append(
        ConversionNote(
            code="CHECK_LEVEL_PENDING_MAPPING",
            message=(
                f"检查「{rule_name}」的检查等级是“{value or '（空）'}”，"
                "现有执行契约的风险等级取值为「高/中/低/待确认」。PM 原「高/低」与"
                "「最高/一级/二级/三级」的对应关系尚未确认，系统不代填映射。"
            ),
            sheet_key=reader.sheet_key,
            sheet_name=reader.sheet_name,
            row_no=row["row_no"],
            cell=reader.location(row, "check_level").cell,
            column=reader.column_names["check_level"],
            value=value,
            question_title=f"检查「{rule_name}」的检查等级“{value or '（空）'}”缺少已确认的等级映射，请指定它对应哪一级",
            question_options=[
                {"label": "最高", "value": "最高"},
                {"label": "一级", "value": "一级"},
                {"label": "二级", "value": "二级"},
                {"label": "三级", "value": "三级"},
            ],
            resolve={"mode": "REPLACE", "target_type": "RULE", "target_ref": rule_ref, "property": "check_level"},
        )
    )
    return None


def _convert_internal_validate(
    reader: SheetReader,
    *,
    material_by_code: dict[str, dict[str, Any]],
    field_catalog: dict[tuple[str, str], dict[str, Any]],
    field_by_code: dict[str, dict[str, Any]],
) -> SheetConversion:
    return _convert_comparison_sheet(
        reader,
        role="INTERNAL_VALIDATE",
        material_by_code=material_by_code,
        field_catalog=field_catalog,
        field_by_code=field_by_code,
    )


def _convert_cross_validate(
    reader: SheetReader,
    *,
    material_by_code: dict[str, dict[str, Any]],
    field_catalog: dict[tuple[str, str], dict[str, Any]],
    field_by_code: dict[str, dict[str, Any]],
) -> SheetConversion:
    return _convert_comparison_sheet(
        reader,
        role="CROSS_VALIDATE",
        material_by_code=material_by_code,
        field_catalog=field_catalog,
        field_by_code=field_by_code,
    )


def _convert_comparison_sheet(
    reader: SheetReader,
    *,
    role: str,
    material_by_code: dict[str, dict[str, Any]],
    field_catalog: dict[tuple[str, str], dict[str, Any]],
    field_by_code: dict[str, dict[str, Any]],
) -> SheetConversion:
    proposals: dict[str, Any] = {"materials": [], "fields": [], "rules": [], "report_sections": [], "questions": []}
    outcomes: list[RowOutcome] = []
    notes: list[ConversionNote] = []
    seen_ids: dict[str, int] = {}
    for row in reader.business_rows():
        if _blank_row(row, reader):
            outcomes.append(RowOutcome(row["row_no"], "EXCLUDED", "该行在所有业务列上都没有内容"))
            continue
        if _retain_disabled_row(reader, row, outcomes):
            continue
        filled = _partial_row(row, reader)
        if len(filled) < 2:
            outcomes.append(
                RowOutcome(row["row_no"], "EXCLUDED", f"该行只填写了「{filled[0] if filled else '无'}」，未形成完整业务记录")
            )
            continue
        source_id = reader.required_code(row, "source_id")
        if source_id in seen_ids:
            raise BusinessError(
                "RULE_ID_DUPLICATED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的规则编号“{source_id}”与第 {seen_ids[source_id]} 行重复",
                details=reader.location(row, "source_id", value=source_id).detail(),
            )
        seen_ids[source_id] = row["row_no"]
        status = _row_config_status(reader, row)
        _assert_row_required_filled(reader, row)
        row_refs: list[str] = []
        name_location = reader.location(row, "name", value=reader.text(row, "name"))
        name = check_business_name(reader.required_text(row, "name"), location=name_location)
        operator = reader.enum(row, "operator")
        check_level = reader.enum(row, "check_level", required=False)
        direction = COMPARISON_DIRECTION.get(operator)
        threshold = parse_non_negative_decimal(
            reader.text(row, "threshold"), location=reader.location(row, "threshold", value=reader.text(row, "threshold"))
        )
        threshold_unit = reader.text(row, "threshold_unit")
        risk_names = split_list(
            reader.text(row, "risk_refs"),
            location=reader.location(row, "risk_refs", value=reader.text(row, "risk_refs")),
        )

        if role == "INTERNAL_VALIDATE":
            material_codes = split_list(
                reader.text(row, "material_refs"),
                location=reader.location(row, "material_refs", value=reader.text(row, "material_refs")),
            )
            if not material_codes:
                raise BusinessError(
                    "RULE_MATERIAL_SCOPE_EMPTY",
                    f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「适用材料」为空："
                    "空白不表示全部，请明确写出这条检查适用于哪些材料",
                    details=reader.location(row, "material_refs").detail(),
                )
            materials = []
            for material_code in material_codes:
                material = material_by_code.get(material_code)
                if material is None:
                    raise BusinessError(
                        "RULE_MATERIAL_REF_UNKNOWN",
                        f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「适用材料」引用“{material_code}”"
                        "不在 01_材料位 中，请核对材料名称或编号后重新上传",
                        details=reader.location(row, "material_refs", value=material_code).detail(),
                    )
                materials.append(material)
            # 内部校验的字段来自同一份材料：多个适用材料分别执行，每个材料的字段都不能混用。
            right_names = parse_field_expression(
                reader.text(row, "right_field_ref"),
                location=reader.location(row, "right_field_ref", value=reader.text(row, "right_field_ref")),
            )
            left_name = reader.required_text(row, "left_field_ref")
            left_expression = parse_field_expression(
                left_name, location=reader.location(row, "left_field_ref", value=left_name)
            )
            if len(left_expression) != 1:
                raise BusinessError(
                    "RULE_LEFT_FIELD_SINGLE_REQUIRED",
                    f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「检查字段」只能是一个字段，"
                    "加法只用于「对照内容」",
                    details=reader.location(row, "left_field_ref").detail(),
                )
            _check_operator_shape(
                reader=reader,
                row=row,
                operator=operator,
                check_level=check_level,
                right_names=right_names,
                original_right=reader.text(row, "right_field_ref"),
                threshold=threshold,
                threshold_unit=threshold_unit,
            )
            for material in materials:
                inputs = _resolve_field_names(
                    left_expression,
                    material=material,
                    catalog=field_catalog,
                    reader=reader,
                    row=row,
                    column_key="left_field_ref",
                    original=left_expression[0],
                    field_by_code=field_by_code,
                )
                inputs += _resolve_field_names(
                    right_names,
                    material=material,
                    catalog=field_catalog,
                    reader=reader,
                    row=row,
                    column_key="right_field_ref",
                    original=reader.text(row, "right_field_ref") or "",
                    field_by_code=field_by_code,
                )
                left_field = _field_for_reference(
                    left_expression[0], material=material, field_by_code=field_by_code, field_catalog=field_catalog
                )
                _assert_operator_field_types(
                    reader=reader,
                    row=row,
                    operator=operator,
                    left_field=left_field,
                    right_fields=[
                        _field_for_reference(name, material=material, field_by_code=field_by_code, field_catalog=field_catalog)
                        for name in right_names
                    ],
                )
                engine_operator = _engine_of(operator=operator, addend_count=len(right_names))
                ref = _candidate_ref("rule", reader.sheet_key, row["row_no"]) + f":{material['candidate_ref']}"
                proposals["rules"].append(
                    _build_rule(
                        ref=ref,
                        source_id=source_id,
                        name=name,
                        node="INTERNAL_VALIDATE",
                        engine_operator=engine_operator,
                        inputs=inputs,
                        materials=[material["candidate_ref"]],
                        parameters=_comparison_parameters(
                            operator=operator,
                            direction=direction,
                            threshold=threshold,
                            threshold_unit=threshold_unit,
                        ),
                        requirement=reader.text(row, "remark") or "",
                        operator_cell=operator,
                        left_field=left_field,
                        check_level=check_level,
                        check_level_value=check_level,
                        risk_names=risk_names,
                        status=status,
                        reader=reader,
                        row=row,
                    )
                )
                row_refs.append(ref)
            outcomes.append(RowOutcome(row["row_no"], "CANDIDATE", None, row_refs))
            _resolve_check_level(
                reader=reader,
                row=row,
                rule_name=name,
                rule_ref=row_refs[0],
                check_level=check_level,
                notes=notes,
            )

            continue

        # 05_文件交叉校验规则：左侧属于「材料」，右侧全部加数属于同一份「对照材料」。
        left_material_code = reader.required_text(row, "material_ref")
        left_material = material_by_code.get(left_material_code)
        if left_material is None:
            raise BusinessError(
                "RULE_MATERIAL_REF_UNKNOWN",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「材料」“{left_material_code}”"
                "不在 01_材料位 中，请核对材料名称或编号后重新上传",
                details=reader.location(row, "material_ref", value=left_material_code).detail(),
            )
        right_material_code = reader.required_text(row, "right_material_ref")
        right_material = material_by_code.get(right_material_code)
        if right_material is None:
            raise BusinessError(
                "RULE_MATERIAL_REF_UNKNOWN",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「对照材料」“{right_material_code}”"
                "不在 01_材料位 中，请核对材料名称或编号后重新上传",
                details=reader.location(row, "right_material_ref", value=right_material_code).detail(),
            )
        right_codes = parse_field_expression(
            reader.text(row, "right_field_ref"),
            location=reader.location(row, "right_field_ref", value=reader.text(row, "right_field_ref")),
        )
        left_code = reader.required_text(row, "left_field_ref")
        _check_operator_shape(
            reader=reader,
            row=row,
            operator=operator,
            check_level=check_level,
            right_names=right_codes,
            original_right=reader.text(row, "right_field_ref"),
            threshold=threshold,
            threshold_unit=threshold_unit,
        )
        inputs = _resolve_field_codes(
            [left_code],
            material=left_material,
            field_by_code=field_by_code,
            reader=reader,
            row=row,
            column_key="left_field_ref",
            field_catalog=field_catalog,
        )
        inputs += _resolve_field_codes(
            right_codes,
            material=right_material,
            field_by_code=field_by_code,
            reader=reader,
            row=row,
            column_key="right_field_ref",
            field_catalog=field_catalog,
        )
        left_field = _field_for_reference(
            left_code, material=left_material, field_by_code=field_by_code, field_catalog=field_catalog
        )
        _assert_operator_field_types(
            reader=reader,
            row=row,
            operator=operator,
            left_field=left_field,
            right_fields=[
                _field_for_reference(code, material=right_material, field_by_code=field_by_code, field_catalog=field_catalog)
                for code in right_codes
            ],
        )
        ref = _candidate_ref("rule", reader.sheet_key, row["row_no"])
        proposals["rules"].append(
            _build_rule(
                ref=ref,
                source_id=source_id,
                name=name,
                node="CROSS_VALIDATE",
                engine_operator=_engine_of(operator=operator, addend_count=len(right_codes)),
                inputs=inputs,
                materials=[left_material["candidate_ref"], right_material["candidate_ref"]],
                parameters=_comparison_parameters(
                    operator=operator,
                    direction=direction,
                    threshold=threshold,
                    threshold_unit=threshold_unit,
                ),
                requirement=reader.text(row, "remark") or "",
                operator_cell=operator,
                # 结果归属按实际参与比较的字段所属材料取得：待核对主体取自左材料、
                # 印章主体候选取自右材料，两者都是这条规则的真实依据。
                left_field=left_field,
                right_fields=[
                    _field_for_reference(code, material=right_material, field_by_code=field_by_code, field_catalog=field_catalog)
                    for code in right_codes
                ],
                check_level=check_level,
                check_level_value=check_level,
                risk_names=risk_names,
                status=status,
                reader=reader,
                row=row,
            )
        )
        outcomes.append(RowOutcome(row["row_no"], "CANDIDATE", None, [ref]))
        _resolve_check_level(
            reader=reader,
            row=row,
            rule_name=name,
            rule_ref=ref,
            check_level=check_level,
            notes=notes,
        )

    return SheetConversion(reader, proposals, outcomes, notes)


def _assert_operator_field_types(
    *,
    reader: SheetReader,
    row: dict[str, Any],
    operator: str,
    left_field: dict[str, Any],
    right_fields: list[dict[str, Any]],
) -> None:
    """检查方式与字段类型必须匹配，不匹配属于配置错误，不能交给模型改写。

    对照内容允许多个加数时，每个加数都要与左侧组成合法组合（加总要求同型）。
    """
    rule = OPERATOR_ALLOWED_TYPES[operator]
    singles = {item for item in rule if isinstance(item, str)}
    pairs = {item for item in rule if isinstance(item, tuple)}
    right_types = [field["value_type"] for field in right_fields]
    if singles:
        if left_field["value_type"] not in singles:
            raise BusinessError(
                "RULE_OPERATOR_TYPE_MISMATCH",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「合格要求」是"
                f"“{reader.column_names['operator']}”，但检查字段“{left_field['name']}”的数据类型是"
                f"“{left_field['value_type']}”：检查方式与字段类型不匹配，这属于配置错误",
                details=reader.location(row, "left_field_ref").detail(
                    operator=operator, value_type=left_field["value_type"], allowed=sorted(singles)
                ),
            )
        for field in right_fields:
            if field["value_type"] not in singles:
                raise BusinessError(
                    "RULE_OPERATOR_TYPE_MISMATCH",
                    f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「对照内容」引用了"
                    f"“{field['name']}”（数据类型 {field['value_type']}），与检查方式不匹配",
                    details=reader.location(row, "right_field_ref").detail(
                        operator=operator, value_type=field["value_type"], allowed=sorted(singles)
                    ),
                )
        return
    # 显式左右侧组合：必须恰好一个对照字段，且左右类型组成一个已允许的组合。
    allowed_pairs = sorted(f"{left}→{right}" for left, right in pairs)
    if len(right_fields) != 1 or (left_field["value_type"], right_types[0]) not in pairs:
        actual = "（无对照内容）" if not right_types else "、".join(right_types)
        raise BusinessError(
            "RULE_OPERATOR_TYPE_MISMATCH",
            f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「合格要求」是"
            f"“{reader.column_names['operator']}”，要求检查字段与对照内容的类型组合是"
            f"{' 或 '.join(allowed_pairs)}，但实际是"
            f"“{left_field['value_type']}→{actual}”：检查方式与字段类型不匹配，这属于配置错误",
            details=reader.location(row, "right_field_ref").detail(
                operator=operator,
                left_value_type=left_field["value_type"],
                right_value_types=right_types,
                allowed=allowed_pairs,
            ),
        )


def _build_rule(
    *,
    ref: str,
    source_id: str,
    name: str,
    node: str,
    engine_operator: str,
    inputs: list[dict[str, Any]],
    materials: list[str],
    parameters: dict[str, Any],
    requirement: str,
    operator_cell: str,
    left_field: dict[str, Any],
    check_level: str | None,
    check_level_value: str | None,
    risk_names: list[str],
    status: str,
    reader: SheetReader,
    row: dict[str, Any],
    right_fields: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "candidate_ref": ref,
        "source_id": source_id,
        "name": name,
        "node": node,
        "operator": engine_operator,
        "inputs": inputs,
        "output_field_refs": [],
        # 规则的材料范围必须覆盖两侧真实字段：只写声明列会让风险汇总、报告归属与
        # 主体检索范围丢掉另一侧的材料。
        "material_refs": _rule_material_refs(materials, left_field, right_fields),
        "parameters": parameters,
        "requirement": requirement,
        "applicability": None,
        "check_operator": operator_cell,
        "check_level": check_level_value,
        "risk_names": list(risk_names),
        "source_rule_refs": [],
        "risk_condition": None,
        "risk_level_value": None,
        "quality_supported": None,
        "config_status": status,
        "source": _source_ref(reader, row),
    }


def _rule_material_refs(
    declared: list[str],
    left_field: dict[str, Any] | None,
    right_fields: list[dict[str, Any]] | None,
) -> list[str]:
    """规则材料范围 = 声明列 + 实际参与比较的字段所属材料（去重，保持稳定顺序）。

    节点版的 05_文件交叉校验规则把两侧材料分列维护，单侧引用声明列就够；
    内部校验按「适用材料」逐份展开。这里统一补齐两侧真实字段的材料，
    使风险汇总、报告材料归属与主体检索范围都拿到完整依据。
    """
    ordered: list[str] = []
    for ref in [*declared, *([left_field] if left_field else []), *(right_fields or [])]:
        if not isinstance(ref, dict):
            material_ref = ref
        else:
            material_ref = ref.get("material_ref") or ref.get("candidate_ref")
        if material_ref and material_ref not in ordered:
            ordered.append(material_ref)
    return ordered


# 02_文件处理规则


def _convert_material_quality(
    reader: SheetReader,
    *,
    material_by_code: dict[str, dict[str, Any]],
) -> SheetConversion:
    proposals: dict[str, Any] = {"materials": [], "fields": [], "rules": [], "report_sections": [], "questions": []}
    outcomes: list[RowOutcome] = []
    notes: list[ConversionNote] = []
    seen_ids: dict[str, int] = {}
    for row in reader.business_rows():
        if _blank_row(row, reader):
            outcomes.append(RowOutcome(row["row_no"], "EXCLUDED", "该行在所有业务列上都没有内容"))
            continue
        if _retain_disabled_row(reader, row, outcomes):
            continue
        filled = _partial_row(row, reader)
        if len(filled) < 2:
            outcomes.append(
                RowOutcome(row["row_no"], "EXCLUDED", f"该行只填写了「{filled[0] if filled else '无'}」，未形成完整业务记录")
            )
            continue
        source_id = reader.required_code(row, "source_id")
        if source_id in seen_ids:
            raise BusinessError(
                "RULE_ID_DUPLICATED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的规则编号“{source_id}”与第 {seen_ids[source_id]} 行重复",
                details=reader.location(row, "source_id", value=source_id).detail(),
            )
        seen_ids[source_id] = row["row_no"]
        status = _row_config_status(reader, row)
        _assert_row_required_filled(reader, row)
        name_location = reader.location(row, "name", value=reader.text(row, "name"))
        name = check_business_name(reader.required_text(row, "name"), location=name_location)
        check_item = reader.enum(row, "check_item")
        requirement = reader.enum(row, "requirement")
        allowed = QUALITY_ITEM_REQUIREMENTS.get(check_item, frozenset())
        if requirement not in allowed:
            raise BusinessError(
                "RULE_QUALITY_REQUIREMENT_MISMATCH",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「检查项目」是"
                f"“{QUALITY_ITEM_LABELS.get(check_item, check_item)}”，但「合格要求」填的是"
                f"“{REQUIREMENT_LABELS.get(requirement, requirement)}”：项目与判断方式不匹配，"
                "这属于配置错误，系统不会自行改写",
                details=reader.location(row, "requirement", value=requirement).detail(
                    check_item=check_item, allowed=sorted(allowed)
                ),
            )
        material_codes = split_list(
            reader.text(row, "material_refs"),
            location=reader.location(row, "material_refs", value=reader.text(row, "material_refs")),
        )
        if not material_codes:
            raise BusinessError(
                "RULE_MATERIAL_SCOPE_EMPTY",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「适用材料」为空："
                "空白不表示全部，请明确写出这条检查适用于哪些材料",
                details=reader.location(row, "material_refs").detail(),
            )
        materials: list[dict[str, Any]] = []
        for material_code in material_codes:
            material = material_by_code.get(material_code)
            if material is None:
                raise BusinessError(
                    "RULE_MATERIAL_REF_UNKNOWN",
                    f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「适用材料」引用“{material_code}”"
                    "不在 01_材料位 中，请核对材料名称或编号后重新上传",
                    details=reader.location(row, "material_refs", value=material_code).detail(),
                )
            materials.append(material)
        standard_value = reader.text(row, "standard_value")
        unit = reader.text(row, "unit")
        criterion = reader.text(row, "criterion") or ""
        risk_names = split_list(
            reader.text(row, "risk_refs"),
            location=reader.location(row, "risk_refs", value=reader.text(row, "risk_refs")),
        )
        check_level = reader.enum(row, "check_level", required=False)
        if requirement == "IN" and not standard_value:
            raise BusinessError(
                "RULE_FORMAT_LIST_REQUIRED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的检查项目是“文件格式”且合格要求是“属于”，"
                "但「标准值」为空：请填写允许的格式集合（例如 PDF；JPG；PNG）",
                details=reader.location(row, "standard_value").detail(),
            )
        if requirement == "LTE":
            # 文件大小检查的标准值与单位必须**在导入期**联合校验：两者缺一或单位不可识别时，
            # 运行时才算不出字节上限。留到执行端才发现会让一份看起来正常的规则库
            # 在真实审核里整条不可判定，问题暴露得太晚。
            if check_item == "FILE_SIZE":
                # 直接复用执行端的换算口径，避免导入期与运行期各写一套单位表而漂移。
                # 换算失败时把执行端的错误码改成导入期的配置错误：这是业务填错了标准值或单位，
                # 必须带 Sheet、行、列定位，而不是一个「执行时失败」的 500。
                try:
                    file_quality.parse_size_limit(standard_value, unit)
                except BusinessError as exc:
                    raise BusinessError(
                        exc.code,
                        f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的文件大小检查配置无效：{exc.message}",
                        details=reader.location(
                            row,
                            "unit" if "UNIT" in exc.code else "standard_value",
                        ).detail(),
                    ) from exc
            elif standard_value:
                parse_non_negative_decimal(
                    standard_value,
                    location=reader.location(row, "standard_value", value=standard_value),
                )
                if not unit:
                    raise BusinessError(
                        "RULE_QUALITY_UNIT_REQUIRED",
                        f"工作表「{reader.sheet_name}」第 {row['row_no']} 行是数值比较，必须填写「单位」"
                        "（MB 指 1,000,000 字节，MiB 指 1,048,576 字节，不能混用）",
                        details=reader.location(row, "unit").detail(),
                    )
        parameters: dict[str, Any] = {
            "check_item": check_item,
            "requirement": requirement,
            "artifact_type": QUALITY_ITEM_ARTIFACT.get(check_item),
            "criterion": criterion,
        }
        if standard_value:
            parameters["standard_value"] = standard_value
        if unit:
            parameters["unit"] = unit
        validate_rule_parameters(name, "FILE_QUALITY_CHECK", parameters)
        # 未知的检查项目不是「证据不足」，是系统不认识的算子：必须显式报错，
        # 否则会被混进「判定口径未确认」的能力缺口里，看起来像已知但未确认的能力。
        if check_item not in file_quality.KNOWN_QUALITY_ITEMS:
            raise BusinessError(
                "RULE_QUALITY_ITEM_UNKNOWN",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「检查项目」是"
                f"“{check_item}”，不是系统已知的检查项目，无法执行也无法形成能力缺口",
                details=reader.location(row, "check_item", value=check_item).detail(
                    known=sorted(file_quality.KNOWN_QUALITY_ITEMS)
                ),
            )
        supported = file_quality.is_implemented(check_item)
        ref = _candidate_ref("rule", reader.sheet_key, row["row_no"])
        proposals["rules"].append(
            {
                "candidate_ref": ref,
                "source_id": source_id,
                "name": name,
                "node": "DOCUMENT_PREPARE",
                "operator": "FILE_QUALITY_CHECK",
                "inputs": [],
                "output_field_refs": [],
                "material_refs": [item["candidate_ref"] for item in materials],
                "parameters": parameters,
                "requirement": criterion or reader.text(row, "remark") or "",
                "applicability": None,
                "check_operator": requirement,
                "check_level": check_level,
                "risk_names": list(risk_names),
                "source_rule_refs": [],
                "risk_condition": None,
                "risk_level_value": None,
                "config_status": status,
                "quality_supported": supported,
                "source": _source_ref(reader, row),
            }
        )
        outcomes.append(RowOutcome(row["row_no"], "CANDIDATE", None, [ref]))
        if not supported:
            notes.append(
                ConversionNote(
                    code="QUALITY_CAPABILITY_GAP",
                    message=(
                        f"检查「{name}」的检查项目是“{file_quality.label_of(check_item)}”，"
                        "当前缺少供应商质量结论字段或尚未确认的业务口径（判定算法、多页汇总方式等）："
                        "运行时该项会明确报“不可判定”，不会默认通过。"
                    ),
                    sheet_key=reader.sheet_key,
                    sheet_name=reader.sheet_name,
                    row_no=row["row_no"],
                    cell=reader.location(row, "criterion").cell,
                    column=reader.column_names["criterion"],
                    value=criterion or None,
                    question_title=f"检查「{name}」的判定口径尚未确认，请处理后重新上传",
                    question_options=[],
                )
            )
        if requirement != "CONTINUOUS_NO_MISSING" and check_item == "PAGE_INTEGRITY":
            raise BusinessError(
                "RULE_QUALITY_REQUIREMENT_MISMATCH",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「检查项目」是“页码”，"
                "合格要求只能是“连续且无缺页”",
                details=reader.location(row, "requirement").detail(),
            )
        _resolve_check_level(
            reader=reader,
            row=row,
            rule_name=name,
            rule_ref=ref,
            check_level=check_level,
            notes=notes,
        )

    return SheetConversion(reader, proposals, outcomes, notes)


# 09_风险规则


def _convert_risks(reader: SheetReader) -> SheetConversion:
    proposals: dict[str, Any] = {"materials": [], "fields": [], "rules": [], "report_sections": [], "questions": []}
    outcomes: list[RowOutcome] = []
    notes: list[ConversionNote] = []
    seen_names: dict[str, int] = {}
    seen_ids: dict[str, int] = {}
    for row in reader.business_rows():
        if _blank_row(row, reader):
            outcomes.append(RowOutcome(row["row_no"], "EXCLUDED", "该行在所有业务列上都没有内容"))
            continue
        if _retain_disabled_row(reader, row, outcomes):
            continue
        filled = _partial_row(row, reader)
        if len(filled) < 2:
            outcomes.append(
                RowOutcome(row["row_no"], "EXCLUDED", f"该行只填写了「{filled[0] if filled else '无'}」，未形成完整业务记录")
            )
            continue
        source_id = reader.required_code(row, "source_id")
        if source_id in seen_ids:
            raise BusinessError(
                "RISK_ID_DUPLICATED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的风险编号“{source_id}”与第 {seen_ids[source_id]} 行重复",
                details=reader.location(row, "source_id", value=source_id).detail(),
            )
        seen_ids[source_id] = row["row_no"]
        status = _row_config_status(reader, row, required=True)
        name_location = reader.location(row, "name", value=reader.text(row, "name"))
        name = check_business_name(reader.required_text(row, "name"), location=name_location)
        if name in seen_names:
            raise BusinessError(
                "RISK_NAME_DUPLICATED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的风险名称“{name}”与第 {seen_names[name]} 行重复，"
                "风险名称必须唯一",
                details=name_location.detail(),
            )
        seen_names[name] = row["row_no"]
        risk_level_value = reader.enum(row, "risk_level")
        condition = reader.enum(row, "condition", required=False)
        description = reader.text(row, "description") or ""
        action = reader.text(row, "action") or ""
        ref = _candidate_ref("risk", reader.sheet_key, row["row_no"])
        if risk_level_value in {"高", "低"}:
            # 「高/低」与「最高/一级/二级/三级」的业务对应关系尚未确认，系统不代填：
            # 这是一条可作答的待确认事项，不是导入失败。
            notes.append(
                ConversionNote(
                    code="RISK_LEVEL_PENDING_MAPPING",
                    message=(
                        f"风险「{name}」的风险等级是“{risk_level_value}”。PM 原「高/低」与"
                        "「最高/一级/二级/三级」的对应关系尚未确认，系统不代填映射。"
                    ),
                    sheet_key=reader.sheet_key,
                    sheet_name=reader.sheet_name,
                    row_no=row["row_no"],
                    cell=reader.location(row, "risk_level").cell,
                    column=reader.column_names["risk_level"],
                    value=risk_level_value,
                    question_title=f"风险「{name}」的风险等级“{risk_level_value}”缺少已确认的等级映射，请指定它对应哪一级",
                    question_options=[
                        {"label": "最高", "value": "最高"},
                        {"label": "一级", "value": "一级"},
                        {"label": "二级", "value": "二级"},
                        {"label": "三级", "value": "三级"},
                    ],
                    resolve={
                        "mode": "SET_PARAMETER",
                        "target_type": "RULE",
                        "target_ref": ref,
                        "property": "parameters",
                        "parameter_key": "risk_level",
                        "base_parameters": {"action": action},
                    },
                )
            )
        if condition is None:
            # 成立条件缺失是业务口径缺失（PM 未说明多个来源是任一还是全部），
            # 属于可处理的待确认事项：记录问题并留在草稿，但不发布为可执行版本。
            # 技术性错误（文件损坏、表头不符、编号重复）仍在上面直接报错终止。
            notes.append(
                ConversionNote(
                    code="RISK_CONDITION_PENDING",
                    message=(
                        f"风险「{name}」没有填写「成立条件」：有多个来源时必须明确"
                        "“任一关联检查不合格”还是“全部关联检查不合格”，系统不替业务选择。"
                    ),
                    sheet_key=reader.sheet_key,
                    sheet_name=reader.sheet_name,
                    row_no=row["row_no"],
                    cell=reader.location(row, "condition").cell,
                    column=reader.column_names["condition"],
                    value=None,
                    question_title=f"风险「{name}」的成立条件尚未确认，请指定任一或全部关联检查不合格时成立",
                    question_options=[
                        {"label": "任一关联检查不合格", "value": RISK_CONDITION_ANY},
                        {"label": "全部关联检查不合格", "value": RISK_CONDITION_ALL},
                    ],
                    resolve={
                        "mode": "REPLACE",
                        "target_type": "RULE",
                        "target_ref": ref,
                        "property": "risk_condition",
                    },
                )
            )
        proposals["rules"].append(
            {
                "candidate_ref": ref,
                "source_id": source_id,
                "name": name,
                "node": "RISK_RATING",
                "operator": "RISK_TRIGGER",
                "inputs": [],
                "output_field_refs": [],
                "material_refs": [],
                "parameters": {"risk_level": risk_level_value, "action": action},
                "requirement": description,
                "applicability": None,
                "check_operator": None,
                "check_level": None,
                "risk_condition": condition,
                "risk_level_value": risk_level_value,
                "source_rule_refs": [],
                "quality_supported": None,
                "config_status": status,
                "source": _source_ref(reader, row),
            }
        )
        outcomes.append(RowOutcome(row["row_no"], "CANDIDATE", None, [ref]))

    return SheetConversion(reader, proposals, outcomes, notes)


# 10_报告模板


def _convert_reports(
    reader: SheetReader,
    *,
    material_by_code: dict[str, dict[str, Any]],
    field_by_code: dict[str, dict[str, Any]],
    field_catalog: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> SheetConversion:
    proposals: dict[str, Any] = {"materials": [], "fields": [], "rules": [], "report_sections": [], "questions": []}
    outcomes: list[RowOutcome] = []
    notes: list[ConversionNote] = []
    for row in reader.business_rows():
        if _blank_row(row, reader):
            outcomes.append(RowOutcome(row["row_no"], "EXCLUDED", "该行在所有业务列上都没有内容"))
            continue
        if _retain_disabled_row(reader, row, outcomes):
            continue
        filled = _partial_row(row, reader)
        if len(filled) < 2:
            outcomes.append(
                RowOutcome(row["row_no"], "EXCLUDED", f"该行只填写了「{filled[0] if filled else '无'}」，未形成完整业务记录")
            )
            continue
        report_code = reader.required_code(row, "report_code")
        status = _row_config_status(reader, row)
        _assert_row_required_filled(reader, row)
        report_name_location = reader.location(row, "report_name", value=reader.text(row, "report_name"))
        report_name = check_business_name(reader.required_text(row, "report_name"), location=report_name_location)
        material_codes = split_list(
            reader.text(row, "material_refs"),
            location=reader.location(row, "material_refs", value=reader.text(row, "material_refs")),
        )
        if not material_codes:
            raise BusinessError(
                "REPORT_SCOPE_EMPTY",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「材料范围」为空："
                "空白不表示全部，请明确写出这份报告覆盖哪些材料",
                details=reader.location(row, "material_refs").detail(),
            )
        materials: list[dict[str, Any]] = []
        for material_code in material_codes:
            material = material_by_code.get(material_code)
            if material is None:
                raise BusinessError(
                    "REPORT_MATERIAL_REF_UNKNOWN",
                    f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「材料范围」引用“{material_code}”"
                    "不在 01_材料位 中，请核对材料名称或编号后重新上传",
                    details=reader.location(row, "material_refs", value=material_code).detail(),
                )
            materials.append(material)
        formats = split_list(
            reader.text(row, "formats"),
            location=reader.location(row, "formats", value=reader.text(row, "formats")),
        )
        if not formats:
            raise BusinessError(
                "REPORT_FORMAT_EMPTY",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「输出格式」为空，请填写 PDF、Word、Excel 的分号组合",
                details=reader.location(row, "formats").detail(),
            )
        normalized_formats: list[str] = []
        for raw_format in formats:
            canonical = REPORT_FORMAT_CODES.get(raw_format.upper())
            if canonical is None:
                raise BusinessError(
                    "REPORT_FORMAT_UNSUPPORTED",
                    f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「输出格式」填写的“{raw_format}”不受支持，"
                    "只支持 PDF、Word、Excel",
                    details=reader.location(row, "formats", value=raw_format).detail(),
                )
            if canonical in normalized_formats:
                raise BusinessError(
                    "REPORT_FORMAT_DUPLICATED",
                    f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的「输出格式」重复填写了“{canonical}”",
                    details=reader.location(row, "formats", value=raw_format).detail(),
                )
            normalized_formats.append(canonical)
        section_order = parse_positive_int(
            reader.text(row, "section_order"),
            location=reader.location(row, "section_order", value=reader.text(row, "section_order")),
        )
        if section_order is None:
            raise BusinessError(
                "REPORT_SECTION_ORDER_REQUIRED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行缺少「章节顺序」，请填写同报告内唯一的正整数",
                details=reader.location(row, "section_order").detail(),
            )
        section_name = reader.required_text(row, "section_name")
        content_type = reader.enum(row, "content_type")
        raw_field_refs = split_list(
            reader.text(row, "field_refs"),
            location=reader.location(row, "field_refs", value=reader.text(row, "field_refs")),
        )
        field_keys: list[str] = []
        scope_refs = {item["candidate_ref"] for item in materials}
        for field_code in raw_field_refs:
            field = field_by_code.get(field_code)
            named_field = None
            if field is None and field_catalog is not None and "." in field_code:
                material_name, field_name = field_code.split(".", 1)
                material = material_by_code.get(material_name)
                if material is not None:
                    field = field_catalog.get((material["source_id"], field_name))
            if field_catalog is not None and "." not in field_code:
                scoped = [
                    field_catalog.get((material["source_id"], field_code))
                    for material in materials
                ]
                matches = {item["candidate_ref"]: item for item in scoped if item is not None}
                if len(matches) > 1:
                    raise BusinessError(
                        "REPORT_FIELD_AMBIGUOUS",
                        f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的展示字段“{field_code}”"
                        "在多个材料下都有定义，请写成“材料名称.字段名称”或使用字段编号",
                        details=reader.location(row, "field_refs", value=field_code).detail(),
                    )
                named_field = next(iter(matches.values()), None)
            if field is not None and named_field is not None and field["candidate_ref"] != named_field["candidate_ref"]:
                raise BusinessError(
                    "REPORT_FIELD_AMBIGUOUS",
                    f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的展示字段“{field_code}”同时是字段编号和其他字段名称，无法确定目标",
                    details=reader.location(row, "field_refs", value=field_code).detail(),
                )
            if field is None:
                field = named_field
            if field is None:
                raise BusinessError(
                    "REPORT_FIELD_UNKNOWN",
                    f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的展示字段“{field_code}”在 03_字段 中不存在",
                    details=reader.location(row, "field_refs", value=field_code).detail(),
                )
            if field["material_ref"] not in scope_refs:
                raise BusinessError(
                    "REPORT_FIELD_OUT_OF_SCOPE",
                    f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的展示字段“{field_code}”"
                    "不在本报告的材料范围内，请补入所属材料或改掉该字段",
                    details=reader.location(row, "field_refs", value=field_code).detail(),
                )
            field_keys.append(field["candidate_ref"])
        if content_type in FIELD_LIST_CONTENT_TYPES and not field_keys:
            raise BusinessError(
                "REPORT_SECTION_FIELD_REQUIRED",
                f"工作表「{reader.sheet_name}」第 {row['row_no']} 行的内容类型是"
                f"“{CONTENT_TYPE_LABELS.get(content_type, content_type)}”，必须指定至少一个展示字段",
                details=reader.location(row, "field_refs").detail(),
            )
        content_instruction = reader.text(row, "content_instruction") or ""
        result_sort = reader.enum(row, "result_sort", required=False)
        ref = f"sec:{reader.sheet_key}:{report_code}:{section_order}"
        proposals["report_sections"].append(
            {
                "candidate_ref": ref,
                "report_code": report_code,
                "report_name": report_name,
                "material_refs": [item["candidate_ref"] for item in materials],
                "formats": normalized_formats,
                "section_order": section_order,
                "section_name": section_name,
                "content_type": content_type,
                "field_refs": field_keys,
                "content_instruction": content_instruction,
                "result_sort": result_sort,
                "config_status": status,
                "source": _source_ref(reader, row),
            }
        )
        outcomes.append(RowOutcome(row["row_no"], "CANDIDATE", None, [ref]))


    return SheetConversion(reader, proposals, outcomes, notes)


def _assert_report_group_consistency(sections: list[dict[str, Any]], reader: SheetReader) -> None:
    """同一报告编号的报告名称、材料范围、输出格式、结果排序必须完全一致。

    重复属性是连续平表的明确代价：这里逐项比对并指出冲突来源，不使用空白继承，
    也不取第一行覆盖冲突。
    """
    groups: dict[str, dict[str, Any]] = {}
    for section in sections:
        code = section["report_code"]
        signature = {
            "report_name": section["report_name"],
            "material_refs": list(section["material_refs"]),
            "formats": list(section["formats"]),
            "result_sort": section["result_sort"],
        }
        existing = groups.get(code)
        source = section["source"]
        if existing is None:
            groups[code] = {"signature": signature, "section_name": section["section_name"], "source": source}
            continue
        if existing["signature"] != signature:
            differing = [key for key in signature if existing["signature"][key] != signature[key]]
            raise BusinessError(
                "REPORT_GROUP_ATTRIBUTE_CONFLICT",
                f"报告编号 {code} 的"
                + "、".join(differing)
                + f"在多行之间不一致（第 {existing['source']['row_no']} 行与第 {source['row_no']} 行）："
                "同一报告的这些属性必须完全一致，请整组修改后重新上传",
                details={
                    "sheet_key": reader.sheet_key,
                    "sheet_name": reader.sheet_name,
                    "row_no": source["row_no"],
                    "report_code": code,
                    "conflicts": differing,
                },
            )
    # 章节顺序在同报告内必须唯一。
    orders: dict[tuple[str, int], int] = {}
    for section in sections:
        key = (section["report_code"], section["section_order"])
        if key in orders:
            raise BusinessError(
                "REPORT_SECTION_ORDER_DUPLICATED",
                f"报告编号 {section['report_code']} 的章节顺序 {section['section_order']} 在第 "
                f"{orders[key]} 行与第 {section['source']['row_no']} 行重复，请改为同报告内唯一的正整数",
                details={
                    "sheet_key": reader.sheet_key,
                    "sheet_name": reader.sheet_name,
                    "row_no": section["source"]["row_no"],
                    "report_code": section["report_code"],
                },
            )
        orders[key] = section["source"]["row_no"]
    # 不同报告编号不能使用同一个名称。
    name_owner: dict[str, str] = {}
    for code, group in groups.items():
        name = group["signature"]["report_name"]
        if name in name_owner and name_owner[name] != code:
            raise BusinessError(
                "REPORT_NAME_DUPLICATED",
                f"报告名称“{name}”同时出现在报告编号 {name_owner[name]} 与 {code} 上，不同报告编号不能同名",
                details={
                    "sheet_key": reader.sheet_key,
                    "sheet_name": reader.sheet_name,
                    "row_no": group["source"]["row_no"],
                    "report_code": code,
                },
            )
        name_owner[name] = code


def _resolve_risk_references(
    comparison_rules: list[dict[str, Any]],
    risk_rules: list[dict[str, Any]],
) -> None:
    """把检查行的「关联风险」按风险名称或编号解析到风险候选引用。

    没有任何关联检查是配置错误；停用来源关系由调用方先核对。
    """
    risk_by_code = _reference_index(risk_rules, kind="风险")
    sources_by_risk: dict[str, list[dict[str, Any]]] = {rule["candidate_ref"]: [] for rule in risk_rules}
    for rule in comparison_rules:
        source = rule["source"]
        for risk_code in rule["risk_names"]:
            risk = risk_by_code.get(risk_code)
            if risk is None:
                raise BusinessError(
                    "RISK_REF_UNKNOWN",
                    f"检查「{rule['name']}」的「关联风险」“{risk_code}”不在 09_风险规则 中，"
                    "请核对风险名称或编号后重新上传",
                    details={
                        "sheet_key": source["sheet_key"],
                        "row_no": source["row_no"],
                        "rule_name": rule["name"],
                        "value": risk_code,
                    },
                )
            sources_by_risk[risk["candidate_ref"]].append(rule)
    for risk in risk_rules:
        sources = sources_by_risk[risk["candidate_ref"]]
        if not sources:
            source = risk["source"]
            raise BusinessError(
                "RISK_SOURCE_MISSING",
                f"风险「{risk['name']}」没有任何关联检查：请在 02/04/05 的检查行写明它关联哪个风险，"
                "风险表不重复维护来源。没有来源是配置错误，不能按空集合推导风险成立或不成立",
                details={
                    "sheet_key": source["sheet_key"],
                    "row_no": source["row_no"],
                    "risk_name": risk["name"],
                },
            )
        risk["source_rule_refs"] = [rule["candidate_ref"] for rule in sources]


def _assert_no_disabled_risk_links(
    check_conversions: tuple[SheetConversion | None, ...],
    risk_conversion: SheetConversion | None,
) -> None:
    if risk_conversion is None:
        return
    active_risks = _reference_index(risk_conversion.proposals["rules"], kind="风险")
    disabled_risks: dict[str, dict[str, Any]] = {}
    for row in _disabled_rows_of_conversion(risk_conversion):
        for value in (
            row.get("source_id"),
            next((column["value"] for column in row["columns"] if column["key"] == "name"), None),
        ):
            if value:
                disabled_risks[str(value).strip()] = row
    for conversion in check_conversions:
        if conversion is None:
            continue
        for check in conversion.proposals["rules"]:
            for risk_code in check["risk_names"]:
                disabled = disabled_risks.get(risk_code)
                if disabled is None:
                    continue
                source = check["source"]
                raise BusinessError(
                    "DISABLED_DEPENDENCY",
                    f"检查“{check['name']}”关联的风险“{risk_code}”已停用；请明确处理这条跨表关系",
                    details={
                        "sheet_key": source["sheet_key"], "row_no": source["row_no"],
                        "risk_sheet_key": disabled["source"]["sheet_key"],
                        "risk_row_no": disabled["source"]["row_no"],
                        "risk_code": risk_code,
                    },
                )
        for disabled in _disabled_rows_of_conversion(conversion):
            raw_refs = next((column["value"] for column in disabled["columns"] if column["key"] == "risk_refs"), "")
            for risk_code in (part.strip() for part in raw_refs.split(LIST_SEPARATOR) if part.strip()):
                risk = active_risks.get(risk_code)
                if risk is None:
                    continue
                source = disabled["source"]
                raise BusinessError(
                    "DISABLED_DEPENDENCY",
                    f"启用风险“{risk['name']}”仍关联已停用的检查行；请明确处理这条跨表关系",
                    details={
                        "sheet_key": source["sheet_key"], "row_no": source["row_no"],
                        "check_code": disabled["source_id"],
                        "risk_sheet_key": risk["source"]["sheet_key"],
                        "risk_row_no": risk["source"]["row_no"],
                        "risk_code": risk_code,
                    },
                )


def _assert_level_conflicts(
    risk_rules: list[dict[str, Any]],
    rule_by_ref: dict[str, dict[str, Any]],
    reader: SheetReader,
) -> list[ConversionNote]:
    """检查等级与风险等级独立保留，不相互覆盖。

    等级冲突必须显式列出给业务确认，不能用较低风险等级覆盖来源异常等级。
    """
    notes: list[ConversionNote] = []
    for risk in risk_rules:
        risk_level = risk.get("risk_level_value")
        for source_ref in risk.get("source_rule_refs", []):
            rule = rule_by_ref.get(source_ref)
            if rule is None:
                continue
            check_level = rule.get("check_level")
            if not check_level or not risk_level:
                continue
            if check_level == risk_level:
                continue
            notes.append(
                ConversionNote(
                    code="LEVEL_MISMATCH",
                    message=(
                        f"风险「{risk['name']}」的风险等级是“{risk_level}”，来源检查「{rule['name']}」"
                        f"的检查等级是“{check_level}”：二者独立保留、不相互覆盖，该差异需要业务确认。"
                    ),
                    sheet_key=reader.sheet_key,
                    sheet_name=reader.sheet_name,
                    row_no=risk["source"]["row_no"],
                    cell=None,
                    column="风险等级",
                    value=risk_level,
                    question_title=f"风险「{risk['name']}」与其来源检查「{rule['name']}」的等级不一致",
                    question_options=[
                        {"label": "保留两者原等级（不覆盖）", "value": {"keep_both": True}},
                    ],
                )
            )
    return notes


# 本模块负责材料、字段、检查、风险与报告 Sheet；计算三表由 financial_report 负责。
OWNED_ROLES: frozenset[str] = frozenset(
    {
        "MATERIAL_LIST",
        "MATERIAL_QUALITY",
        "FIELD_EXTRACTION",
        "INTERNAL_VALIDATE",
        "CROSS_VALIDATE",
        "RISK",
        "REPORT_TEMPLATE",
    }
)


def _stamp_rule_node(
    conversion: SheetConversion,
    *,
    node_code_by_sheet: dict[str, str],
) -> None:
    """把一条规则的能力类型换成它所属 Sheet 绑定的**节点实例码**。

    转换函数按业务语义写的是能力类型（`INTERNAL_VALIDATE`、`RISK_RATING` …），
    而规则在已发布配置里的归属必须是用户配置的实例码：同一场景可以有两个同类节点，
    能力类型不足以定位到其中哪一个。

    实例码来自这张 Sheet 的绑定结果（`import_sheet.bound_node_code`）。绑定缺失时
    明确报错——静默退回能力类型会让规则挂到一个用户并没有建立的节点上，
    发布期才以「规则指向不存在的节点」报出，那时已经看不出真实原因。
    """
    if not conversion.proposals["rules"]:
        return
    sheet_key = conversion.reader.sheet_key
    node_code = node_code_by_sheet.get(sheet_key)
    if node_code is None:
        raise BusinessError(
            "SHEET_NODE_REQUIRED",
            f"工作表「{conversion.reader.sheet_name}」上的规则没有可归属的节点实例："
            "该工作表没有绑定业务节点",
            details={"sheet_key": sheet_key, "sheet_name": conversion.reader.sheet_name},
        )
    for rule in conversion.proposals["rules"]:
        rule["node_type"] = rule["node"]
        rule["node"] = node_code


def _optional_conversion(convert: Any, reader: SheetReader | None, **kwargs: Any) -> SheetConversion | None:
    """本场景没绑定这张表时返回 None，否则照常转换。

    「没绑定」与「绑定了但内容不合法」是两件事：前者是配置事实（这个场景不配这类检查），
    后者由转换函数照常明确报错。这里只处理前者，不吞任何异常。
    """
    if reader is None:
        return None
    return convert(reader, **kwargs)


def _reader(readers: dict[str, SheetReader], role: str) -> SheetReader:
    """读取材料位表；没有材料身份时字段和检查无法建立来源关系。"""
    reader = readers.get(role)
    if reader is None:
        raise BusinessError(
            "SHEET_ROLE_MISSING",
            f"当前导入没有绑定「{role}」对应的 Sheet；请在绑定步骤把相关工作表绑定到对应节点",
            details={"sheet_role": role, "bound_roles": sorted(readers)},
        )
    return reader


def _optional_reader(readers: dict[str, SheetReader], role: str) -> SheetReader | None:
    """按角色取读取器；本场景没有绑定这张表时返回 None。

    材料位表是本转换器的身份基础；字段、检查、风险与报告表按实际绑定读取。

    它**不吞掉**任何真实问题：表绑定了但列缺失、表头行不存在、内容填错，
    仍由 `SheetReader` 与各转换函数照常明确报错。
    """
    return readers.get(role)


@dataclass
class NodeSheets:
    """一份节点版规则库的全部工作表读取器。"""

    by_role: dict[str, SheetReader]
    order: list[str]


def build_convert_bundle(
    ir: WorkbookIR,
    bindings: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """把已绑定的非计算 Sheet 确定性转换成候选集合。

    返回结构与 `ProposalBundle` 兼容的字典，并附带：
      - `node_notes`：必须留给业务处理的待确认事项与能力缺口；
      - `coverage`：逐业务行的处置对账。

    只转换**用户已绑定**的 Sheet：未绑定或被排除的 Sheet 不进入转换，
    也不产生候选——绑定结果是本次场景配置的明确输入，系统不按表头自动推断用途。

    本函数不调用任何模型，不读取向量索引。
    """
    # 统一模板协议是全系统唯一的一份：列协议按它取。
    # 转换分派只使用用户确认的 sheet_role，不按名称或表头猜。
    profile = unified_profile.load_unified_profile()
    sheet_definitions = unified_profile.sheet_index(profile)
    readers: dict[str, SheetReader] = {}
    # Sheet 键 → 用户绑定的**节点实例码**。规则必须落在实例上，不能落在能力类型上：
    # 同一场景可以有两个同类节点，用能力类型当归属会让两条检查都挂到同一个节点上，
    # 而发布期的节点覆盖校验只看实例码，于是规则会被判成「指向不存在的节点」。
    node_code_by_sheet: dict[str, str] = {}
    for sheet in ir.sheets:
        binding = bindings.get(sheet.sheet_key, {})
        # 判断依据只有用户绑定或明确排除：**不看 `sheet.visible`**。
        # 隐藏只是 Excel 的展示属性，隐藏的业务 Sheet 仍然可以由用户绑定与导入；
        # 按隐藏跳过会让一份用户明确绑定好的表在转换期被静默丢掉。
        if binding.get("excluded"):
            continue
        unified_role = str(binding.get("role"))
        if unified_role not in OWNED_ROLES:
            continue
        short_role = unified_role
        node_code = binding.get("node_code")
        if not isinstance(node_code, str) or not node_code:
            raise BusinessError(
                "SHEET_NODE_REQUIRED",
                f"工作表「{sheet.name}」没有绑定业务节点，无法确定它上面的规则归属哪个节点实例",
                details={"sheet_key": sheet.sheet_key, "sheet_name": sheet.name, "role": unified_role},
            )
        node_code_by_sheet[sheet.sheet_key] = node_code

        # 空白模板可以读取和填写：只有表头、没有任何业务数据行的 Sheet，只要它绑定了
        # 业务角色，就必须保留该角色并转换成**零行业务集合**。
        # 在预处理阶段按「无业务数据」把它排除、之后又报缺少业务表，会让空白模板
        # 永远无法通过转换——两处说法必须一致。
        header_row = binding.get("header_row")
        if header_row is None:
            raise BusinessError(
                "SHEET_HEADER_REQUIRED",
                f"工作表「{sheet.name}」缺少表头行",
                details={"sheet_key": sheet.sheet_key, "sheet_name": sheet.name},
            )
        header = next((row for row in sheet.rows if row.row_no == header_row), None)
        if header is None:
            raise BusinessError(
                "SHEET_HEADER_SOURCE_MISSING",
                f"工作表「{sheet.name}」不存在所选表头行",
                details={"sheet_key": sheet.sheet_key, "sheet_name": sheet.name, "row_no": header_row},
            )
        if short_role in readers:
            # 同一角色绑定多个 Sheet 必须明确报错：readers[role] 只能保留一个，
            # 后一个覆盖前一个会让前一张表的数据被静默丢掉，转换结果看起来正常却少了内容。
            raise BusinessError(
                "SHEET_ROLE_DUPLICATED",
                f"工作表「{sheet.name}」与「{readers[short_role].sheet_name}」都被绑定为"
                f"「{sheet_definitions[unified_role]['display_name']}」：同一用途只能绑定一张工作表",
                details={
                    "role": unified_role,
                    "sheet_names": [readers[short_role].sheet_name, sheet.name],
                },
            )
        readers[short_role] = SheetReader(
            sheet_key=sheet.sheet_key,
            sheet_name=sheet.name,
            role_definition=sheet_definitions[unified_role],
            header_row=header_row,
            header_values=header.cells,
            rows=[{"row_no": row.row_no, "cells": row.cells} for row in sheet.rows],
        )

    # 材料位表提供字段与检查引用的材料身份；其余 Sheet 按实际绑定转换。
    materials_conversion = _convert_materials(_reader(readers, "MATERIAL_LIST"))
    material_proposals = materials_conversion.proposals["materials"]
    material_by_code = _reference_index(material_proposals, kind="材料")

    fields_conversion = _optional_conversion(
        _convert_fields,
        _optional_reader(readers, "FIELD_EXTRACTION"),
        material_by_code=material_by_code,
    )
    field_proposals = fields_conversion.proposals["fields"] if fields_conversion is not None else []
    field_catalog = _build_field_catalog(field_proposals, material_proposals)
    field_by_code = {item["source_id"]: item for item in field_proposals}

    quality_conversion = _optional_conversion(
        _convert_material_quality,
        _optional_reader(readers, "MATERIAL_QUALITY"),
        material_by_code=material_by_code,
    )
    internal_conversion = _optional_conversion(
        _convert_internal_validate,
        _optional_reader(readers, "INTERNAL_VALIDATE"),
        material_by_code=material_by_code,
        field_catalog=field_catalog,
        field_by_code=field_by_code,
    )
    cross_conversion = _optional_conversion(
        _convert_cross_validate,
        _optional_reader(readers, "CROSS_VALIDATE"),
        material_by_code=material_by_code,
        field_catalog=field_catalog,
        field_by_code=field_by_code,
    )
    risk_conversion = _optional_conversion(
        _convert_risks,
        _optional_reader(readers, "RISK"),
    )
    report_conversion = _optional_conversion(
        _convert_reports,
        _optional_reader(readers, "REPORT_TEMPLATE"),
        material_by_code=material_by_code,
        field_by_code=field_by_code,
        field_catalog=field_catalog,
    )

    quality_rules = quality_conversion.proposals["rules"] if quality_conversion is not None else []
    internal_rules = internal_conversion.proposals["rules"] if internal_conversion is not None else []
    cross_rules = cross_conversion.proposals["rules"] if cross_conversion is not None else []
    risk_rules = risk_conversion.proposals["rules"] if risk_conversion is not None else []

    comparison_rules = [
        *quality_rules,
        *internal_rules,
        *cross_rules,
    ]
    _assert_no_disabled_risk_links(
        (quality_conversion, internal_conversion, cross_conversion), risk_conversion
    )
    _resolve_risk_references(comparison_rules, risk_rules)

    # 规则归属改为**用户绑定的节点实例码**：转换函数内部按业务书写习惯写的是能力类型
    # （「这条检查是内部校验」），这里把它换成该 Sheet 绑定的实例码，并把能力类型
    # 记进 `node_type`。转换函数因此不必各自知道实例码，归属只有一个改写点。
    for conversion in (quality_conversion, internal_conversion, cross_conversion, risk_conversion):
        if conversion is not None:
            _stamp_rule_node(conversion, node_code_by_sheet=node_code_by_sheet)

    rule_by_ref = {rule["candidate_ref"]: rule for rule in [*comparison_rules, *risk_rules]}
    risk_level_notes = (
        _assert_level_conflicts(risk_rules, rule_by_ref, risk_conversion.reader)
        if risk_conversion is not None
        else []
    )
    if report_conversion is not None:
        _assert_report_group_consistency(report_conversion.proposals["report_sections"], report_conversion.reader)

    # 本场景没绑定的表不进入候选集合，也不产生覆盖对账行——它不参与本次转换。
    conversions = [
        conversion
        for conversion in (
            materials_conversion,
            fields_conversion,
            quality_conversion,
            internal_conversion,
            cross_conversion,
            risk_conversion,
            report_conversion,
        )
        if conversion is not None
    ]
    notes: list[ConversionNote] = [*risk_level_notes]
    for conversion in conversions:
        notes.extend(conversion.notes)

    sheets = [
        {
            "sheet_key": conversion.reader.sheet_key,
            "suggested_role": _role_of(conversion.reader.sheet_key, readers),
            "column_mapping": dict(conversion.reader.column_keys),
            "materials": conversion.proposals["materials"],
            "fields": conversion.proposals["fields"],
            "rules": conversion.proposals["rules"],
            "report_sections": conversion.proposals["report_sections"],
            "disabled_rows": _disabled_rows_of_conversion(conversion),
            "questions": [],
        }
        for conversion in conversions
    ]
    coverage_sheets = [
        {
            "sheet_key": conversion.reader.sheet_key,
            "sheet_name": conversion.reader.sheet_name,
            "business_rows": len(conversion.outcomes),
            "rows": [
                {
                    "row_no": outcome.row_no,
                    "disposition": outcome.disposition,
                    "reason": outcome.reason,
                    "candidate_count": len(outcome.candidate_refs),
                    "question_count": 0,
                    "merged_into": None,
                    "notes": list(outcome.notes),
                }
                for outcome in conversion.outcomes
            ],
        }
        for conversion in conversions
    ]
    totals = coverage_totals(coverage_sheets)
    bundle_hash = hashlib.sha256(
        json.dumps(
            {
                "materials": [item["candidate_ref"] for item in material_proposals],
                "fields": [item["candidate_ref"] for item in field_proposals],
                "rules": [item["candidate_ref"] for item in [*comparison_rules, *risk_rules]],
                "disabled_rows": [sheet["disabled_rows"] for sheet in sheets],
                "sections": (
                    [item["candidate_ref"] for item in report_conversion.proposals["report_sections"]]
                    if report_conversion is not None
                    else []
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "sheets": sheets,
        "coverage": {"sheets": coverage_sheets, "totals": totals},
        "node_notes": [note.to_payload() for note in notes],
        "bundle_hash": bundle_hash,
    }


def _role_of(sheet_key: str, readers: dict[str, SheetReader]) -> str:
    for role, reader in readers.items():
        if reader.sheet_key == sheet_key:
            return role
    raise BusinessError("SHEET_ROLE_MISSING", "工作表用途缺失", status_code=500)
