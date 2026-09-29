"""计算三表的确定性转换。

转换**按 Sheet 独立进行、结果合并**：本模块只负责
06_计算定义 / 07_计算参数 / 08_计算判定 三张表，其余 Sheet 由 `node_rulebook` 负责
（见 `llm_service.convert_workbook_with_notes`）。两边的候选共用 `ProposalBundle` 契约，
因此下游的引用校验、裁决、编译、发布与页面展示不需要第二套代码。

确定性边界
----------
「确定性解析」不等于「什么都能解析出来」。本模块只做两类判定：

1. **精确匹配**：公式里的标识符与已声明的科目名称/上游指标名称完全相同；
2. **已声明的期间前缀**：`本期 / 上期 / 期初 / 期末 / 平均` 是可枚举的期间声明，
   剥离后仍须精确匹配到科目名称。

除此之外一律**明确报错并保留原值**。不做近似匹配、不按相似度挑最像的科目、
不把「流动资产」自动当成「流动资产合计」——这两个名字在真实报表里是不同的行，
猜错会让整条指标算出一个看起来完全正常的错误值。

样本里缺的东西不补：`所得税费用`、`折旧摊销`、`资本性支出` 等在 03 里没有定义时，
编译阶段直接报出缺失依赖。

计算三表
--------
计算规则由 `CALC_DEFINITION`、`CALC_PARAMETER`、`CALC_JUDGEMENT` 按规则编号关联：**参数与判定区间只来自各自那张 Sheet**，
不由系统从公式文本或「结果区间定义」描述列推断。公式里的标识符仍会被解析，
但与参数表逐条核对——两侧不一致即报错，不静默取一边。

RAG 不参与本模块任何一步。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl.utils import get_column_letter

from ..errors import BusinessError
from .manifest import label_resolver
from . import calculation as calc
from . import unified_profile
from .proposals import coverage_totals
from .workbook import WorkbookIR, WorkbookRow, WorkbookSheet


# 07「触发条件」列里的规则编号。只做编号抽取，不解释自然语言条件语义。
# 不能用 \b 词边界：中文与字母之间不构成词边界（`FSCV-007命中` 的 `7` 与 `命`
# 都是 \w），加边界会让紧邻中文的编号抽不出来。改用显式的前后视断言：
# 左侧不能是字母数字或连字符（避免从长编号中间截取），右侧不能是数字或连字符。
_RULE_CODE = re.compile(r"(?<![A-Za-z0-9-])([A-Z]{2,6}-[A-Z0-9]{1,8}-\d{1,4}|[A-Z]{2,6}-\d{1,4})(?![\d-])")

# 05/06「结果区间定义」列：`<1：预警；1~1.5：一般；1.5~2：良好；>2：优秀`
_BAND_SEPARATOR = "；"
_BAND_ITEM = re.compile(
    r"^(?P<prefix>[<>≤≥])?\s*(?P<first>-?\d+(?:\.\d+)?)\s*(?P<percent>%?)\s*"
    r"(?:(?P<range>[~～\-])\s*(?P<second>-?\d+(?:\.\d+)?)\s*(?P<percent2>%?))?$"
)

# 指标类别 → 结果类型。类别是业务在 06 里选的枚举，结果类型是执行契约里的值类型。
# 映射是**显式**的：比率与增长率产出无量纲比率，金额与差额产出金额。
# 表里没有的类别不猜，报错。
_CATEGORY_RESULT_TYPE: dict[str, str] = {
    "RATIO": "PERCENT",
    "GROWTH": "PERCENT",
    "AMOUNT": "AMOUNT",
    "DIFFERENCE": "AMOUNT",
}


# 本模块负责的 Sheet 角色：**计算三表**。其余 Sheet 由 `node_rulebook` 负责，
# 两边按已绑定的 Sheet 角色分派、结果合并（见 llm_service.convert_workbook_with_notes）。
OWNED_ROLES: frozenset[str] = frozenset(
    {"CALC_DEFINITION", "CALC_PARAMETER", "CALC_JUDGEMENT"}
)


def convert_disabled_calculation_rows(
    rows: list[dict[str, Any]], *, scenario_code: str, node_codes: dict[str, str], profile_version: str,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把编辑后的 06/07/08 来源行交回原有计算转换器，避免另写一套解析口径。"""
    # 重新启用历史来源行时必须使用草稿冻结的场景有效 Profile；独立调用才回退到内部协议。
    profile = profile or unified_profile.load_unified_profile()
    if profile_version != profile["profile_version"]:
        raise BusinessError(
            "DISABLED_SOURCE_ROW_PROFILE_CHANGED",
            "停用来源行使用的模板协议与当前协议不同，不能按新列定义启用",
            status_code=409,
            details={"draft_profile_version": profile_version, "current_profile_version": profile["profile_version"]},
        )
    definitions = unified_profile.sheet_index(profile)
    sheets: list[WorkbookSheet] = []
    bindings: dict[str, dict[str, Any]] = {}
    for role in ("CALC_DEFINITION", "CALC_PARAMETER", "CALC_JUDGEMENT"):
        members = [row for row in rows if row["sheet_role"] == role]
        if not members:
            continue
        node_code = node_codes.get(role)
        if not node_code:
            raise BusinessError(
                "DISABLED_CALC_SHEET_UNBOUND",
                "停用计算行所属工作表未绑定计算节点，不能启用",
                status_code=409,
                details={"sheet_role": role},
            )
        definition = definitions[role]
        columns = definition["columns"]
        expected_keys = [column["key"] for column in columns]
        sheet_keys = {row["source"]["sheet_key"] for row in members}
        sheet_names = {row["sheet_name"] for row in members}
        row_numbers = [row["source"]["row_no"] for row in members]
        if len(sheet_keys) != 1 or len(sheet_names) != 1 or len(set(row_numbers)) != len(row_numbers) or min(row_numbers) <= 1:
            raise BusinessError(
                "DISABLED_CALC_SOURCE_CONFLICT",
                "同一计算工作表的来源或行号不一致，不能合并转换",
                status_code=409,
                details={"sheet_role": role, "row_keys": [row["row_key"] for row in members]},
            )
        sheet_key = next(iter(sheet_keys))
        sheet_name = next(iter(sheet_names))
        header = WorkbookRow(
            row_no=1,
            cells={f"{get_column_letter(index)}1": column["name"] for index, column in enumerate(columns, start=1)},
            cell_details=[],
        )
        business_rows: list[WorkbookRow] = []
        for row in members:
            saved_columns = row["columns"]
            if [column["key"] for column in saved_columns] != expected_keys:
                raise BusinessError(
                    "DISABLED_SOURCE_ROW_PROFILE_CHANGED",
                    "停用来源行的列结构与当前模板协议不同，不能启用",
                    status_code=409,
                    details={"row_key": row["row_key"], "sheet_role": role},
                )
            row_no = row["source"]["row_no"]
            business_rows.append(WorkbookRow(
                row_no=row_no,
                cells={
                    f"{get_column_letter(index)}{row_no}": column["value"]
                    for index, column in enumerate(saved_columns, start=1)
                    if column["value"] != ""
                },
                cell_details=[],
            ))
        sheet_rows = [header, *sorted(business_rows, key=lambda item: item.row_no)]
        sheets.append(WorkbookSheet(
            sheet_id=sheet_key,
            sheet_key=sheet_key,
            name=sheet_name,
            original_name=sheet_name,
            visible=True,
            state="DATA",
            max_row=max(row_numbers),
            max_column=len(columns),
            non_empty_rows=len(sheet_rows),
            used_range=f"A1:{get_column_letter(len(columns))}{max(row_numbers)}",
            merged_ranges=[],
            header_candidates=[1],
            rows=sheet_rows,
            preview_rows=sheet_rows,
            rows_truncated=False,
            columns_truncated=False,
            cells_truncated=False,
            suggested_role=role,
        ))
        bindings[sheet_key] = {"role": role, "node_code": node_code, "header_row": 1}
    replay = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ir = WorkbookIR(
        workbook_id=f"draft:{scenario_code}",
        source_filename="scenario-draft-source-rows",
        source_size=len(replay),
        source_sha256=hashlib.sha256(replay).hexdigest(),
        profile_version=profile_version,
        scenario_code=scenario_code,
        sheets=sheets,
    )
    return build_convert_bundle(ir, bindings, profile)


@dataclass
class SheetConversion:
    """一张 Sheet 的转换结果。与 node_rulebook.SheetConversion 同形，供统一收口。"""

    sheet_key: str
    sheet_name: str
    materials: list[dict[str, Any]] = field(default_factory=list)
    fields: list[dict[str, Any]] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)
    calculations: list[dict[str, Any]] = field(default_factory=list)
    report_sections: list[dict[str, Any]] = field(default_factory=list)
    disabled_rows: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    outcomes: list[dict[str, Any]] = field(default_factory=list)


def _column_letter_of(address: str) -> str:
    """单元格地址的列字母部分（`B12` → `B`）。"""
    letters = "".join(char for char in address if char.isalpha())
    return letters.upper()


def _source_ref(sheet_key: str, row_no: int, cells: list[str], texts: dict[str, str]) -> dict[str, Any]:
    return {"sheet_key": sheet_key, "row_no": row_no, "cells": cells, "texts": texts}


def _row_texts(row: dict[str, Any]) -> dict[str, str]:
    return {
        address: str(value)
        for address, value in (row.get("cells") or {}).items()
        if value not in (None, "")
    }



def _error(
    code: str,
    message: str,
    *,
    sheet_key: str,
    sheet_name: str,
    row_no: int,
    column: str,
    value: Any = None,
    cell: str | None = None,
    **extra: Any,
) -> BusinessError:
    details: dict[str, Any] = {
        "sheet_key": sheet_key,
        "sheet_name": sheet_name,
        "row_no": row_no,
        "column": column,
    }
    if cell:
        details["cell"] = cell
    if value not in (None, ""):
        details["value"] = str(value)
    details.update(extra)
    return BusinessError(code, message, details=details)


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _optional_yes_no(row: dict[str, Any], key: str) -> bool | None:
    """「是 / 否」列的取值：空白返回 None（未填写），不代填成「是」。

    含不含边界在临界值上是两个不同的业务结论，系统不替业务选一个默认值；
    返回 None 后由计算契约在发布期明确报「该字段必须显式填写」。
    除「是 / 否」以外的取值同样原样交回契约报错，不在这里猜。
    """
    text = _text(row, key)
    if not text:
        return None
    if text == "是":
        return True
    if text == "否":
        return False
    return text  # 非法取值交给契约报错，保留原值便于定位




def _strip_trailing_annotations(formula: str) -> tuple[str, list[str]]:
    """剥掉公式末尾的全角括号业务注解，返回（算式, 被剥掉的注解）。

    样本里业务会在算式末尾补一句说明：`流动负债 - 流动资产（缺口为正值）`。
    注解用全角括号包起来，且**只在末尾**——算式内部不会出现全角括号
    （表达式里的括号必须写成半角，否则无法与注解区分）。

    只剥末尾且括号配平的全角括号段；中间出现的全角括号不处理，
    由语法分析如实报错——那更可能是业务写错了算式，而不是注解。
    """
    text = formula.strip()
    annotations: list[str] = []
    while text.endswith("）"):
        depth = 0
        start = -1
        for index in range(len(text) - 1, -1, -1):
            char = text[index]
            if char == "）":
                depth += 1
            elif char == "（":
                depth -= 1
                if depth == 0:
                    start = index
                    break
        if start < 0:
            break
        annotations.insert(0, text[start:])
        text = text[:start].strip()
    return text, annotations



class FinancialReportConverter:
    """财报场景的确定性转换器。

    转换按 **Sheet 角色**进行，不按 Sheet 名称：同一份文件里 Sheet 改名不影响结果，
    而把「04_勾稽核算规则库」错绑成计算定义会立刻在列校验处报错。
    """

    def __init__(self, ir: WorkbookIR, bindings: dict[str, dict[str, Any]], profile: dict[str, Any]) -> None:
        self.ir = ir
        self.bindings = bindings
        self.profile = profile
        # 列协议来自导入时传入的场景有效 Profile。
        self.sheet_definitions = unified_profile.sheet_index(profile)
        # 当前正在转换的 Sheet：待确认事项由解析过程产生，需要一个落点。
        # 它在每次 convert_* 开始时被设置，不是跨调用共享状态。
        self._pending_conversion: SheetConversion | None = None

    def _begin(self, sheet: Any) -> SheetConversion:
        conversion = SheetConversion(sheet_key=sheet.sheet_key, sheet_name=sheet.name)
        self._pending_conversion = conversion
        return conversion

    def _disabled_row(self, sheet_role: str, sheet: Any, row: dict[str, Any]) -> dict[str, Any]:
        definition = self.sheet_definitions[sheet_role]
        texts = _row_texts(row)
        return {
            "source": _source_ref(sheet.sheet_key, row["row_no"], sorted(texts), texts),
            "sheet_name": sheet.name,
            "source_id": _text(row, "rule_code") or None,
            "columns": [
                {
                    "key": column["key"],
                    "label": column["name"],
                    "value": _text(row, column["key"]),
                    "cell": (row.get("_column_cells") or {}).get(column["key"]),
                }
                for column in definition["columns"]
            ],
        }

    def _assert_code_prefix(
        self, sheet_role: str, row: dict[str, Any], key: str, *, code_role: str | None = None
    ) -> None:
        value = _text(row, key)
        prefix = str(self.sheet_definitions[code_role or sheet_role]["code_prefix"])
        if value.startswith(prefix) and len(value) > len(prefix):
            return
        sheet = self._sheet_of(sheet_role)
        column = next(item["name"] for item in self.sheet_definitions[sheet_role]["columns"] if item["key"] == key)
        raise _error(
            "CALC_CODE_PREFIX_INVALID",
            f"工作表「{sheet.name}」第 {row['row_no']} 行的「{column}」必须以“{prefix}”开头且包含编号内容",
            sheet_key=sheet.sheet_key,
            sheet_name=sheet.name,
            row_no=row["row_no"],
            column=column,
            value=value,
            expected_prefix=prefix,
        )

    # 读取

    def _bound(self, sheet_role: str) -> tuple[Any, int] | None:
        """按 Sheet 角色找已绑定的工作表。

        绑定关系是用户确认后落库的（`import_sheet.bound_role`），这里只读它——
        不按 Sheet 名称或表头重新推断：系统给的建议只用于页面预选，
        未确认的建议不构成绑定。
        """
        matches: list[tuple[Any, dict[str, Any]]] = []
        for sheet in self.ir.sheets:
            binding = self.bindings.get(sheet.sheet_key) or {}
            # 判断依据只有用户绑定或明确排除：隐藏只是 Excel 展示属性，
            # 隐藏的业务 Sheet 仍可由用户绑定并参与转换。
            if binding.get("excluded"):
                continue
            if binding.get("role") == sheet_role:
                matches.append((sheet, binding))
        if len(matches) > 1:
            raise BusinessError(
                "SHEET_ROLE_DUPLICATED",
                f"工作表用途「{self.sheet_definitions[sheet_role]['display_name']}」绑定了多张工作表，无法确定使用哪一张",
                details={"role": sheet_role, "sheet_names": [sheet.name for sheet, _ in matches]},
            )
        if not matches:
            return None
        sheet, binding = matches[0]
        header_row = binding.get("header_row")
        if not isinstance(header_row, int):
            raise BusinessError(
                "SHEET_HEADER_REQUIRED",
                f"工作表「{sheet.name}」没有选择表头行",
                details={"sheet_key": sheet.sheet_key, "sheet_name": sheet.name},
            )
        return sheet, header_row

    def read(self, sheet_role: str, *, required: bool = True) -> tuple[Any, int, list[dict[str, Any]]] | None:
        """按角色读取一张业务表，返回（Sheet, 表头行, 业务行）。

        表头列名与 profile 声明的列名必须一一对应：**未知列、重复列、缺必填列**
        都在这里明确报错。列顺序可以变化——映射按列名建立，不按位置。
        """
        bound = self._bound(sheet_role)
        if bound is None:
            if required:
                raise BusinessError(
                    "SHEET_ROLE_MISSING",
                    f"当前导入没有绑定「{self.sheet_definitions[sheet_role]['display_name']}」对应的 Sheet。"
                    "请在绑定步骤确认每张工作表对应的业务节点",
                    details={
                        "sheet_role": sheet_role,
                        "display_name": self.sheet_definitions[sheet_role]["display_name"],
                    },
                )
            return None
        sheet, header_row = bound
        definition = self.sheet_definitions[sheet_role]
        header = next((row for row in sheet.rows if row.row_no == header_row), None)
        if header is None:
            raise BusinessError(
                "SHEET_HEADER_SOURCE_MISSING",
                f"工作表「{sheet.name}」不存在所选表头行",
                details={"sheet_key": sheet.sheet_key, "sheet_name": sheet.name, "row_no": header_row},
            )
        column_keys = unified_profile.map_sheet_columns(
            definition,
            header.cells,
            sheet_key=sheet.sheet_key,
            sheet_name=sheet.name,
            header_row=header_row,
            required_keys={column["key"] for column in definition["columns"] if column.get("required")},
        )
        # 表头坐标是第 1 行的 A1/B1…，数据行是 A2/B2…：必须按**列字母**映射，
        # 按完整坐标映射会让每一行的数据都匹配不上表头，转换结果看起来成功却是空的。
        column_to_key = {
            _column_letter_of(address): key for key, address in column_keys.items()
        }
        rows: list[dict[str, Any]] = []
        for row in sheet.rows:
            if row.row_no <= header_row:
                continue
            mapped: dict[str, Any] = {
                "row_no": row.row_no,
                "cells": dict(row.cells),
                "_column_cells": {
                    key: f"{column}{row.row_no}" for column, key in column_to_key.items()
                },
            }
            for address, value in row.cells.items():
                key = column_to_key.get(_column_letter_of(address))
                if key is None:
                    continue
                mapped[key] = value
            if any(str(mapped.get(column["key"]) or "").strip() for column in definition["columns"]):
                rows.append(mapped)
        return sheet, header_row, rows

    # 06/07/08 计算三表 → 计算规则候选

    def _enum_of(
        self,
        row: dict[str, Any],
        key: str,
        *,
        definition: dict[str, Any],
        sheet: Any,
        row_no: int,
        rule_name: str,
        required: bool = True,
    ) -> str | None:
        """按列协议读取一个枚举列，把中文标签映射回**内部取值**。

        `required=False` 时空白返回 None，由**发布期**阻断（与「含不含边界」同一口径：
        缺失就是缺失，不在导入期代填一个默认值）。取值不在允许集合内时无论是否必填都明确报错。
        """
        column = next((item for item in definition["columns"] if item["key"] == key), None)
        label = (column or {}).get("name", key)
        # 用协议侧**唯一**的标签解析器：`value_labels` 在协议里有两种书写方向
        # （「内部取值→中文标签」与「界面标签→契约值」），由 `label_resolver` 按
        # `allowed_values` 自适应并只接受落在契约取值集合内的映射。
        # 自己反转一次会踩它 docstring 里写明的那个陷阱：写反方向时
        # 「一级」被解析成并不存在的「一级风险」，而「一级风险」又被判成非法。
        resolved_labels = label_resolver(column or {})
        raw = _text(row, key)
        if not raw and not required:
            return None
        if raw in resolved_labels:
            return resolved_labels[raw]
        shown = sorted(resolved_labels)
        raise _error(
            "CALC_ENUM_VALUE_INVALID",
            f"工作表「{sheet.name}」第 {row_no} 行指标“{rule_name}”的「{label}」"
            f"填写的“{raw}”不在模板允许取值内（允许：{'、'.join(shown)}），"
            "请使用下拉选项填写",
            sheet_key=sheet.sheet_key,
            sheet_name=sheet.name,
            row_no=row_no,
            column=label,
            value=raw,
        )

    def input_sheet_coverage(
        self, sheet_role: str, *, definition_codes: set[str], disabled_definition_codes: set[str]
    ) -> SheetConversion | None:
        """只做逐行对账、不产出候选的输入表（07_计算参数 / 08_计算判定）。

        两表按规则编号被 06 引用；能对上定义的行走 CANDIDATE（它确实参与了这条计算规则），
        对不上的行是跨表引用错误，不能作为 EXCLUDED 静默丢弃。
        未绑定该表时返回 None——「没绑定」是配置事实，不是缺行。
        """
        bound = self.read(sheet_role, required=False)
        if bound is None:
            return None
        sheet, _header_row, rows = bound
        conversion = self._begin(sheet)
        for row in rows:
            rule_code = _text(row, "rule_code")
            if rule_code in disabled_definition_codes:
                conversion.disabled_rows.append(self._disabled_row(sheet_role, sheet, row))
                conversion.outcomes.append({
                    "row_no": row["row_no"],
                    "disposition": "DISABLED",
                    "reason": "关联的计算定义已停用，原始填写内容保留在场景草稿中",
                })
                continue
            if rule_code not in definition_codes:
                raise _error(
                    "CALC_INPUT_RULE_UNKNOWN",
                    f"工作表「{sheet.name}」第 {row['row_no']} 行引用的计算规则“{rule_code}”"
                    "在 06_计算定义 中不存在，不能导入该行",
                    sheet_key=sheet.sheet_key,
                    sheet_name=sheet.name,
                    row_no=row["row_no"],
                    column="规则编号",
                    value=rule_code,
                    sheet_role=sheet_role,
                )
            conversion.outcomes.append(
                {
                    "row_no": row["row_no"],
                    "disposition": "CANDIDATE",
                    "reason": None,
                    "candidate_refs": [f"calc:{rule_code}"],
                    "notes": [],
                }
            )
        return conversion

    def convert_calculations(self, sheet_role: str) -> SheetConversion | None:
        """计算定义表 → 计算规则候选（**参数与判定来自各自的 Sheet**）。

        计算规则由三张表按 `rule_code` 关联：

        - `CALC_DEFINITION`：指标名称、类别、公式、结果期间与报表口径；
        - `CALC_PARAMETER`：参数声明（取值来源、期间/主体/报表口径、单位币种）；
        - `CALC_JUDGEMENT`：判定区间（上下界与是否含边界、比较基准、判定结论）。

        三张表都必须真实读取：以前参数由公式标识符推断、判定由「结果区间定义」
        文本列解析，那两个 Sheet 声明了却没人读——业务在表里填的内容不起作用，
        而页面上看不出任何异常。现在参数与判定**只能来自对应 Sheet**，
        定义表里的同名文本列不再作为取值来源。

        公式里的标识符仍要解析：它是「这条公式引用了哪些参数」的依据，
        解析结果与参数表逐条核对；缺少参数声明时记录来源和缺口，由发布编译阻断。
        """
        bound = self.read(sheet_role, required=False)
        if bound is None:
            return None
        sheet, header_row, rows = bound
        conversion = self._begin(sheet)
        seen_rule_codes: set[str] = set()
        for row in rows:
            row_no = row["row_no"]
            texts = _row_texts(row)
            cells = sorted(texts)
            rule_code = _text(row, "rule_code")
            rule_name = _text(row, "rule_name")
            formula = _text(row, "formula")
            if rule_code in seen_rule_codes:
                raise _error(
                    "CALC_RULE_CODE_DUPLICATE",
                    f"工作表「{sheet.name}」第 {row_no} 行的计算规则编号“{rule_code}”重复",
                    sheet_key=sheet.sheet_key,
                    sheet_name=sheet.name,
                    row_no=row_no,
                    column="规则编号",
                    value=rule_code,
                )
            if rule_code:
                seen_rule_codes.add(rule_code)
            if _optional_yes_no(row, "enabled") is False:
                conversion.disabled_rows.append(self._disabled_row(sheet_role, sheet, row))
                conversion.outcomes.append({
                    "row_no": row_no,
                    "disposition": "DISABLED",
                    "reason": "该计算定义已停用，原始填写内容保留在场景草稿中",
                })
                continue
            if not rule_code:
                raise _error(
                    "CALC_RULE_ID_MISSING",
                    f"工作表「{sheet.name}」第 {row_no} 行没有填写「规则ID」",
                    sheet_key=sheet.sheet_key,
                    sheet_name=sheet.name,
                    row_no=row_no,
                    column="规则ID",
                )
            self._assert_code_prefix("CALC_DEFINITION", row, "rule_code")
            if not rule_name:
                raise _error(
                    "CALC_RULE_NAME_MISSING",
                    f"工作表「{sheet.name}」第 {row_no} 行没有填写指标名称",
                    sheet_key=sheet.sheet_key,
                    sheet_name=sheet.name,
                    row_no=row_no,
                    column="指标名称",
                )
            if not formula:
                raise _error(
                    "CALC_FORMULA_MISSING",
                    f"工作表「{sheet.name}」第 {row_no} 行指标“{rule_name}”没有填写计算公式",
                    sheet_key=sheet.sheet_key,
                    sheet_name=sheet.name,
                    row_no=row_no,
                    column="计算公式",
                )
            # 公式里的标识符：它决定这条规则引用了哪些参数，必须与参数表逐条对上。
            expression_text, annotations = _strip_trailing_annotations(formula)
            if annotations:
                conversion.notes.append(
                    {
                        "code": "CALC_FORMULA_ANNOTATION_STRIPPED",
                        "message": (
                            f"计算规则“{rule_name}”（{rule_code}）的「计算公式」末尾带有业务注解"
                            "“" + "".join(annotations) + "”，它不是算式的一部分。"
                            f"系统按去掉注解后的算式“{expression_text}”解析"
                        ),
                        "sheet_key": sheet.sheet_key,
                        "sheet_name": sheet.name,
                        "row_no": row_no,
                        "column": "计算公式",
                        "value": formula,
                    }
                )
            identifiers = calc.identifiers(
                calc.parse_expression(
                    expression_text,
                    location=f"工作表「{sheet.name}」第 {row_no} 行指标“{rule_name}”的「计算公式」",
                )
            )
            parameters = self._parameters_of_sheet(
                rule_code=rule_code,
                rule_name=rule_name,
                identifiers=identifiers,
                sheet=sheet,
                row_no=row_no,
                formula=formula,
                conversion=conversion,
            )
            dependencies = [
                item["source_code"]
                for item in parameters
                if item["source_type"] == "CALC_RESULT" and item.get("source_code")
            ]
            bands = self._bands_of_sheet(
                rule_code=rule_code, rule_name=rule_name, sheet=sheet, row_no=row_no, conversion=conversion
            )
            # 「指标类别」是**枚举列**：内部取值决定结果类型，原文用于展示。
            # 直接读原文会让下面按内部取值查表查不到，报出一个与真实原因无关的错。
            category = self._enum_of(
                row,
                "calculation_type_name",
                definition=self.sheet_definitions[sheet_role],
                sheet=sheet,
                row_no=row_no,
                rule_name=rule_name,
            )
            category_label = _text(row, "calculation_type_name")
            result_type = _CATEGORY_RESULT_TYPE.get(category)
            if result_type is None:
                raise _error(
                    "CALC_CATEGORY_UNSUPPORTED",
                    f"工作表「{sheet.name}」第 {row_no} 行指标“{rule_name}”的「指标类别」是“{category}”，"
                    "当前没有对应的结果类型口径（已支持：" + "、".join(sorted(_CATEGORY_RESULT_TYPE)) + "）",
                    sheet_key=sheet.sheet_key,
                    sheet_name=sheet.name,
                    row_no=row_no,
                    column="指标类别",
                    value=category,
                )
            calculation = {
                "candidate_ref": f"calc:{rule_code}",
                "rule_code": rule_code,
                "rule_name": rule_name,
                "calculation_type": category,
                "formula": formula,
                "result_code": rule_code,
                "result_type": result_type,
                "unit": None,
                "currency": None,
                # 「结果期间」：这条计算**算出来的数**属于哪一期，与字段的取数期间是两件事。
                # 跨期指标（(本期-上期)/上期）不属于任何单一期间，因此必须由业务声明，
                # 系统不按参数推断——推断出来的期间会让下游引用取到错的一期。
                "period_policy": self._enum_of(
                    row,
                    "period_scope",
                    definition=self.sheet_definitions[sheet_role],
                    sheet=sheet,
                    row_no=row_no,
                    rule_name=rule_name,
                ),
                # 计算结果的报表口径：派生事实按它落库，下游引用与报告都读它。
                "statement_scope": self._enum_of(
                    row,
                    "statement_scope",
                    definition=self.sheet_definitions[sheet_role],
                    sheet=sheet,
                    row_no=row_no,
                    rule_name=rule_name,
                ),
                "status": self._config_status_of(
                    row, sheet=sheet, row_no=row_no, rule_name=rule_name, rule_code=rule_code
                ),
                "remark": _text(row, "remark"),
                "parameters": parameters,
                "bands": bands,
                # 未解析的标识符随候选带到发布期：转换期不阻断（让用户看到全貌），
                # 发布期由 compile_calculations 明确阻断。
                "unresolved_identifiers": [
                    name for name in identifiers if name not in {item["parameter_code"] for item in parameters}
                ],
                "depends_on_result_codes": dependencies,
                # 这条计算规则落在哪个**计算节点实例**上：来自计算三表绑定的那个实例。
                # 用能力类型 `CALCULATION` 当实例码会把两个计算节点的规则挂到同一个上。
                "node_code": self._node_code_of(sheet_role),
                "node_type": "CALCULATION",
                # 展示用原文与执行用取值分开：`calculation_type` 是内部取值，
                # `category` 是业务写下的原文（导出与页面看这一份）。
                "category": category_label,
                "description": _text(row, "description"),
                # 风险等级是枚举列：按标签映射回内部取值（「一级风险」与「一级」是同一等级的
                # 不同写法，映射是 profile 里显式声明的，不是猜）。
                "risk_level_text": self._enum_of(
                    row,
                    "risk_level",
                    definition=self.sheet_definitions[sheet_role],
                    sheet=sheet,
                    row_no=row_no,
                    rule_name=rule_name,
                    required=False,
                ),
                # 业务说明类配置事实：不参与执行判定，但预览与导出要看到业务真正写下的
                # 「结果区间定义」与「定性判定」原文。丢了它们，导出再导入会少这两列。
                "bands_text": _text(row, "bands_text"),
                "verification_logic": _text(row, "qualitative"),
                "source": _source_ref(sheet.sheet_key, row_no, cells, texts),
            }
            conversion.calculations.append(calculation)
            conversion.outcomes.append(
                {
                    "row_no": row_no,
                    "disposition": "CANDIDATE",
                    "reason": None,
                    "candidate_refs": [calculation["candidate_ref"]],
                    "notes": [],
                }
            )
        return conversion

    # 计算参数表 → 参数声明

    def _parameter_rows(self) -> dict[str, list[dict[str, Any]]]:
        """`CALC_PARAMETER` 按 rule_code 分组。未绑定该表时返回空字典。

        未绑定不等于「没有参数」：定义表里的公式标识符仍会被解析，
        与空参数表核对后逐条报为未声明——这是配置缺口，不是静默通过。
        """
        if not self._is_bound("CALC_PARAMETER"):
            return {}
        _, _, rows = self.read("CALC_PARAMETER")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            rule_code = _text(row, "rule_code")
            if not rule_code:
                raise _error(
                    "CALC_PARAM_RULE_ID_MISSING",
                    f"工作表「{self._sheet_name_of('CALC_PARAMETER')}」第 {row['row_no']} 行没有填写「规则ID」，"
                    "参数无法关联到计算规则",
                    sheet_key=self._sheet_key_of("CALC_PARAMETER"),
                    sheet_name=self._sheet_name_of("CALC_PARAMETER"),
                    row_no=row["row_no"],
                    column="规则ID",
                )
            self._assert_code_prefix("CALC_PARAMETER", row, "rule_code", code_role="CALC_DEFINITION")
            grouped.setdefault(rule_code, []).append(row)
        return grouped

    def _parameters_of_sheet(
        self,
        *,
        rule_code: str,
        rule_name: str,
        identifiers: list[str],
        sheet: Any,
        row_no: int,
        formula: str,
        conversion: SheetConversion,
    ) -> list[dict[str, Any]]:
        """参数声明**只来自 `CALC_PARAMETER`**，不解析为公式标识符。

        这是「配置就是配置」的落点：业务在参数表里写的取值来源、期间口径、
        单位与币种就是执行时用的那一份，不由系统从公式文本或「数据来源科目」列
        推断出来。公式里的标识符与参数表逐条核对：

        - 公式引用了参数表**没有声明**的标识符 → 明确报错并保留原值；
        - 参数表声明了公式**没有用到**的参数 → 明确报错，不让一条无人使用的配置
          悄悄进入已发布版本。

        两者都报错是刻意的：静默忽略任一侧都会让「配置」与「实际执行」不一致，
        而页面上两边看起来都正常。
        """
        declared = self._parameter_rows().get(rule_code, [])
        parameters: list[dict[str, Any]] = []
        declared_codes: list[str] = []
        code_rows: dict[str, int] = {}
        for row in declared:
            parameter_code = _text(row, "parameter_code")
            if not parameter_code:
                raise _error(
                    "CALC_PARAM_CODE_MISSING",
                    f"工作表「{self._sheet_name_of('CALC_PARAMETER')}」第 {row['row_no']} 行"
                    f"（规则 {rule_code}）没有填写「参数编号」",
                    sheet_key=self._sheet_key_of("CALC_PARAMETER"),
                    sheet_name=self._sheet_name_of("CALC_PARAMETER"),
                    row_no=row["row_no"],
                    column="参数编号",
                )
            self._assert_code_prefix("CALC_PARAMETER", row, "parameter_code")
            if parameter_code in code_rows:
                raise _error(
                    "CALC_PARAMETER_CODE_DUPLICATE",
                    f"计算规则“{rule_code}”的参数编号“{parameter_code}”重复，"
                    f"首次出现在第 {code_rows[parameter_code]} 行",
                    sheet_key=self._sheet_key_of("CALC_PARAMETER"),
                    sheet_name=self._sheet_name_of("CALC_PARAMETER"),
                    row_no=row["row_no"],
                    column="参数编号",
                    value=parameter_code,
                )
            code_rows[parameter_code] = row["row_no"]
            # 「取值来源」是**枚举列**：读中文标签并映射回内部取值，
            # 直接把单元格原文当契约值会在下面立刻被拒。
            # 参数编号要**直接写进公式**，因此必须是合法的公式标识符。
            # 含 `-`、`+` 一类的编号会被词法器切成两段（`P-001` 读成 `P 减 001`），
            # 结果是一句「参数表声明了公式没有用到的参数」——看不出真实原因。
            if not calc.is_identifier(parameter_code):
                raise _error(
                    "CALC_PARAM_CODE_NOT_IDENTIFIER",
                    f"参数编号“{parameter_code}”（规则 {rule_code}）不能作为公式标识符："
                    "公式里的标识符只能由中文、字母、下划线开头，后接中文、字母、数字、下划线组成；"
                    "“-”“+”“*”“/”“(”“)”是运算符，不能出现在编号里",
                    sheet_key=self._sheet_key_of("CALC_PARAMETER"),
                    sheet_name=self._sheet_name_of("CALC_PARAMETER"),
                    row_no=row["row_no"],
                    column="参数编号",
                    value=parameter_code,
                )
            source_type = self._enum_of(
                row,
                "source_type",
                definition=self.sheet_definitions["CALC_PARAMETER"],
                sheet=self._sheet_of("CALC_PARAMETER"),
                row_no=row["row_no"],
                rule_name=parameter_code,
            )
            if source_type not in {"FIELD", "CALC_RESULT", "CONSTANT"}:
                raise _error(
                    "CALC_PARAM_SOURCE_UNSUPPORTED",
                    f"参数“{parameter_code}”（规则 {rule_code}）的「取值来源」是“{source_type}”，"
                    "只支持 FIELD / CALC_RESULT / CONSTANT",
                    sheet_key=self._sheet_key_of("CALC_PARAMETER"),
                    sheet_name=self._sheet_name_of("CALC_PARAMETER"),
                    row_no=row["row_no"],
                    column="取值来源",
                    value=source_type,
                )
            source_code = _text(row, "source_code")
            constant_text = _text(row, "constant_value")
            if source_type == "CONSTANT":
                if not constant_text:
                    raise _error(
                        "CALC_PARAM_CONSTANT_MISSING",
                        f"参数“{parameter_code}”（规则 {rule_code}）的取值来源是常量，但没有填写「常量值」",
                        sheet_key=self._sheet_key_of("CALC_PARAMETER"),
                        sheet_name=self._sheet_name_of("CALC_PARAMETER"),
                        row_no=row["row_no"],
                        column="常量值",
                    )
                try:
                    constant_value: Any = format(Decimal(constant_text), "f")
                except InvalidOperation as exc:
                    raise _error(
                        "CALC_PARAM_CONSTANT_INVALID",
                        f"参数“{parameter_code}”（规则 {rule_code}）的「常量值」不是有效数值：{constant_text}",
                        sheet_key=self._sheet_key_of("CALC_PARAMETER"),
                        sheet_name=self._sheet_name_of("CALC_PARAMETER"),
                        row_no=row["row_no"],
                        column="常量值",
                        value=constant_text,
                    ) from exc
            else:
                constant_value = None
                if not source_code:
                    raise _error(
                        "CALC_PARAM_SOURCE_CODE_MISSING",
                        f"参数“{parameter_code}”（规则 {rule_code}）的取值来源是 {source_type}，"
                        "但没有填写「来源编号」",
                        sheet_key=self._sheet_key_of("CALC_PARAMETER"),
                        sheet_name=self._sheet_name_of("CALC_PARAMETER"),
                        row_no=row["row_no"],
                        column="来源编号",
                    )
                # 引用**存在性**不在这里判：字段编号来自 `03_字段`，由 `node_rulebook`
                # 转换，本模块看不到那份候选；上游结果编号的声明顺序也取决于行序。
                # 两者都由**发布期** `compile_calculations` 对照最终字段集合与计算结果集合
                # 逐条校验（`MANIFEST_CALC_FIELD_MISSING` / `CALC_PARAMETER_RESULT_MISSING`），
                # 那里报出的定位更准。转换期只做结构校验（取值来源、常量值、单位声明）。
            declared_codes.append(parameter_code)
            source_texts = _row_texts(row)
            parameters.append(
                {
                    "parameter_code": parameter_code,
                    "parameter_name": _text(row, "parameter_name") or parameter_code,
                    "source_type": source_type,
                    "source_code": source_code or None,
                    "constant_value": constant_value,
                    # 主体范围是枚举列（当前只有「本主体」一档），同样按标签映射。
                    # **期间与报表口径不在这里**：它们由来源自身声明——FIELD 看字段的
                    # 声明（03），CALC_RESULT 看上游计算定义的结果期间与报表口径（06）。
                    # 参数再声明一遍会形成「两处各说一套」，谁赢没有依据。
                    "entity_scope": self._enum_of(
                        row,
                        "entity_scope",
                        definition=self.sheet_definitions["CALC_PARAMETER"],
                        sheet=self._sheet_of("CALC_PARAMETER"),
                        row_no=row["row_no"],
                        rule_name=parameter_code,
                    ),
                    "required": _optional_yes_no(row, "required") is not False,
                    "unit": _text(row, "unit") or None,
                    "currency": _text(row, "currency") or None,
                    "remark": _text(row, "remark"),
                    "source": _source_ref(
                        self._sheet_key_of("CALC_PARAMETER"), row["row_no"],
                        sorted(source_texts), source_texts,
                    ),
                }
            )
        missing = [name for name in identifiers if name not in set(declared_codes)]
        if missing:
            # 公式引用了参数表没有声明的标识符：保留缺口供预览，发布期明确阻断。
            # 不按标识符文本去推断取值来源——那正是本次要移除的行为。
            self._note_unresolved(
                conversion=conversion,
                sheet=sheet,
                row_no=row_no,
                rule_code=rule_code,
                rule_name=rule_name,
                column="计算公式",
                unresolved=missing,
                expression=formula,
            )
        unused = [code for code in declared_codes if code not in set(identifiers)]
        if unused:
            raise _error(
                "CALC_PARAM_UNUSED",
                f"计算规则“{rule_name}”（{rule_code}）的参数表声明了公式没有用到的参数："
                + "、".join(f"“{code}”" for code in unused),
                sheet_key=self._sheet_key_of("CALC_PARAMETER"),
                sheet_name=self._sheet_name_of("CALC_PARAMETER"),
                row_no=row_no,
                column="参数编号",
            )
        return parameters

    # 计算判定表 → 判定区间

    def _judgement_rows(self) -> dict[str, list[dict[str, Any]]]:
        """`CALC_JUDGEMENT` 按 rule_code 分组。未绑定该表时返回空字典。"""
        if not self._is_bound("CALC_JUDGEMENT"):
            return {}
        _, _, rows = self.read("CALC_JUDGEMENT")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            rule_code = _text(row, "rule_code")
            if not rule_code:
                raise _error(
                    "CALC_JUDGE_RULE_ID_MISSING",
                    f"工作表「{self._sheet_name_of('CALC_JUDGEMENT')}」第 {row['row_no']} 行没有填写「规则ID」，"
                    "判定区间无法关联到计算规则",
                    sheet_key=self._sheet_key_of("CALC_JUDGEMENT"),
                    sheet_name=self._sheet_name_of("CALC_JUDGEMENT"),
                    row_no=row["row_no"],
                    column="规则ID",
                )
            self._assert_code_prefix("CALC_JUDGEMENT", row, "rule_code", code_role="CALC_DEFINITION")
            grouped.setdefault(rule_code, []).append(row)
        return grouped

    def _bands_of_sheet(
        self, *, rule_code: str, rule_name: str, sheet: Any, row_no: int, conversion: SheetConversion
    ) -> list[dict[str, Any]]:
        """判定区间**只来自 `CALC_JUDGEMENT`**，不解析定义表的文本列。

        上下界是可空的：只写下界表示「≥下界」，只写上界表示「≤上界」，
        两者都写是闭/开区间；两者都空表示「任何结果都命中」——这是合法配置，
        但必须由业务显式声明（写一行没有边界的判定），不由系统补一条默认区间。
        """
        declared = self._judgement_rows().get(rule_code, [])
        if not declared:
            return []
        bands: list[dict[str, Any]] = []
        seen_condition_codes: set[str] = set()
        for row in declared:
            condition_code = _text(row, "condition_code")
            if not condition_code:
                raise _error(
                    "CALC_JUDGE_CONDITION_MISSING",
                    f"工作表「{self._sheet_name_of('CALC_JUDGEMENT')}」第 {row['row_no']} 行"
                    f"（规则 {rule_code}）没有填写「条件编号」",
                    sheet_key=self._sheet_key_of("CALC_JUDGEMENT"),
                    sheet_name=self._sheet_name_of("CALC_JUDGEMENT"),
                    row_no=row["row_no"],
                    column="条件编号",
                )
            self._assert_code_prefix("CALC_JUDGEMENT", row, "condition_code")
            if condition_code in seen_condition_codes:
                raise _error(
                    "CALC_CONDITION_CODE_DUPLICATE",
                    f"计算规则“{rule_code}”的判定条件编号“{condition_code}”重复",
                    sheet_key=self._sheet_key_of("CALC_JUDGEMENT"),
                    sheet_name=self._sheet_name_of("CALC_JUDGEMENT"),
                    row_no=row["row_no"],
                    column="条件编号",
                    value=condition_code,
                )
            seen_condition_codes.add(condition_code)
            conclusion = _text(row, "judgement")
            if not conclusion:
                raise _error(
                    "CALC_JUDGE_CONCLUSION_MISSING",
                    f"判定条件“{condition_code}”（规则 {rule_code}）没有填写「判定结论」",
                    sheet_key=self._sheet_key_of("CALC_JUDGEMENT"),
                    sheet_name=self._sheet_name_of("CALC_JUDGEMENT"),
                    row_no=row["row_no"],
                    column="判定结论",
                )
            source_texts = _row_texts(row)
            bands.append(
                {
                    # 判定契约的键名与 08 表的列 key 一致（`condition_code` / `judgement`）：
                    # 另起 `band_code` / `conclusion` 会让发布期按契约校验时全部报「字段缺失」。
                    "condition_code": condition_code,
                    "lower_value": self._decimal_of_cell(row, "lower_value", rule_code=rule_code),
                    # 含不含边界是业务口径（`<1` 与 `≤1` 在临界值上是两个结论），
                    # 空白按**未填写**保留，不代填「含」。缺失由发布期明确阻断。
                    "lower_inclusive": _optional_yes_no(row, "lower_inclusive"),
                    "upper_value": self._decimal_of_cell(row, "upper_value", rule_code=rule_code),
                    "upper_inclusive": _optional_yes_no(row, "upper_inclusive"),
                    # 比较基准是**枚举列**（计算结果 / 计算结果的绝对值）：读标签映射回
                    # 内部取值；空白不代填——拿结果比与拿绝对值比是两个判定，缺失由发布期阻断。
                    "comparison_basis": self._enum_of(
                        row,
                        "comparison_basis",
                        definition=self.sheet_definitions["CALC_JUDGEMENT"],
                        sheet=self._sheet_of("CALC_JUDGEMENT"),
                        row_no=row["row_no"],
                        rule_name=rule_name,
                        required=False,
                    ),
                    "judgement": conclusion,
                    # 判定文本只负责展示，不能决定执行状态。“预警”“一般”“良好”
                    # 在不同业务里含义不同，因此状态必须由 08 表显式给出。
                    "result_status": self._enum_of(
                        row,
                        "result_status",
                        definition=self.sheet_definitions["CALC_JUDGEMENT"],
                        sheet=self._sheet_of("CALC_JUDGEMENT"),
                        row_no=row["row_no"],
                        rule_name=rule_name,
                    ),
                    "risk_level": self._enum_of(
                        row,
                        "risk_level",
                        definition=self.sheet_definitions["CALC_JUDGEMENT"],
                        sheet=self._sheet_of("CALC_JUDGEMENT"),
                        row_no=row["row_no"],
                        rule_name=rule_name,
                        required=False,
                    ),
                    "action": _text(row, "action") or None,
                    "remark": _text(row, "remark"),
                    "source": _source_ref(
                        self._sheet_key_of("CALC_JUDGEMENT"), row["row_no"],
                        sorted(source_texts), source_texts,
                    ),
                }
            )
        return bands

    def _decimal_of_cell(self, row: dict[str, Any], key: str, *, rule_code: str) -> str | None:
        """判定边界值的十进制字符串；空表示该侧无界，非法值明确报错。"""
        text = _text(row, key)
        if not text:
            return None
        try:
            return format(Decimal(text), "f")
        except InvalidOperation as exc:
            raise _error(
                "CALC_JUDGE_BOUND_INVALID",
                f"规则 {rule_code} 的判定边界「{key}」不是有效数值：{text}",
                sheet_key=self._sheet_key_of("CALC_JUDGEMENT"),
                sheet_name=self._sheet_name_of("CALC_JUDGEMENT"),
                row_no=row["row_no"],
                column=key,
                value=text,
            ) from exc

    def _is_bound(self, sheet_role: str) -> bool:
        return self._bound(sheet_role) is not None

    def _node_code_of(self, sheet_role: str) -> str:
        """这张 Sheet 绑定的**节点实例码**。

        规则在已发布配置里的归属必须是用户配置的实例码，不能是能力类型：
        同一场景可以有两个同类节点（两张报表各一个计算节点），能力类型不足以定位。
        实例码来自绑定结果（`import_sheet.bound_node_code`）；缺失时明确报错，
        静默退回能力类型会让规则挂到用户并没有建立的节点上。
        """
        for sheet in self.ir.sheets:
            binding = self.bindings.get(sheet.sheet_key) or {}
            # 与 _bound 同一口径：只看绑定与明确排除，不看隐藏。
            if binding.get("excluded"):
                continue
            if binding.get("role") != sheet_role:
                continue
            node_code = binding.get("node_code")
            if not isinstance(node_code, str) or not node_code:
                raise BusinessError(
                    "SHEET_NODE_REQUIRED",
                    f"工作表「{sheet.name}」没有绑定业务节点，无法确定它上面的规则归属哪个节点实例",
                    details={"sheet_key": sheet.sheet_key, "sheet_name": sheet.name, "role": sheet_role},
                )
            return node_code
        raise BusinessError(
            "SHEET_ROLE_MISSING",
            f"当前导入没有绑定「{sheet_role}」对应的 Sheet",
            details={"sheet_role": sheet_role},
        )

    def _sheet_of(self, sheet_role: str) -> Any:
        bound = self._bound(sheet_role)
        if bound is None:
            raise BusinessError(
                "SHEET_ROLE_MISSING",
                f"当前导入没有绑定「{sheet_role}」对应的 Sheet",
                details={"sheet_role": sheet_role},
            )
        return bound[0]

    def _sheet_key_of(self, sheet_role: str) -> str:
        return self._sheet_of(sheet_role).sheet_key

    def _sheet_name_of(self, sheet_role: str) -> str:
        return self._sheet_of(sheet_role).name

    # 公式 → 参数声明

    def _note_unresolved(
        self,
        *,
        conversion: SheetConversion,
        sheet: Any,
        row_no: int,
        rule_code: str,
        rule_name: str,
        column: str,
        unresolved: list[str],
        expression: str,
    ) -> None:
        """把「公式里引用了未声明的参数」记成导入提示。

        这里**不抛错**：抛错会让整份规则库一条都导不进来，用户看不到其余内容，
        也无从知道到底缺哪些参数。缺失依赖在**发布期**由编译明确阻断
        （见 manifest.compile_calculations），因此它不会被静默带进已执行配置。
        参数缺失不能靠文本答案修复：参数还必须声明取值来源、单位等结构化属性。
        用户应修正计算参数表或公式后重新转换。
        """
        conversion.notes.append(
            {
                "code": "CALC_IDENTIFIER_UNRESOLVED",
                "message": (
                    f"计算规则“{rule_name}”（{rule_code}）的{column}“{expression}”引用了"
                    "未声明的参数：" + "、".join(f"“{name}”" for name in unresolved)
                    + "。请在 07_计算参数补齐编号及取值来源，或修正 06_计算定义中的公式，"
                    "然后重新上传并转换。补齐前不能发布"
                ),
                "sheet_key": sheet.sheet_key,
                "sheet_name": sheet.name,
                "row_no": row_no,
                "column": column,
                "value": expression,
                "unresolved": list(unresolved),
            }
        )

    def _config_status_of(
        self, row: dict[str, Any], *, sheet: Any, row_no: int, rule_name: str, rule_code: str
    ) -> str:
        """该行「是否生效」→ 内部配置状态（已明确 / 停用）。

        base Excel 里只有「是否生效」一列（是 / 否），**没有**「状态」列：业务在表里
        不表达「系统配没配好」，只表达「这一行参不参与审核」。因此这里只会得到
        CLEAR 或 DISABLED，不再产生 PENDING。

        未填写时明确报错，不代填默认值：默认「生效」会让一行没填状态的规则静默进入执行。
        """
        value = _optional_yes_no(row, "enabled")
        if value is True:
            return "CLEAR"
        if value is False:
            return "DISABLED"
        raise _error(
            "CALC_ENABLED_REQUIRED",
            f"工作表「{sheet.name}」第 {row_no} 行指标“{rule_name}”的「是否生效」没有填写："
            "请明确填写“是”或“否”，系统不代填默认值",
            sheet_key=sheet.sheet_key,
            sheet_name=sheet.name,
            row_no=row_no,
            column="是否生效",
            value=_text(row, "enabled"),
        )

    # 05/06 计算 → 计算规则候选（见 convert_calculations）


def build_convert_bundle(
    ir: WorkbookIR,
    bindings: dict[str, dict[str, Any]],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """把一份规则库确定性转换成候选集合。

    返回结构与 `ProposalBundle` 兼容的字典，并附带：

    - `node_notes`：必须留给业务处理的待确认事项（成立条件推断、章节未结构化、
      检测项无映射、科目缺单位币种等）；
    - `coverage`：逐业务行的处置对账；
    - `calculations`：编译前的计算规则候选（参数与判定区间**来自各自 Sheet**）。

    **只转换已绑定的 Sheet**：未绑定或被排除的 Sheet 不进入转换；隐藏属性不改变绑定。
    统一模板覆盖系统能力全集，一个场景只用其中一部分，因此「没绑定某张表」
    是配置事实而不是错误；用到却读不到才由各转换函数明确报错。

    本函数不调用任何模型，不读取向量索引。
    """
    converter = FinancialReportConverter(ir, bindings, profile)
    conversions: list[SheetConversion] = []

    bound_nodes = {
        role: converter._node_code_of(role)
        for role in ("CALC_DEFINITION", "CALC_PARAMETER", "CALC_JUDGEMENT")
        if converter._is_bound(role)
    }
    if len(set(bound_nodes.values())) > 1:
        raise BusinessError(
            "CALC_SHEET_NODE_MISMATCH",
            "计算定义、计算参数与计算判定必须绑定同一个计算节点",
            details={"bound_nodes": bound_nodes},
        )

    # 本模块只负责**计算三表**。其余 Sheet（01_材料位 / 02_文件处理规则 / 03_字段 /
    # 04_文件内部校验规则 / 05_文件交叉校验规则 / 09_风险规则 / 10_报告模板）由
    # `node_rulebook` 负责，两边按**已绑定的 Sheet 角色**分派、结果合并
    # （见 llm_service.convert_workbook_with_notes）。
    #
    # 没绑定计算三表时返回 None：一个场景只用模板的一部分，「没绑定」是配置事实。
    calculations = converter.convert_calculations("CALC_DEFINITION")
    for conversion in (calculations,):
        if conversion is not None:
            conversions.append(conversion)

    # 07_计算参数 / 08_计算判定 是**计算规则的输入**，不各自产出候选，但它们的业务行
    # 同样必须逐行对账：这两张表被读取、行内容参与了编译，若不给覆盖条目，
    # 合并后的覆盖报告会显示「业务行 0 / 已全部对账」——与「表头行之后每条非空业务行
    # 都必须有明确处置，不得静默丢弃」相反，用户填了 20 行的参数表看起来一行都没进。
    definition_codes = {
        str(item.get("rule_code")) for item in (calculations.calculations if calculations else [])
    }
    disabled_definition_codes = {
        str(item["source_id"])
        for item in (calculations.disabled_rows if calculations else [])
        if item.get("source_id")
    }
    for role in ("CALC_PARAMETER", "CALC_JUDGEMENT"):
        conversion = converter.input_sheet_coverage(
            role, definition_codes=definition_codes, disabled_definition_codes=disabled_definition_codes
        )
        if conversion is not None:
            conversions.append(conversion)

    _assert_no_disabled_dependency(conversions)

    sheets = [
        {
            "sheet_key": conversion.sheet_key,
            "suggested_role": _role_of(conversion.sheet_key, ir, bindings),
            "column_mapping": {},
            "materials": conversion.materials,
            "fields": conversion.fields,
            "rules": conversion.rules,
            "report_sections": conversion.report_sections,
            "disabled_rows": conversion.disabled_rows,
            "questions": [],
        }
        for conversion in conversions
    ]
    coverage_sheets = [
        {
            "sheet_key": conversion.sheet_key,
            "sheet_name": conversion.sheet_name,
            "business_rows": len(conversion.outcomes),
            "rows": [
                {
                    "row_no": outcome["row_no"],
                    "disposition": outcome["disposition"],
                    "reason": outcome.get("reason"),
                    "candidate_count": len(outcome.get("candidate_refs") or []),
                    "question_count": 0,
                    "merged_into": None,
                    "notes": list(outcome.get("notes") or []),
                }
                for outcome in conversion.outcomes
            ],
        }
        for conversion in conversions
    ]
    totals = coverage_totals(coverage_sheets)
    notes = [note for conversion in conversions for note in conversion.notes]
    all_calculations = [
        calculation
        for conversion in conversions
        for calculation in conversion.calculations
    ]
    # 指纹只覆盖**本模块**产出的候选：其余 Sheet 由 `node_rulebook` 转换，
    # 两边的指纹在 `llm_service._merge_bundles` 里合并。
    bundle_hash = hashlib.sha256(
        json.dumps(
            {
                "calculations": [item["rule_code"] for item in all_calculations],
                "rules": [item["rule_code"] for conversion in conversions for item in conversion.rules],
                "disabled_rows": [conversion.disabled_rows for conversion in conversions],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "sheets": sheets,
        "coverage": {"sheets": coverage_sheets, "totals": totals},
        "node_notes": notes,
        "calculations": all_calculations,
        "bundle_hash": bundle_hash,
    }


def _role_of(sheet_key: str, ir: WorkbookIR, bindings: dict[str, dict[str, Any]]) -> str:
    """这张 Sheet 在候选集合里的用途。

    只认**用户确认的绑定**（`import_sheet.bound_role`）。不退回表头建议：
    建议只用于页面预选，把它写进候选等于让一条未经确认的推断进入已发布配置。
    未绑定的 Sheet 不会走到这里（它们不参与转换），因此缺绑定是内部矛盾，明确报错。
    """
    binding = bindings.get(sheet_key) or {}
    role = binding.get("role")
    if isinstance(role, str) and role:
        return role
    sheet = next((item for item in ir.sheets if item.sheet_key == sheet_key), None)
    raise BusinessError(
        "SHEET_ROLE_MISSING",
        f"工作表「{sheet.name if sheet else sheet_key}」没有确认的用途绑定，不能进入候选集合",
        status_code=500,
        details={"sheet_key": sheet_key},
    )


def _assert_no_disabled_dependency(conversions: list[SheetConversion]) -> None:
    """停用对象被引用时必须明确报出，不为了发布自动停用一条链路。

    与合同审核路径同一口径：停用的上游不会静默变成「不适用」，也不会被自动启用。
    """
    disabled_fields = {
        field["source_id"]
        for conversion in conversions
        for field in conversion.fields
        if field.get("config_status") == "DISABLED"
    }
    if not disabled_fields:
        return
    for conversion in conversions:
        for calculation in conversion.calculations:
            for parameter in calculation["parameters"]:
                if parameter["source_type"] == "FIELD" and parameter["source_code"] in disabled_fields:
                    raise BusinessError(
                        "CALC_DEPENDENCY_DISABLED",
                        f"计算规则“{calculation['rule_code']}”引用的科目"
                        f"“{parameter['parameter_name']}”（{parameter['source_code']}）已停用，"
                        "依赖断裂。请启用该科目或移除该依赖",
                        details={
                            "rule_code": calculation["rule_code"],
                            "parameter_code": parameter["parameter_code"],
                            "source_code": parameter["source_code"],
                        },
                    )
