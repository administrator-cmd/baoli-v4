import hashlib
import json
import logging
from typing import Any

from openpyxl.utils.cell import coordinate_from_string
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import get_settings
from ..domain.manifest import (
    DATE_COMPARISON_LABELS,
    DECIMAL_COMPARISON_LABELS,
    DECIMAL_COMPARE_OPERATORS,
    DATE_COMPARE_OPERATORS,
    DOCUMENT_ARTIFACT_TYPES,
    detect_unsupported_capability_for_operator,
    validate_rule_parameters,
)
from ..domain.proposals import (
    ApplicabilityProposal,
    AnswerResolution,
    BusinessQuestion,
    CoverageReport,
    CoverageRow,
    CoverageSheet,
    ProposalBundle,
    QuestionOption,
    RuleInputProposal,
    SheetProposal,
    UnsupportedCapabilityCode,
    coverage_totals,
)
from ..domain.workbook import WorkbookIR, WorkbookRow, workbook_source_index
from ..domain import node_rulebook
from ..domain import unified_profile
from ..errors import BusinessError
from ..integrations.llm.gateway import call_json


logger = logging.getLogger(__name__)

# 规则库转换完全由代码确定性完成，不经过远程大模型。
# base Excel 是系统唯一的模板，转换**按 Sheet 独立进行、结果合并**：每张表由负责它的
# 解析器转换（01/02/03/04/05/09/10 走 node_rulebook，06/07/08 计算三表走
# financial_report），绑定里出现哪几类表就转换哪几类。没有「业务族」这一层，
# 也不存在「按族二选一、另一族的表被静默丢掉」这条分支。


# 规则列的填写契约。这些取值由后端解析确定性事实并校验，模型只负责把自然语言要求
# 落到明确的算子与参数上，不能自行发明阈值、方向或字段。
RULE_CONTRACT_PROMPT = f"""规则列填写契约：
1. 后端已按表头原文给出确定性列映射 deterministic_column_mapping（内部列 key -> 表头单元格地址），
   你必须原样放进 column_mapping，不得改写；只有某列存在多个同名表头时后端才参考你为该列选择的地址。
   “检查方式/检查项目”列只使用模板下拉里的中文取值；审核节点由工作表用途决定，不需要你选择。
2. 日期比较的 parameters 只放 "operator"，取值 {sorted(DATE_COMPARE_OPERATORS)}。
   数值比较的 parameters 放 "operator"，取值 {sorted(DECIMAL_COMPARE_OPERATORS)}；仅当 operator=ABS_DIFF_LTE 时再放数值参数 "threshold"（允许的绝对差额，必须 >= 0）。
3. 比较方向与 threshold 只能来自“审核要求”原文，禁止采用任何默认阈值或默认方向，也禁止在“审核要求”里书写参数名或 Excel 公式。
   方向或差额无法从审核要求确定时，把 parameters 留空（或只放能确定的部分）：不要填默认值，也不要自己生成业务问题，
   后端会针对缺少的参数生成需要业务补充的问题。
4. “条款审核”（LLM_VALIDATE）可以只填左侧材料与左侧字段，右侧留空，此时 inputs 只含一个字段。
   字段一致、字段不同、日期比较、数值比较必须恰好有两个字段，且 inputs 按“左侧字段在前、右侧字段在后”的顺序给出。
   后端会按“左侧材料 / 左侧字段 / 右侧材料 / 右侧字段”四列的单元格原文重新建立规则输入：
   你只需要原样复制这四列文本，不得改写材料名或字段名，也不得自行更换左右关联。
   “条款审核”的 parameters 不需要你填写：后端会用“审核要求”原文作为判断依据，你不要自己编造参数。
5. 后端会以单元格原文为准重新读取业务说明、抽取说明、审核要求、检查要求、内容要求、适用条件、字段类型、检查方式、风险等级、来源节点和是否必填；
   你仍然要原样复制这些单元格文本，不得改写、不得省略、不得补写业务含义。
6. 以下要求当前不支持，遇到时不要生成可执行规则，改为填写 unsupported_capability
   （code 只能取 {list(UnsupportedCapabilityCode.__args__)}，detail 用中文写清这条规则要求哪种取数/汇总方式）：
   全部发票求和、多份同类文件合计、跨期间汇总、多合同自动分组等汇总取数；与固定常量比较（例如“合同金额等于 100000 元”）；需要查询系统外数据。
   禁止取第一份或最后一份文件代替，禁止让大模型估算合计，禁止把常量写成一个虚构字段。
7. 材料质量工作表的 FILE_QUALITY_CHECK 的 parameters 只放 "artifact_type"，取值 {sorted(DOCUMENT_ARTIFACT_TYPES)}；检查要求原文仍然放进 requirement。
8. questions 不要填写 resolve 字段，它只由后端在缺少必要参数时生成。
9. 材料质量与风险工作表要按“材料名称”“来源审核规则”列真实填写 material_refs 与 source_rule_ref，引用不唯一时系统会报错而不是替你选择。
   风险疑点工作表的风险规则算子固定为 RISK_TRIGGER，不需要你选择。
10. 一行只转换一条业务记录：空行、只填了分组标题的行（例如“主合同字段”“通用校验规则”“轻量预校验子集”）、
    以及整行只是说明文字的行，一律不得生成材料、字段、规则或报告章节候选；不要为了凑数把它们转成信息残缺的记录。
11. 同一份材料内的字段名称必须唯一；不同材料之间允许出现同名字段（例如“协议编号”“签署日期”）。
    字段引用一律按“材料名称 + 字段名称”给出，不得只给字段名称，也不得凭字段名猜测它属于哪份材料。"""


SYSTEM_PROMPT = """你是合同规则 Excel 结构转换器。只根据请求中的结构化工作表数据返回 JSON。
禁止生成数据库 ID 或内部 key；candidate_ref 仅用于当前响应内的临时关联，后端会依据已验证的工作表来源生成正式内部引用。candidate_ref 可以复用 Excel 来源编码或构造当前导入内的临时引用。
材料引用使用材料清单中的中文材料名称；字段引用必须同时给出所属材料的名称和字段名称，避免不同材料下的同名字段混淆。禁止引用请求中不存在的 sheet_key、真实行号和单元格地址。禁止生成允许目录之外的算子。
审核节点由工作表用途决定，不需要你选择；你只需根据“审核要求”等自然语言列给出算子与参数。
无法确定的业务含义必须放入 questions，不得猜测。每个问题必须给出 target_type、target_ref、property 和选项对象；property 是要裁决的候选属性。
返回内容必须严格匹配 response_contract，不能包含 Markdown。

""" + RULE_CONTRACT_PROMPT



def _response_contract() -> dict[str, Any]:
    return SheetProposal.model_json_schema()


def _llm_row_payload(row: WorkbookRow) -> dict[str, Any]:
    # 转换范围是完整 IR 的业务行，因此这里给的是原文单元格值，不带预览截断。
    return {"row_no": row.row_no, "cells": row.cells}


def _call_llm(payload: dict[str, Any], role: str, version: str) -> SheetProposal:
    """把一次工作表分块交给模型，拿回已校验的候选集合。"""
    raw = _normalize_sheet_payload(call_json(payload, SYSTEM_PROMPT), role, version)
    try:
        return SheetProposal.model_validate(raw)
    except ValidationError as exc:
        logger.warning("远程大模型规则转换响应不符合 DTO error_type=%s", type(exc).__name__)
        raise BusinessError(
            "LLM_CONTRACT_INVALID",
            "远程大模型返回内容不符合规则转换契约",
            status_code=502,
        ) from exc


# 工作表用途决定规则挂在哪个审核节点；算子决定比较方式。两者都不要求业务用户填写。
ROLE_NODE = {
    "MATERIAL_LIST": "MATERIAL_CHECK",
    "MATERIAL_QUALITY": None,
    "FIELD_EXTRACTION": "FIELD_EXTRACTION",
    "INTERNAL_VALIDATE": "INTERNAL_VALIDATE",
    "CROSS_VALIDATE": "CROSS_VALIDATE",
    "RISK": "RISK_RATING",
    "REPORT_TEMPLATE": "REPORT_OUTPUT",
}
QUALITY_OPERATOR_NODE = {
    "MATERIAL_EXISTS": "MATERIAL_CHECK",
    "FILE_QUALITY_CHECK": "DOCUMENT_PREPARE",
}


def _normalize_sheet_payload(raw: Any, role: str, version: str | None = None) -> Any:
    """把业务模板里的中文下拉结果规范化为契约枚举，并按工作表用途确定审核节点。

    模板对业务用户只暴露中文下拉（例如“金额数值比较”“条款审核”“所引用材料均已上传”）；
    这里把标签换回契约值，同时对既有的英文契约值原样保留，旧模板不需要迁移。
    枚举标签一律取该列自己的下拉表（profiles 的单列标签）：模型不得套用别的列的下拉取值。
    """
    if not isinstance(raw, dict):
        return raw
    profile_role = unified_profile.sheet_index(unified_profile.load_unified_profile()).get(role) if role else None
    fields = raw.get("fields")
    if isinstance(fields, list):
        value_type_labels = _column_value_labels(profile_role, "value_type") if profile_role else {}
        for item in fields:
            if isinstance(item, dict) and isinstance(item.get("value_type"), str):
                item["value_type"] = value_type_labels.get(item["value_type"].strip(), item["value_type"])
    rules = raw.get("rules")
    if isinstance(rules, list):
        operator_labels = _column_value_labels(profile_role, "operator") if profile_role else {}
        applicability_labels = _column_value_labels(profile_role, "applicability") if profile_role else {}
        for item in rules:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("operator"), str):
                item["operator"] = operator_labels.get(item["operator"].strip(), item["operator"])
            node = ROLE_NODE.get(role)
            if node is None:
                node = QUALITY_OPERATOR_NODE.get(str(item.get("operator")), item.get("node"))
            if node:
                item["node"] = node
            item["applicability"] = _normalize_applicability(item.get("applicability"), applicability_labels)
    return raw


def _normalize_applicability(value: Any, labels: dict[str, str]) -> dict[str, str] | None:
    """适用条件留空表示始终适用；其他取值必须落在已支持的两种条件内。

    列存在时后端会以单元格原文为准重新读取，这里只处理旧模板或模型自行给出契约值的情况。
    """
    if value in (None, ""):
        return None
    if isinstance(value, str):
        kind = labels.get(value.strip(), value.strip())
    elif isinstance(value, dict) and isinstance(value.get("kind"), str):
        kind = labels.get(value["kind"].strip(), value["kind"].strip())
    else:
        raise BusinessError("RULE_APPLICABILITY_UNSUPPORTED", "规则的适用条件填写格式不受支持，请使用模板下拉中的取值")
    if kind not in {"ALWAYS", "MATERIALS_UPLOADED"}:
        raise BusinessError(
            "RULE_APPLICABILITY_UNSUPPORTED",
            "规则的适用条件当前不受支持，只支持“始终适用”和“所引用材料均已上传”，留空表示始终适用",
            details={"kind": kind},
        )
    return {"kind": kind}



def _validate_source(
    proposal: SheetProposal,
    source_index: dict[str, dict[str, Any]],
    *,
    expected_sheet_key: str,
    header_row: int,
    allowed_rows: dict[int, set[str]],
) -> None:
    if proposal.sheet_key != expected_sheet_key:
        raise BusinessError("LLM_SOURCE_REF_INVALID", "大模型返回的工作表与当前分块不一致")
    if proposal.sheet_key not in source_index:
        raise BusinessError("LLM_SOURCE_REF_INVALID", "大模型引用了不存在的工作表")
    source_objects = [*proposal.materials, *proposal.fields, *proposal.rules, *proposal.report_sections, *proposal.questions]
    for item in source_objects:
        source = item.source
        if source.sheet_key != proposal.sheet_key:
            raise BusinessError("LLM_SOURCE_REF_INVALID", "大模型引用了错误的工作表", details={"target": getattr(item, "candidate_ref", getattr(item, "question_ref", ""))})
        rows = source_index[source.sheet_key]["rows"]
        if source.row_no not in rows or any(cell not in rows[source.row_no] for cell in source.cells):
            raise BusinessError(
                "LLM_SOURCE_REF_INVALID",
                "大模型引用了不存在的行或单元格",
                details={"sheet_key": source.sheet_key, "row_no": source.row_no, "cells": source.cells},
            )
        if source.row_no not in allowed_rows or any(cell not in allowed_rows[source.row_no] for cell in source.cells):
            raise BusinessError(
                "LLM_SOURCE_REF_INVALID",
                "大模型引用了当前分块未提供的行或单元格",
                details={"sheet_key": source.sheet_key, "row_no": source.row_no, "cells": source.cells},
            )
        if source.row_no == header_row and not hasattr(item, "question_ref"):
            raise BusinessError("LLM_SOURCE_REF_INVALID", "业务候选不能把表头行作为来源", details={"sheet_key": source.sheet_key, "row_no": source.row_no})


def _normalize_header_label(text: Any) -> str:
    """表头文本归一化：去掉空白并统一大小写，供 profile 列名/别名匹配。"""
    return "".join(str(text).split()).casefold()


def _profile_column_labels(profile_role: dict[str, Any]) -> dict[str, set[str]]:
    """内部列 key -> 该列的归一化名称与别名集合。"""
    labels: dict[str, set[str]] = {}
    for column in profile_role["columns"]:
        names = {_normalize_header_label(column["name"])}
        names.update(_normalize_header_label(alias) for alias in column.get("aliases") or [])
        labels[column["key"]] = names
    return labels


def _header_hits(
    profile_role: dict[str, Any],
    header_values: dict[str, Any],
) -> dict[str, set[str]]:
    """表头单元格地址 -> 匹配到的内部列 key 集合。"""
    labels = _profile_column_labels(profile_role)
    hits: dict[str, set[str]] = {}
    for address, value in header_values.items():
        text = _normalize_header_label(value)
        if not text:
            continue
        hits[address] = {key for key, names in labels.items() if text in names}
    return hits


def _deterministic_column_mapping(
    profile_role: dict[str, Any],
    header_values: dict[str, Any],
    *,
    sheet_name: str,
    header_row: int,
) -> tuple[dict[str, str], set[str]]:
    """按 profile 的表头名称/别名确定性建立 内部列 key -> 表头单元格地址。

    重名（一个内部列命中多个表头）、缺列（必填列命中不到）和未知列（表头命中不到任何列）
    都在这里明确报错，并带上工作表名、表头行号和列名，绝不由模型猜出表头语义。
    一个表头命中多个内部列时无法确定性判定，收集为待模型选择的歧义列。
    """
    hits_by_cell = _header_hits(profile_role, header_values)
    cells_by_key: dict[str, list[str]] = {}
    anomalies: list[dict[str, Any]] = []
    for address, hits in sorted(hits_by_cell.items()):
        if len(hits) == 1:
            cells_by_key.setdefault(next(iter(hits)), []).append(address)
        elif not hits:
            anomalies.append(
                {"kind": "UNKNOWN_COLUMN", "cell": address, "header": str(header_values[address])}
            )
        else:
            anomalies.append(
                {
                    "kind": "AMBIGUOUS_HEADER",
                    "cell": address,
                    "header": str(header_values[address]),
                    "columns": sorted(hits),
                }
            )
    mapping: dict[str, str] = {}
    ambiguous_keys: set[str] = set()
    for key, addresses in sorted(cells_by_key.items()):
        if len(addresses) == 1:
            mapping[key] = addresses[0]
        else:
            ambiguous_keys.add(key)
    for column in profile_role["columns"]:
        key = column["key"]
        if key in mapping or key in ambiguous_keys or not column["required"]:
            continue
        anomalies.append({"kind": "REQUIRED_COLUMN_MISSING", "column": key, "name": column["name"]})
    if anomalies:
        # 列名与地址同时写进 error message：details 会被错误处理器按安全键裁剪，消息不会被裁掉。
        summary = "；".join(_column_anomaly_text(item) for item in anomalies)
        raise BusinessError(
            "LLM_COLUMN_MAPPING_INVALID",
            f"工作表「{sheet_name}」第 {header_row} 行的表头无法与模板列一一对应（{summary}），请修正表头后重新上传",
            details={"sheet_name": sheet_name, "header_row": header_row, "columns": anomalies},
        )
    return mapping, ambiguous_keys


def _column_anomaly_text(anomaly: dict[str, Any]) -> str:
    kind = anomaly.get("kind")
    if kind == "UNKNOWN_COLUMN":
        return f"未知列 {anomaly['cell']}「{anomaly['header']}」"
    if kind == "AMBIGUOUS_HEADER":
        return f"表头 {anomaly['cell']}「{anomaly['header']}」同时匹配多列"
    if kind == "REQUIRED_COLUMN_MISSING":
        return f"缺少必需列「{anomaly['name']}」"
    return str(kind)


def _merge_column_mapping(
    proposal: SheetProposal,
    *,
    profile_role: dict[str, Any],
    deterministic: dict[str, str],
    ambiguous_keys: set[str],
    header_values: dict[str, Any],
    sheet_name: str,
    header_row: int,
) -> list[str]:
    """以确定性映射为准合并模型返回的列映射。

    确定性命中的列一律覆盖模型结果，模型给出的冲突映射不被静默接受，而是记进返回的说明；
    只有确定性无法判定的歧义列才由模型在候选表头里选定，选定值不在候选中就明确报错。
    """
    labels = _profile_column_labels(profile_role)
    column_names = {column["key"]: column["name"] for column in profile_role["columns"]}
    ambiguous_cells: dict[str, set[str]] = {}
    for address, value in header_values.items():
        text = _normalize_header_label(value)
        if not text:
            continue
        hits = {key for key, names in labels.items() if text in names}
        for key in hits & ambiguous_keys:
            ambiguous_cells.setdefault(key, set()).add(address)
    final = dict(deterministic)
    for key in sorted(ambiguous_keys):
        chosen = proposal.column_mapping.get(key)
        if chosen in ambiguous_cells.get(key, set()):
            final[key] = chosen
            continue
        raise BusinessError(
            "LLM_COLUMN_MAPPING_AMBIGUOUS",
            f"工作表「{sheet_name}」第 {header_row} 行有多个表头都可能是「{column_names.get(key, key)}」列，"
            "不能在多个同名列之间自动选择，请删除多余的同名列后重新上传",
            details={
                "sheet_name": sheet_name,
                "header_row": header_row,
                "column": key,
                "candidates": sorted(ambiguous_cells.get(key, set())),
                "chosen": chosen,
            },
        )
    conflicts: list[str] = []
    for key, address in sorted(proposal.column_mapping.items()):
        expected = final.get(key)
        if expected is None:
            if address:
                conflicts.append(
                    f"模型把「{column_names.get(key, key)}」列映射到 {address}，但表头里没有该列，已按未映射处理"
                )
            continue
        if address and address != expected:
            conflicts.append(
                f"「{column_names.get(key, key)}」列的模型映射 {address} 与表头原文不一致，已按 {expected} 读取"
            )
    proposal.column_mapping = final
    return conflicts


def _validate_candidate_source_ids(
    proposal: SheetProposal,
    *,
    source_index: dict[str, dict[str, Any]],
    role: str,
    version: str,
) -> None:
    definition = unified_profile.sheet_index(unified_profile.load_unified_profile()).get(role)
    if definition is None or not any(column["key"] == "source_id" for column in definition["columns"]):
        # 业务模板不要求填写内部编码；候选来源由来源工作表、行号和单元格承担。
        return
    source_id_header = proposal.column_mapping.get("source_id")
    candidates = [*proposal.materials, *proposal.fields, *proposal.rules]
    if not candidates:
        return
    if not source_id_header:
        raise BusinessError("LLM_SOURCE_ID_MAPPING_MISSING", "业务候选缺少来源编码列映射")
    column, _ = coordinate_from_string(source_id_header)
    values = source_index[proposal.sheet_key]["values"]
    for item in candidates:
        source_id_cell = f"{column}{item.source.row_no}"
        actual_value = values.get(item.source.row_no, {}).get(source_id_cell)
        actual_source_id = str(actual_value).strip() if actual_value not in (None, "") else ""
        if source_id_cell not in item.source.cells or not actual_source_id or item.source_id != actual_source_id:
            raise BusinessError(
                "LLM_SOURCE_ID_INVALID",
                "大模型返回的来源编码与当前工作表实际单元格不一致",
                details={"sheet_key": proposal.sheet_key, "row_no": item.source.row_no},
            )


def _mapped_cell(proposal: SheetProposal, values: dict[int, dict[str, Any]], row_no: int, key: str) -> tuple[str | None, str | None]:
    """返回（单元格地址，单元格文本）。列未映射时返回 (None, None)，空单元格返回 (地址, None)。"""
    header = proposal.column_mapping.get(key)
    if not header:
        return None, None
    column, _ = coordinate_from_string(header)
    address = f"{column}{row_no}"
    raw = values.get(row_no, {}).get(address)
    if raw in (None, ""):
        return address, None
    return address, str(raw).strip()


def _column_value_labels(profile_role: dict[str, Any], key: str) -> dict[str, str]:
    """该列自己的下拉标签 -> 契约值。跨列取值不算合法，避免把别的列的下拉值混用进来。"""
    column = next((item for item in profile_role["columns"] if item["key"] == key), None)
    if column is None:
        return {}
    labels = {str(label).strip(): canonical for canonical, label in (column.get("value_labels") or {}).items()}
    for canonical in column.get("allowed_values") or []:
        labels.setdefault(str(canonical).strip(), str(canonical))
    return labels


# 左右输入必须一对一的四个明确列。只有模板声明了这四列，才由单元格原文重建规则输入；
# 遗留格式（contract_review_v1 的内部校验只有材料编码一列）沿用模型给出的输入。
EXPLICIT_INPUT_COLUMNS = ("left_material_ref", "left_field_ref", "right_material_ref", "right_field_ref")
# 单元格里出现这些分隔符说明业务想表达多个字段/材料之间的关系，当前不支持。
# 注意：07_报告模板的「展示字段」允许一格写多个引用，该列不经过这里的校验。
MULTI_INPUT_SEPARATORS = (",", "，", "、", ";", "；", "/", "|", "｜", "+")


def _split_reference_list(text: str | None) -> list[str]:
    """按多引用分隔符拆分列表列（07_报告模板的「展示字段」）。

    该列的设计就是一个单元格写多个字段引用，因此不能用 _reject_multi_input 报错；
    只做拆分、去空白与去空项，引用是否能解析仍由后续引用校验明确判定。
    """
    if text is None:
        return []
    parts = [text]
    for separator in MULTI_INPUT_SEPARATORS:
        parts = [piece for part in parts for piece in part.split(separator)]
    return [piece.strip() for piece in parts if piece.strip()]


def _has_explicit_input_columns(profile_role: dict[str, Any]) -> bool:
    declared = {column["key"] for column in profile_role["columns"]}
    return all(key in declared for key in EXPLICIT_INPUT_COLUMNS)


def _reject_multi_input(
    text: str,
    *,
    sheet_key: str,
    sheet_name: str,
    row_no: int,
    address: str,
    label: str,
) -> str:
    """一格多个引用一律明确报错，绝不只取第一个引用继续转换。"""
    separator = next((item for item in MULTI_INPUT_SEPARATORS if item in text), None)
    if separator is None:
        return text
    raise BusinessError(
        "RULE_MULTI_INPUT_UNSUPPORTED",
        f"工作表「{sheet_name}」第 {row_no} 行 {address} 的「{label}」填了多个字段（出现分隔符“{separator}”），"
        "当前只支持一个左输入对一个右输入，请拆成多条 1 对 1 规则后重新上传",
        details={
            "sheet_key": sheet_key,
            "row_no": row_no,
            "cell": address,
            "column": label,
            "separator": separator,
        },
    )


def _input_count_error(
    rule: Any,
    *,
    sheet_key: str,
    sheet_name: str,
    message: str,
    details: dict[str, Any],
) -> BusinessError:
    return BusinessError(
        "RULE_INPUT_COUNT_INVALID",
        f"工作表「{sheet_name}」第 {rule.source.row_no} 行的规则「{rule.name}」{message}",
        details={"sheet_key": sheet_key, "row_no": rule.source.row_no, "rule": rule.name, **details},
    )


def _rebuild_rule_inputs_from_cells(
    rule: Any,
    *,
    role: str,
    sheet_key: str,
    sheet_name: str,
    left_material: str | None,
    left_field: str | None,
    right_material: str | None,
    right_field: str | None,
) -> None:
    """按四个单元格原文确定性重建比较规则的左右输入与材料范围。

    一条规则只允许一个左输入对一个右输入：多字段关系必须由业务人员拆成多条规则，
    后端不做拆分、不取第一个引用，也不接受模型自行决定的字段关联。
    """
    location = f"工作表「{sheet_name}」第 {rule.source.row_no} 行的规则「{rule.name}」"
    if role == "INTERNAL_VALIDATE" and rule.operator == "LLM_VALIDATE":
        # 条款审核只判断同一份材料里的一段约定，只允许左侧一个输入。
        if right_field is not None or right_material is not None:
            raise _input_count_error(
                rule,
                sheet_key=sheet_key,
                sheet_name=sheet_name,
                message="是“条款审核”，只能填写左侧一个字段，右侧材料与右侧字段必须留空，请修改后重新上传",
                details={"operator": rule.operator, "right_material_ref": right_material, "right_field_ref": right_field},
            )
        if left_material is None or left_field is None:
            raise _input_count_error(
                rule,
                sheet_key=sheet_key,
                sheet_name=sheet_name,
                message="是“条款审核”，必须填写左侧材料与左侧字段（且右侧留空），请补充后重新上传",
                details={"operator": rule.operator, "missing": [
                    label
                    for label, value in (("左侧材料", left_material), ("左侧字段", left_field))
                    if value is None
                ]},
            )
    elif rule.operator == "LLM_VALIDATE":
        # 交叉校验的条款审核同样是“一个左输入对一个右输入”：左右材料与字段都必须完整。
        missing = [
            label
            for label, value in (
                ("左侧材料", left_material),
                ("左侧字段", left_field),
                ("右侧材料", right_material),
                ("右侧字段", right_field),
            )
            if value is None
        ]
        if missing:
            raise BusinessError(
                "RULE_CROSS_INPUT_INCOMPLETE",
                f"{location}的交叉校验必须同时填写左侧材料、左侧字段、右侧材料与右侧字段"
                f"（当前缺少「{'、'.join(missing)}」），请补充后重新上传",
                details={
                    "sheet_key": sheet_key,
                    "row_no": rule.source.row_no,
                    "rule": rule.name,
                    "operator": rule.operator,
                    "missing": missing,
                },
            )
    else:
        # 字段一致 / 字段不同 / 日期比较 / 数值比较必须左右各一个字段，04、05 都只允许两个输入。
        missing = [
            label
            for label, value in (
                ("左侧材料", left_material),
                ("左侧字段", left_field),
                ("右侧材料", right_material),
                ("右侧字段", right_field),
            )
            if value is None
        ]
        if missing:
            raise _input_count_error(
                rule,
                sheet_key=sheet_key,
                sheet_name=sheet_name,
                message=f"的「{'、'.join(missing)}」为空，比较规则必须恰好左右各一个字段"
                "（左侧材料、左侧字段、右侧材料、右侧字段都要填写），请修改后重新上传",
                details={"operator": rule.operator, "missing": missing},
            )
    if role == "INTERNAL_VALIDATE" and left_material is not None and right_material is not None and left_material != right_material:
        # 该格式的材料引用就是材料名称，重名材料会由引用解析明确报错，这里按原文比较即可。
        raise BusinessError(
            "RULE_INTERNAL_MATERIAL_MISMATCH",
            f"{location}的左右字段必须属于同一份材料（左侧材料“{left_material}”、右侧材料“{right_material}”），"
            "文件内部校验不能跨材料比较，请改为交叉校验或修改材料后重新上传",
            details={
                "sheet_key": sheet_key,
                "row_no": rule.source.row_no,
                "rule": rule.name,
                "left_material_ref": left_material,
                "right_material_ref": right_material,
            },
        )
    # 走到这里时“该侧填了字段就一定填了材料”已由上面的数量校验保证（字段引用必须带材料范围）。
    inputs: list[RuleInputProposal] = []
    if left_field is not None:
        inputs.append(RuleInputProposal(field_ref=left_field, material_ref=left_material))
    if right_field is not None:
        inputs.append(RuleInputProposal(field_ref=right_field, material_ref=right_material))
    rule.inputs = inputs
    material_refs: list[str] = []
    for material in (left_material, right_material):
        if material is not None and material not in material_refs:
            material_refs.append(material)
    rule.material_refs = material_refs


def _apply_source_cells(
    proposal: SheetProposal,
    *,
    source_index: dict[str, dict[str, Any]],
    profile_role: dict[str, Any],
    role: str,
    sheet_name: str,
) -> None:
    """以单元格原文覆盖枚举、是否与业务文本。

    5.1 要求中文枚举与标准引用由后端解析，模型不重写这些确定性事实。这里同时保证
    「业务说明 / 抽取说明 / 审核要求 / 检查要求 / 报告内容要求」这些由 UI 收集的文本
    被真实保存并交给后续步骤，而不是被模型改写或丢弃；必填单元格为空时明确报错，
    绝不用模型的替代值补位。校验规则的左右输入与材料范围同样按单元格原文重建，
    模型只负责自然语言说明与算子建议，不决定材料、字段和规则的正式关联。
    """
    values = source_index[proposal.sheet_key]["values"]

    def cell(row_no: int, key: str) -> tuple[str | None, str | None]:
        return _mapped_cell(proposal, values, row_no, key)

    def empty_cell_error(row_no: int, address: str, label: str) -> BusinessError:
        return BusinessError(
            "RULE_REQUIRED_CELL_EMPTY",
            f"工作表「{sheet_name}」第 {row_no} 行的「{label}」为空，请补充后重新上传",
            details={"sheet_key": proposal.sheet_key, "row_no": row_no, "cell": address},
        )

    def text_or_error(row_no: int, address: str, label: str, text: str) -> str:
        if text.startswith("="):
            # 没有缓存值的公式文字不是业务内容：既不当作金额，也不当作审核要求传给模型。
            raise BusinessError(
                "RULE_CELL_FORMULA_UNSUPPORTED",
                f"工作表「{sheet_name}」{address} 的「{label}」是公式而不是业务文本，请直接填写业务描述内容",
                details={"sheet_key": proposal.sheet_key, "row_no": row_no, "cell": address, "column": label},
            )
        return text

    def column_label(key: str) -> str:
        column = next((item for item in profile_role["columns"] if item["key"] == key), None)
        return column["name"] if column else key

    def required_text(row_no: int, key: str, label: str) -> str | None:
        address, text = cell(row_no, key)
        if address is None:
            return None
        if text is None:
            raise empty_cell_error(row_no, address, label)
        return text_or_error(row_no, address, label, text)

    def required_enum(row_no: int, key: str, label: str) -> str | None:
        address, text = cell(row_no, key)
        if address is None:
            return None
        if text is None:
            raise empty_cell_error(row_no, address, label)
        resolved = _column_value_labels(profile_role, key).get(text)
        if not resolved:
            raise BusinessError(
                "RULE_ENUM_VALUE_INVALID",
                f"工作表「{sheet_name}」{address} 的「{label}」填写的“{text}”不在模板允许取值内，请使用下拉选项填写",
                details={"sheet_key": proposal.sheet_key, "cell": address, "column": key},
            )
        return resolved

    def required_boolean(row_no: int, key: str, label: str) -> bool | None:
        address, text = cell(row_no, key)
        if address is None:
            return None
        if text is None:
            raise empty_cell_error(row_no, address, label)
        if text in {"是", "true", "True", "TRUE"}:
            return True
        if text in {"否", "false", "False", "FALSE"}:
            return False
        raise BusinessError(
            "RULE_BOOLEAN_VALUE_INVALID",
            f"工作表「{sheet_name}」{address} 的「{label}」只能填“是”或“否”",
            details={"sheet_key": proposal.sheet_key, "cell": address, "column": key},
        )

    def override_text(row_no: int, key: str, setter: Any) -> None:
        """列已映射时以原格覆盖模型值：单元格存在但为空也要覆盖成空，模型不得补写。"""
        address, text = cell(row_no, key)
        if address is None:
            return
        if text is None:
            setter("")
            return
        setter(text_or_error(row_no, address, column_label(key), text))

    if role == "MATERIAL_LIST":
        for material in proposal.materials:
            row_no = material.source.row_no
            name = required_text(row_no, "name", "材料名称")
            if name is not None:
                material.name = name
            flag = required_boolean(row_no, "required", "是否必传")
            if flag is not None:
                material.required = flag
            override_text(row_no, "description", lambda text: setattr(material, "description", text))
        return
    if role == "FIELD_EXTRACTION":
        for field in proposal.fields:
            row_no = field.source.row_no
            name = required_text(row_no, "name", "字段名称")
            if name is not None:
                field.name = name
            # 「来源材料」是字段的确定性归属：必须按原格重建，指向另一个真实存在的材料也不能保留模型值。
            material_ref = required_text(row_no, "material_ref", column_label("material_ref"))
            if material_ref is not None:
                field.material_ref = material_ref
            value_type = required_enum(row_no, "value_type", "字段类型")
            if value_type is not None:
                field.value_type = value_type
            instruction = required_text(row_no, "instruction", "抽取说明")
            if instruction is not None:
                field.instruction = instruction
            flag = required_boolean(row_no, "required", "是否必抽")
            if flag is not None:
                field.required = flag
            override_text(row_no, "unit", lambda text: setattr(field, "unit", text))
            override_text(row_no, "currency", lambda text: setattr(field, "currency", text))
        return
    if role in {"INTERNAL_VALIDATE", "CROSS_VALIDATE"}:
        applicability_column = any(item["key"] == "applicability" for item in profile_role["columns"])
        # 模板声明了左右四列时，规则输入只由单元格原文派生，模型给出的关联一律丢弃。
        explicit_inputs = _has_explicit_input_columns(profile_role)

        def input_cell(row_no: int, key: str, label: str) -> str | None:
            address, text = cell(row_no, key)
            if address is None or text is None:
                return None
            return _reject_multi_input(
                text,
                sheet_key=proposal.sheet_key,
                sheet_name=sheet_name,
                row_no=row_no,
                address=address,
                label=label,
            )

        for rule in proposal.rules:
            row_no = rule.source.row_no
            name = required_text(row_no, "name", "规则名称")
            if name is not None:
                rule.name = name
            operator = required_enum(row_no, "operator", "检查方式")
            if operator is not None:
                rule.operator = operator
            requirement = required_text(row_no, "parameters", "审核要求")
            if requirement is not None:
                rule.requirement = requirement
            if explicit_inputs:
                _rebuild_rule_inputs_from_cells(
                    rule,
                    role=role,
                    sheet_key=proposal.sheet_key,
                    sheet_name=sheet_name,
                    left_material=input_cell(row_no, "left_material_ref", "左侧材料"),
                    left_field=input_cell(row_no, "left_field_ref", "左侧字段"),
                    right_material=input_cell(row_no, "right_material_ref", "右侧材料"),
                    right_field=input_cell(row_no, "right_field_ref", "右侧字段"),
                )
            if not applicability_column:
                # 该格式版本没有“适用条件”列，规则只能始终适用；模型不能自行扩大或缩小适用范围。
                rule.applicability = None
                continue
            address, text = cell(row_no, "applicability")
            location = f"第 {row_no} 行" + (f"（{address}）" if address else "")
            rule.applicability = _applicability_from_cell(text, sheet_name, location, profile_role)
        return
    if role == "MATERIAL_QUALITY":
        for rule in proposal.rules:
            row_no = rule.source.row_no
            name = required_text(row_no, "name", "检查名称")
            if name is not None:
                rule.name = name
            operator = required_enum(row_no, "operator", "检查项目")
            if operator is not None:
                rule.operator = operator
            # 该工作表两个算子的参数契约由算子本身决定；“检查要求”原文保留在 requirement 上，
            # 模型自造的其它参数执行器无法使用，不能带进已发布配置。
            if rule.operator == "MATERIAL_EXISTS":
                rule.parameters = {}
            else:
                rule.parameters = {
                    key: value for key, value in (rule.parameters or {}).items() if key == "artifact_type"
                }
            override_text(row_no, "parameters", lambda text: setattr(rule, "requirement", text))
            # 「材料名称」是必填列：单元格为空时明确报错，不用模型猜出来的材料范围补位。
            material_ref = required_text(row_no, "material_ref", "材料名称")
            if material_ref is not None:
                rule.material_refs = [material_ref]
        return
    if role == "RISK":
        operator_column = any(item["key"] == "operator" for item in profile_role["columns"])
        for rule in proposal.rules:
            row_no = rule.source.row_no
            name = required_text(row_no, "name", "风险名称")
            if name is not None:
                rule.name = name
            # 「来源节点」是模板下拉列：中文标签在这里确定性转成契约值，不由模型判断。
            # 该值不是 RISK_TRIGGER 的执行参数（参数只允许 risk_level 与 action），因此单独放在规则上。
            source_node = required_enum(row_no, "source_node", "来源节点")
            if source_node is not None:
                rule.source_node = source_node
            source_rule = required_text(row_no, "source_rule_ref", "来源审核规则")
            if source_rule is not None:
                rule.source_rule_ref = source_rule
            if operator_column:
                declared = required_enum(row_no, "operator", "算子")
                if declared is not None and declared != "RISK_TRIGGER":
                    raise BusinessError(
                        "RULE_OPERATOR_INVALID",
                        f"工作表「{sheet_name}」第 {row_no} 行的风险规则算子只能是“命中来源规则即触发风险”",
                        details={"sheet_key": proposal.sheet_key, "row_no": row_no, "operator": declared},
                    )
            # 风险疑点工作表挂在风险评定节点，该节点只有 RISK_TRIGGER 一个算子，由工作表用途确定。
            # 该算子的参数就是这两列的业务内容，由单元格原文确定，模型不参与改写。
            rule.operator = "RISK_TRIGGER"
            rule.parameters = {}
            risk_level = required_enum(row_no, "risk_level", "风险等级")
            if risk_level is not None:
                rule.parameters["risk_level"] = risk_level
            action = required_text(row_no, "action", "处理建议")
            if action is not None:
                rule.parameters["action"] = action
        return
    if role == "REPORT_TEMPLATE":
        for section in proposal.report_sections:
            row_no = section.source.row_no
            section_name = required_text(row_no, "section_name", "章节名称")
            if section_name is not None:
                section.section_name = section_name
            content_instruction = required_text(row_no, "content_instruction", "内容要求")
            if content_instruction is not None:
                section.content_instruction = content_instruction
            # 「展示字段」本来就是列表列，允许一格写多个引用：按分隔符拆分重建，不走 _reject_multi_input。
            address, raw_refs = cell(row_no, "field_refs")
            if address is not None:
                section.field_refs = _split_reference_list(raw_refs)


def _applicability_from_cell(
    text: str | None,
    sheet_name: str,
    location: str,
    profile_role: dict[str, Any],
) -> ApplicabilityProposal | None:
    """适用条件留空 = 始终适用；只支持两种条件，其他复杂条件明确指出未支持。"""
    if text is None:
        return None
    kind = _column_value_labels(profile_role, "applicability").get(text)
    if kind not in {"ALWAYS", "MATERIALS_UPLOADED"}:
        raise BusinessError(
            "RULE_APPLICABILITY_UNSUPPORTED",
            f"工作表「{sheet_name}」{location} 的“适用条件”填写的“{text}”当前不支持："
            "只支持“始终适用”和“所引用材料均已上传”，留空表示始终适用",
            details={"sheet_name": sheet_name, "location": location, "value": text},
        )
    return ApplicabilityProposal(kind=kind)


# 来源节点 -> 规则节点代码集合。风险规则声明的来源节点决定了它的来源规则必须落在哪个节点里，
# 因此规则重名时可以用它把候选收窄到唯一一条。
# 「材料质量」是集合：02_材料质量的「材料是否齐全」在 MATERIAL_CHECK、「文件质量检查」在 DOCUMENT_PREPARE。
SOURCE_NODE_RULE_NODES: dict[str, frozenset[str]] = {
    "MATERIAL_QUALITY": frozenset({"MATERIAL_CHECK", "DOCUMENT_PREPARE"}),
    "INTERNAL_VALIDATE": frozenset({"INTERNAL_VALIDATE"}),
    "CROSS_VALIDATE": frozenset({"CROSS_VALIDATE"}),
}


def _normalize_references_to_candidate_refs(bundle: ProposalBundle) -> None:
    """把「中文名称」形式的引用还原成候选引用。

    业务模板里用户填写的是中文材料名与字段名（字段引用带材料范围），模型据此返回引用。
    这里用模板自身的目录把它还原为候选引用，之后照常走既有的引用校验与规范化。
    已经是候选引用或来源编码的引用原样保留，因此旧模板的行为不变；
    无法唯一确定时明确报错，不做猜测。
    """
    materials = [item for sheet in bundle.sheets for item in sheet.materials]
    fields = [item for sheet in bundle.sheets for item in sheet.fields]
    rules = [item for sheet in bundle.sheets for item in sheet.rules]
    known_refs = {item.candidate_ref for item in [*materials, *fields, *rules]}
    known_source_ids = {item.source_id for item in [*materials, *fields, *rules] if item.source_id}

    material_by_name: dict[str, list[Any]] = {}
    for item in materials:
        material_by_name.setdefault(item.name.strip(), []).append(item)
    duplicated = sorted(name for name, items in material_by_name.items() if len(items) > 1)
    if duplicated:
        raise BusinessError(
            "LLM_MATERIAL_NAME_DUPLICATED",
            "材料清单中存在重复的材料名称，无法确定引用对象",
            details={"names": duplicated},
        )

    def resolve_material(reference: str, owner: str) -> str:
        reference = reference.strip()
        if reference in known_refs or reference in known_source_ids:
            return reference
        matched = material_by_name.get(reference)
        if matched:
            # 材料重名在上面已经整体报错，因此这里是唯一匹配，不存在“取第一条”的猜测。
            return matched[0].candidate_ref
        return reference

    material_of_field: dict[str, str] = {}
    for item in fields:
        item.material_ref = resolve_material(item.material_ref, item.candidate_ref)
        material_of_field[item.candidate_ref] = item.material_ref

    field_by_name: dict[str, list[Any]] = {}
    for item in fields:
        field_by_name.setdefault(item.name.strip(), []).append(item)

    def resolve_field(reference: str, material_reference: str | None, owner: str) -> str:
        reference = reference.strip()
        if reference in known_refs or reference in known_source_ids:
            return reference
        scoped_material = resolve_material(material_reference, owner) if material_reference else None
        if "." in reference or "。" in reference:
            material_name, _, field_name = reference.replace("。", ".").partition(".")
            target = material_by_name.get(material_name.strip())
            if target:
                candidates = [
                    item
                    for item in field_by_name.get(field_name.strip(), [])
                    if material_of_field.get(item.candidate_ref) == target[0].candidate_ref
                ]
                return _single_field(candidates, reference, owner)
            return reference
        candidates = [
            item
            for item in field_by_name.get(reference, [])
            if scoped_material is None or material_of_field.get(item.candidate_ref) == scoped_material
        ]
        return _single_field(candidates, reference, owner)

    rule_by_name: dict[str, list[Any]] = {}
    for item in rules:
        rule_by_name.setdefault(item.name.strip(), []).append(item)
    rule_by_candidate_ref = {item.candidate_ref: item for item in rules}

    def resolve_rule(reference: str, owner: str) -> str:
        reference = reference.strip()
        if reference in known_refs or reference in known_source_ids:
            return reference
        matched = rule_by_name.get(reference)
        if not matched:
            return reference
        if len(matched) == 1:
            return matched[0].candidate_ref
        # 同名规则对应多条候选：先用引用方从单元格确定性读到的「来源节点」收窄，
        # 仍不唯一就明确报错。绝不取第一条来伪装成可确定解析。
        owner_rule = rule_by_candidate_ref.get(owner)
        source_node = owner_rule.source_node if owner_rule is not None else None
        expected_nodes = SOURCE_NODE_RULE_NODES.get(source_node or "")
        if expected_nodes:
            narrowed = [item for item in matched if item.node in expected_nodes]
            if len(narrowed) == 1:
                return narrowed[0].candidate_ref
        raise BusinessError(
            "LLM_RULE_REF_AMBIGUOUS",
            f"来源审核规则“{reference}”在当前工作簿里对应多条规则，无法唯一确定引用，"
            "请改用唯一的规则名称或在「来源节点」里指明它属于哪一类检查",
            details={
                "owner": owner,
                "reference": reference,
                "source_node": source_node,
                "candidates": [
                    {
                        "sheet_key": item.source.sheet_key,
                        "row_no": item.source.row_no,
                        "name": item.name,
                        "node": item.node,
                    }
                    for item in matched
                ],
            },
        )

    for rule in rules:
        for rule_input in rule.inputs:
            rule_input.material_ref = resolve_material(rule_input.material_ref, rule.candidate_ref)
            rule_input.field_ref = resolve_field(rule_input.field_ref, rule_input.material_ref, rule.candidate_ref)
        rule.output_field_refs = [resolve_field(reference, None, rule.candidate_ref) for reference in rule.output_field_refs]
        rule.material_refs = [resolve_material(reference, rule.candidate_ref) for reference in rule.material_refs]
        if rule.source_rule_ref:
            rule.source_rule_ref = resolve_rule(rule.source_rule_ref, rule.candidate_ref)

    for sheet in bundle.sheets:
        for section in sheet.report_sections:
            section.field_refs = [resolve_field(reference, None, section.candidate_ref) for reference in section.field_refs]
        for question in sheet.questions:
            if question.target_type == "MATERIAL":
                question.target_ref = resolve_material(question.target_ref, question.question_ref)
            elif question.target_type == "FIELD":
                question.target_ref = resolve_field(question.target_ref, None, question.question_ref)
            else:
                question.target_ref = resolve_rule(question.target_ref, question.question_ref)
            for option in question.options:
                if question.target_type == "FIELD" and question.property == "material_ref":
                    if isinstance(option.value, str):
                        option.value = resolve_material(option.value, question.question_ref)
                elif question.target_type == "RULE" and question.property == "source_rule_ref":
                    if isinstance(option.value, str):
                        option.value = resolve_rule(option.value, question.question_ref)
                elif question.target_type == "RULE" and question.property == "material_refs":
                    if isinstance(option.value, list):
                        option.value = [
                            resolve_material(reference, question.question_ref) if isinstance(reference, str) else reference
                            for reference in option.value
                        ]


def _single_field(candidates: list[Any], reference: str, owner: str) -> str:
    if len(candidates) == 1:
        return candidates[0].candidate_ref
    if len(candidates) > 1:
        raise BusinessError(
            "LLM_FIELD_REF_AMBIGUOUS",
            "字段引用无法唯一确定，请补充材料范围",
            details={"reference": reference, "owner": owner, "candidates": [item.name for item in candidates]},
        )
    return reference


COVERED_RULE_ROLES = {"INTERNAL_VALIDATE", "CROSS_VALIDATE", "MATERIAL_QUALITY", "RISK"}
# 各类工作表里承载“业务要求”的那一列，用于参数不完整时告诉用户该改哪一列。
RULE_CONTENT_COLUMN = {
    "INTERNAL_VALIDATE": "审核要求",
    "CROSS_VALIDATE": "审核要求",
    "MATERIAL_QUALITY": "检查要求",
    "RISK": "处理建议",
}
COMPARISON_OPERATORS = {"FIELD_EQUAL", "FIELD_NOT_EQUAL", "DATE_COMPARE", "DECIMAL_COMPARE"}


def _parameter_option(label: str, value: dict[str, Any]) -> QuestionOption:
    return QuestionOption(label=label, value=value)


def _parameter_question(
    rule: Any,
    *,
    suffix: str,
    title: str,
    description: str,
    options: list[QuestionOption],
    input_kind: str = "SELECT",
    resolve: AnswerResolution | None = None,
) -> BusinessQuestion:
    """参数问题统一由后端生成，作答值直接就是写到 parameters 上的完整取值。"""
    return BusinessQuestion(
        question_ref=f"param:{rule.candidate_ref}:{suffix}",
        title=title,
        description=description,
        target_type="RULE",
        target_ref=rule.candidate_ref,
        property="parameters",
        input_kind=input_kind,
        options=options,
        resolve=resolve,
        source=rule.source,
    )


def _align_input_order(rule: Any, proposal: SheetProposal, values: dict[int, dict[str, Any]]) -> None:
    """按左侧/右侧单元格的真实填写顺序纠正模型给出的输入顺序。

    比较方向（不早于/不晚于、不小于/不大于）依赖左右顺序，顺序反了结论就反了。
    只在单元格文本能唯一指认左右两侧时交换；模型返回候选引用时保持原样，不做猜测。
    只有遗留格式（没有左右四列的模板）才需要它：这类格式的输入来自模型，
    新格式的输入已由左右四列单元格原文确定性重建，顺序天然是左前右后。
    """
    if rule.operator not in COMPARISON_OPERATORS or len(rule.inputs) != 2:
        return
    left = _mapped_cell(proposal, values, rule.source.row_no, "left_field_ref")[1]
    right = _mapped_cell(proposal, values, rule.source.row_no, "right_field_ref")[1]
    if not left or not right or left == right:
        return

    def matches(reference: str, text: str) -> bool:
        reference = reference.strip()
        return reference == text or reference.endswith(f".{text}")

    first, second = rule.inputs[0].field_ref, rule.inputs[1].field_ref
    if matches(second, left) and matches(first, right) and not matches(first, left):
        rule.inputs = [rule.inputs[1], rule.inputs[0]]


def _complete_parameters(rule: Any, proposal: SheetProposal, sheet_name: str) -> bool:
    """补全比较规则的方向与差额参数。

    参数只能来自“审核要求”原文：确定不了就生成业务问题要求补充，绝不设默认方向或默认阈值。
    返回是否生成了参数问题；生成了就不能再按算子契约校验（等用户补充后再校验）。
    """
    parameters = rule.parameters if isinstance(rule.parameters, dict) else {}
    requirement = rule.requirement or "（空）"
    location = f"工作表「{sheet_name}」第 {rule.source.row_no} 行的规则「{rule.name}」"
    if rule.operator == "DATE_COMPARE":
        if parameters.get("operator") in DATE_COMPARE_OPERATORS:
            return False
        proposal.questions.append(
            _parameter_question(
                rule,
                suffix="date_direction",
                title=f"请确认规则「{rule.name}」的日期比较方向",
                description=(
                    f"{location}没有给出可用的比较方向，审核要求原文是：“{requirement}”。"
                    "请选择这条规则实际的日期比较方向；系统不会采用默认方向。"
                ),
                options=[
                    _parameter_option(DATE_COMPARISON_LABELS[item], {"operator": item})
                    for item in sorted(DATE_COMPARE_OPERATORS)
                ],
            )
        )
        return True
    if rule.operator != "DECIMAL_COMPARE":
        return False
    comparison = parameters.get("operator")
    threshold = parameters.get("threshold")
    has_threshold = isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
    if comparison not in DECIMAL_COMPARE_OPERATORS:
        # 差额选项只在审核要求已经给出具体数值时提供，不编造阈值让用户二选一。
        directions = [
            item for item in sorted(DECIMAL_COMPARE_OPERATORS) if item != "ABS_DIFF_LTE" or has_threshold
        ]
        proposal.questions.append(
            _parameter_question(
                rule,
                suffix="decimal_direction",
                title=f"请确认规则「{rule.name}」的金额比较方式",
                description=(
                    f"{location}没有给出可用的金额比较方式，审核要求原文是：“{requirement}”。"
                    "请选择这条规则实际的比较方式；系统不会采用默认方式，也不会替你设定差额阈值。"
                ),
                options=[
                    _parameter_option(
                        f"{DECIMAL_COMPARISON_LABELS[item]} {threshold:g}" if item == "ABS_DIFF_LTE" else DECIMAL_COMPARISON_LABELS[item],
                        {"operator": item} if item != "ABS_DIFF_LTE" else {"operator": item, "threshold": threshold},
                    )
                    for item in directions
                ],
            )
        )
        return True
    if comparison == "ABS_DIFF_LTE" and not has_threshold:
        proposal.questions.append(
            _parameter_question(
                rule,
                suffix="decimal_threshold",
                title=f"请补充规则「{rule.name}」允许的金额差额",
                description=(
                    f"{location}采用“绝对差不超过”比较，但审核要求没有给出具体差额数值，原文是：“{requirement}”。"
                    "请填写允许的绝对差额（元），系统不会替你指定默认阈值。"
                ),
                input_kind="DECIMAL",
                options=[],
                resolve=AnswerResolution(
                    mode="SET_PARAMETER",
                    parameter_key="threshold",
                    base_parameters={"operator": "ABS_DIFF_LTE"},
                ),
            )
        )
        return True
    return False


def _require_executable_parameters(rule: Any, sheet_name: str, content_column: str) -> None:
    """按算子契约校验参数；不完整时在导入阶段就明确报错，而不是等到发布。"""
    try:
        validate_rule_parameters(rule.name, rule.operator, rule.parameters)
    except BusinessError as exc:
        raise BusinessError(
            exc.code,
            f"工作表「{sheet_name}」第 {rule.source.row_no} 行的规则「{rule.name}」参数不符合要求（{exc.message}），"
            f"请补充或修改“{content_column}”等业务列后重新上传",
            details={
                "sheet_key": rule.source.sheet_key,
                "row_no": rule.source.row_no,
                "rule": rule.name,
                **(exc.details or {}),
            },
        ) from exc


def _prepare_rules(
    proposal: SheetProposal,
    *,
    source_index: dict[str, dict[str, Any]],
    profile_role: dict[str, Any],
    role: str,
    sheet_name: str,
) -> None:
    """把规则参数补全成可执行契约，并纠正遗留格式比较规则的左右输入顺序。"""
    if role not in COVERED_RULE_ROLES:
        return
    if role in {"INTERNAL_VALIDATE", "CROSS_VALIDATE"}:
        # 参数问题一律由后端生成：模型给出的问题对象无法保证作答值能落到 parameters 上。
        # 这里丢掉模型自造的参数问题不影响业务输入，“审核要求”原文始终保留在规则上。
        proposal.questions = [
            question
            for question in proposal.questions
            if not (question.target_type == "RULE" and question.property == "parameters")
        ]
    # 只有遗留格式的输入来自模型，需要按单元格指认的左右字段纠正顺序；
    # 新格式的输入由左右四列原文重建，顺序已经是左侧在前、右侧在后。
    align_input_order = role in {"INTERNAL_VALIDATE", "CROSS_VALIDATE"} and not _has_explicit_input_columns(
        profile_role
    )
    values = source_index[proposal.sheet_key]["values"]
    pending: set[str] = set()
    for rule in proposal.rules:
        if role in {"INTERNAL_VALIDATE", "CROSS_VALIDATE"}:
            if align_input_order:
                _align_input_order(rule, proposal, values)
            if rule.operator == "LLM_VALIDATE":
                # “条款审核”的判断依据就是“审核要求”原文，由后端按单元格文本确定性填写 instruction。
                # 该算子的参数契约只有 instruction 与 requires_manual_review，其余取值执行器无法使用；
                # 审核要求原文本身仍然完整保存在规则的 requirement 上，业务输入不丢失。
                rule.parameters = {
                    key: value for key, value in (rule.parameters or {}).items() if key == "requires_manual_review"
                }
                rule.parameters["instruction"] = rule.requirement
            if _complete_parameters(rule, proposal, sheet_name):
                pending.add(rule.candidate_ref)
        if rule.candidate_ref not in pending:
            _require_executable_parameters(rule, sheet_name, RULE_CONTENT_COLUMN[role])


def _reject_unsupported_capabilities(bundle: ProposalBundle, sheet_names: dict[str, str]) -> None:
    """汇总当前不支持的能力并明确报错。

    统一走 domain.manifest.detect_unsupported_capability：对所有算子生效（含 LLM_VALIDATE），
    绝不把“全部发票求和”这类要求降级成“取第一份/最后一份文件”，也不改成让大模型估算合计。
    模型主动声明的 unsupported_capability 原样保留，命中来源记为 MODEL，后端识别的记为 BACKEND。
    """
    findings: list[dict[str, Any]] = []
    for sheet in bundle.sheets:
        for rule in sheet.rules:
            item = {
                "sheet_name": sheet_names.get(rule.source.sheet_key, rule.source.sheet_key),
                "row_no": rule.source.row_no,
                "rule_name": rule.name,
            }
            if rule.unsupported_capability is not None:
                findings.append(
                    {
                        **item,
                        "code": rule.unsupported_capability.code,
                        "detail": rule.unsupported_capability.detail,
                        "matched": None,
                        "detected_by": "MODEL",
                    }
                )
                continue
            parameters = rule.parameters if isinstance(rule.parameters, dict) else {}
            detected = detect_unsupported_capability_for_operator(
                rule.operator, rule.requirement, parameters.get("instruction"), rule.name
            )
            if detected:
                findings.append(
                    {
                        **item,
                        "code": detected[0],
                        "detail": detected[2],
                        "matched": detected[1],
                        "detected_by": "BACKEND",
                    }
                )
    if not findings:
        return
    raise BusinessError(
        "RULE_CAPABILITY_UNSUPPORTED",
        "以下规则要求的取数能力当前版本不支持，不能转换："
        + "；".join(
            f"工作表「{item['sheet_name']}」第 {item['row_no']} 行的规则「{item['rule_name']}」要求{item['detail']}"
            for item in findings
        )
        + "。请改为两个明确字段之间的一对一比较：系统不会取第一份或最后一份文件代替，也不会让大模型估算合计。",
        details={"rules": findings},
    )


DIRECTORY_ROLE_ORDER = {"MATERIAL_LIST": 0, "FIELD_EXTRACTION": 1}
# 这些工作表的填写内容就是规则；填了却没有转换出规则时必须报错，不能静默丢弃。
RULE_ONLY_ROLES = {"MATERIAL_QUALITY", "INTERNAL_VALIDATE", "CROSS_VALIDATE", "RISK"}


def _directory_entries(
    proposal: SheetProposal,
    material_names: dict[str, str],
) -> list[dict[str, Any]]:
    """目录只登记材料、字段和规则；后续工作表据此引用，而不是反过来。

    material_names 把字段的 material_ref（可能是候选引用或材料名）映射成可读材料名，
    这样规则表能直接按模板要求填写中文引用。
    """
    entries: list[dict[str, Any]] = [
        {
            "kind": "MATERIAL",
            "name": item.name,
            "required": item.required,
        }
        for item in proposal.materials
    ]
    entries.extend(
        {
            "kind": "FIELD",
            "name": item.name,
            "material_name": material_names.get(item.material_ref, item.material_ref),
            "value_type": item.value_type,
            "unit": item.unit,
            "currency": item.currency,
            "required": item.required,
        }
        for item in proposal.fields
    )
    entries.extend(
        {"kind": "RULE", "name": item.name, "node": item.node, "operator": item.operator}
        for item in proposal.rules
    )
    return entries


def _conversion_targets(
    ir: WorkbookIR,
    bindings: dict[str, dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """确定每个待转换工作表的内容；材料与字段目录表排在最前，不依赖 Excel 物理顺序。"""
    targets: list[dict[str, Any]] = []
    for sheet in ir.sheets:
        binding = bindings.get(sheet.sheet_key, {})
        role = binding.get("role")
        # 判断依据只有用户绑定或明确排除：**不看 `sheet.visible`**。
        # 隐藏只是 Excel 的展示属性，隐藏的业务 Sheet 仍然可以由用户绑定并参与转换；
        # 按隐藏跳过会让用户明确绑定过的表在转换期被静默丢掉。
        if binding.get("excluded") or sheet.state in {"EMPTY", "NO_BUSINESS_DATA"}:
            continue
        if not role:
            raise BusinessError(
                "SHEET_ROLE_REQUIRED",
                "存在尚未确认用途的工作表，请先完成工作表绑定。系统不会把建议用途写成已确认角色",
                details={"sheet_key": sheet.sheet_key, "sheet_name": sheet.name},
            )
        if role not in profiles:
            raise BusinessError("SHEET_ROLE_INVALID", "工作表用途不在合同审核支持范围内", details={"role": role})
        header_row = binding.get("header_row")
        if not isinstance(header_row, int):
            raise BusinessError(
                "SHEET_HEADER_REQUIRED",
                "业务工作表必须先确认表头行，系统不会把第一表头候选写成已确认表头",
                details={"sheet_name": sheet.name},
            )
        # 业务转换范围是完整 IR 的非空业务行，preview_rows 只用于界面摘要，不得当作最终转换范围。
        header = next((row for row in sheet.rows if row.row_no == header_row), None)
        if not header:
            raise BusinessError("SHEET_HEADER_SOURCE_MISSING", "工作表不存在所选表头行", details={"sheet_name": sheet.name})
        data_rows = [_llm_row_payload(row) for row in sheet.rows if row.row_no > header_row]
        if not data_rows:
            raise BusinessError(
                "SHEET_DATA_REQUIRED",
                "业务工作表在所选表头后没有可转换的数据行",
                details={"sheet_key": sheet.sheet_key, "sheet_name": sheet.name, "header_row": header_row},
            )
        targets.append(
            {"sheet": sheet, "role": role, "header_row": header_row, "header": header, "data_rows": data_rows}
        )
    # 材料清单必须先于字段抽取建立，规则表最后；不依赖工作表在 Excel 里的物理顺序。
    targets.sort(key=lambda item: DIRECTORY_ROLE_ORDER.get(item["role"], 2))
    return targets


def _attach_source_texts(proposal: SheetProposal, source_index: dict[str, dict[str, Any]]) -> None:
    """按行原文填充 SourceRef.texts 并同步 cells。

    每个已映射列的非空原格都成为来源文本，预览与证据链因此能逐项对照 Excel 原文；
    模型给出的来源单元格不足以承担这件事，故由后端确定性重建。
    """
    values = source_index[proposal.sheet_key]["values"]
    items = [
        *proposal.materials,
        *proposal.fields,
        *proposal.rules,
        *proposal.report_sections,
        *proposal.questions,
    ]
    for item in items:
        row_values = values.get(item.source.row_no, {})
        texts: dict[str, str] = {}
        for header in proposal.column_mapping.values():
            column, _ = coordinate_from_string(header)
            address = f"{column}{item.source.row_no}"
            raw = row_values.get(address)
            if raw not in (None, ""):
                texts[address] = str(raw)
        if texts:
            item.source.texts = texts
            item.source.cells = sorted(texts)


def _build_coverage(
    target: dict[str, Any],
    sheet_proposals: list[SheetProposal],
    *,
    source_index: dict[str, dict[str, Any]],
    profile_role: dict[str, Any],
    sheet_notes: list[str],
) -> CoverageSheet:
    """逐业务行对账：表头行之后每条非空业务行都必须有候选、明确排除或待处理问题之一。

    模型只返回九条时第十条会落到 QUESTION/EXCLUDED，绝不会显示成“已转换”。
    分组标题行 / 纯说明行 / 只填一列的残行记 EXCLUDED 并给出原因，不静默丢弃。
    sheet_proposals 用的是规范化后的最终候选（引用已固定），因此 notes 里的引用与候选一致。
    """
    sheet = target["sheet"]
    header_row = target["header_row"]
    sheet_key = sheet.sheet_key
    values = source_index[sheet_key]["values"]
    mapping = sheet_proposals[0].column_mapping if sheet_proposals else {}
    column_names = {column["key"]: column["name"] for column in profile_role["columns"]}
    counts: dict[int, dict[str, int]] = {}
    candidate_refs: dict[int, list[str]] = {}

    def record(row_no: int, is_question: bool, reference: str) -> None:
        entry = counts.setdefault(row_no, {"candidate": 0, "question": 0})
        if is_question:
            entry["question"] += 1
            return
        entry["candidate"] += 1
        candidate_refs.setdefault(row_no, []).append(reference)

    for proposal in sheet_proposals:
        for item in [*proposal.materials, *proposal.fields, *proposal.rules, *proposal.report_sections]:
            record(item.source.row_no, False, item.candidate_ref)
        for question in proposal.questions:
            record(question.source.row_no, True, question.question_ref)

    rows: list[CoverageRow] = []
    for row in sheet.rows:
        if row.row_no <= header_row:
            continue
        row_values = values.get(row.row_no, {})
        entry = counts.get(row.row_no, {"candidate": 0, "question": 0})
        filled = [
            column_names.get(key, key)
            for key, header in mapping.items()
            if row_values.get(f"{coordinate_from_string(header)[0]}{row.row_no}") not in (None, "")
        ]
        notes = list(sheet_notes)
        if entry["candidate"] > 0:
            disposition = "CANDIDATE"
            reason = None
            refs = candidate_refs.get(row.row_no, [])
            if len(refs) > 1:
                notes.append(f"该行拆分为 {len(refs)} 条候选：{'、'.join(refs)}")
        elif not filled:
            disposition = "EXCLUDED"
            reason = "该行在所有已映射的业务列上都没有内容，视为分组标题或说明文字"
        elif len(filled) == 1:
            disposition = "EXCLUDED"
            reason = f"该行只填写了「{filled[0]}」一列，未形成完整业务记录（分组标题或说明行）"
        else:
            disposition = "QUESTION"
            reason = (
                f"该行填写了「{'、'.join(filled)}」，但没有转换出任何候选；"
                "请确认它是业务记录，还是需要补充完整后重新上传"
            )
        rows.append(
            CoverageRow(
                row_no=row.row_no,
                disposition=disposition,
                reason=reason,
                candidate_count=entry["candidate"],
                question_count=entry["question"],
                merged_into=None,
                notes=notes,
            )
        )
    return CoverageSheet(sheet_key=sheet_key, sheet_name=sheet.name, business_rows=len(rows), rows=rows)


def _convert_target(
    target: dict[str, Any],
    *,
    ir: WorkbookIR,
    profiles: dict[str, dict[str, Any]],
    source_index: dict[str, dict[str, Any]],
    directory: list[dict[str, Any]],
) -> tuple[list[SheetProposal], list[str]]:
    """转换单个工作表；规则表能拿到已经建立好的材料、字段和规则目录。

    返回该工作表已转换的提案，以及列映射冲突说明（供覆盖对账记录，不静默覆盖模型映射）。
    """
    settings = get_settings()
    sheet = target["sheet"]
    role = target["role"]
    header_row = target["header_row"]
    header = target["header"]
    version = ir.profile_version
    profile_role = profiles[role]
    header_values = source_index[sheet.sheet_key]["values"].get(header_row, {})
    # 表头语义先由 profile 确定性判定；模型只填补歧义列，不得改写确定性命中的映射。
    deterministic, ambiguous_keys = _deterministic_column_mapping(
        profile_role, header_values, sheet_name=sheet.name, header_row=header_row
    )
    produced: list[SheetProposal] = []
    column_notes: list[str] = []
    for offset in range(0, len(target["data_rows"]), settings.llm_chunk_rows):
        chunk = target["data_rows"][offset : offset + settings.llm_chunk_rows]
        payload = {
            "workbook_id": ir.workbook_id,
            "sheet_id": sheet.sheet_id,
            "profile_version": version,
            "sheet_key": sheet.sheet_key,
            "sheet_name": sheet.name,
            "bound_role": role,
            "header_row": header_row,
            "header_cells": _llm_row_payload(header),
            "standard_columns": profile_role["columns"],
            "deterministic_column_mapping": deterministic,
            "directory": directory,
            "rows": chunk,
            "response_contract": _response_contract(),
        }
        proposal = _call_llm(payload, role, version)
        if proposal.suggested_role != role:
            raise BusinessError(
                "LLM_SHEET_ROLE_CONFLICT",
                "大模型建议的工作表用途与当前绑定不一致，请调整工作表绑定后重试",
                details={"sheet_name": sheet.name, "bound_role": role, "suggested_role": proposal.suggested_role},
            )
        allowed_rows = {
            header.row_no: set(header.cells),
            **{row["row_no"]: set(row["cells"]) for row in chunk},
        }
        _validate_source(
            proposal,
            source_index,
            expected_sheet_key=sheet.sheet_key,
            header_row=header_row,
            allowed_rows=allowed_rows,
        )
        column_notes = _merge_column_mapping(
            proposal,
            profile_role=profile_role,
            deterministic=deterministic,
            ambiguous_keys=ambiguous_keys,
            header_values=header_values,
            sheet_name=sheet.name,
            header_row=header_row,
        )
        _validate_candidate_source_ids(proposal, source_index=source_index, role=role, version=version)
        # 确定性事实（中文枚举、是否、业务文本、空值覆盖）以单元格原文为准，模型不重写这些内容。
        _apply_source_cells(
            proposal,
            source_index=source_index,
            profile_role=profile_role,
            role=role,
            sheet_name=sheet.name,
        )
        _prepare_rules(
            proposal,
            source_index=source_index,
            profile_role=profile_role,
            role=role,
            sheet_name=sheet.name,
        )
        _attach_source_texts(proposal, source_index)
        produced.append(proposal)
    if produced and role in RULE_ONLY_ROLES and not any(item.rules for item in produced):
        raise BusinessError(
            "LLM_SHEET_RULES_MISSING",
            "该工作表填有内容，但没有转换出任何规则，请检查填写内容是否符合模板列的要求",
            details={"sheet_name": sheet.name, "role": role},
        )
    if produced and role == "REPORT_TEMPLATE" and not any(item.report_sections for item in produced):
        raise BusinessError(
            "LLM_SHEET_SECTIONS_MISSING",
            "报告模板工作表填有内容，但没有转换出任何报告章节，请检查填写内容是否符合模板列的要求",
            details={"sheet_name": sheet.name, "role": role},
        )
    return produced, column_notes



def convert_node_rulebook(
    ir: WorkbookIR,
    bindings: dict[str, dict[str, Any]],
    *,
    profile: dict[str, Any] | None = None,
) -> tuple[ProposalBundle, dict[str, Any]]:
    """节点版规则库的确定性转换入口。

    返回（候选集合，节点版附加信息）。附加信息是 `build_convert_bundle` 的原始 payload，
    含 `node_notes`（必须留给业务处理的待确认事项与能力缺口）、`coverage`（逐行对账）
    与 `bundle_hash`。消费方按 `node_notes` 取待确认事项，不能把整份 payload 当成列表。

    转换结果与模型路径共用 `ProposalBundle` 契约，因此下游的引用校验、裁决、编译、
    发布与页面展示不需要第二套代码；差别只在于这里的候选来自代码解析而非模型建议。
    """
    payload = node_rulebook.build_convert_bundle(ir, bindings, profile=profile)
    try:
        bundle = ProposalBundle.model_validate(
            {"sheets": payload["sheets"], "coverage": payload["coverage"]}
        )
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first_error["loc"])
        raise BusinessError(
            "NODE_RULEBOOK_BUNDLE_INVALID",
            f"节点版规则库解析结果与候选契约不一致：{location}（{first_error['type']}）",
            status_code=500,
        ) from exc
    return bundle, payload


def convert_workbook_with_notes(
    ir: WorkbookIR,
    bindings: dict[str, dict[str, Any]],
    *,
    profile: dict[str, Any] | None = None,
) -> tuple[ProposalBundle, dict[str, Any] | None]:
    """返回（候选集合，转换附加信息）。

    确定性路径返回含 `node_notes` / `coverage` / `bundle_hash` 的字典；
    模型路径返回 None。消费方必须按类型取 `node_notes`，不能把整份附加信息当成列表。

    分派依据是**用户绑定的 Sheet 角色**，不是文件名、表名或表头；
    列名和别名由上传时冻结的场景有效 Profile 提供。

    **按 Sheet 独立转换，结果合并**：每张表由负责它的解析器转换（01/02/03/04/05/09/10
    走 `node_rulebook`，06/07/08 计算三表走 `financial_report`），绑定里出现哪几类表就
    转换哪几类，彼此互不干扰。此前是「按业务族二选一」，绑定里一旦混了，就必然有一条
    路径静默胜出、另一批表被丢掉；现在没有这条分支，也没有业务族这一层。

    角色完全不在当前有效协议里时明确报错，不静默按模型路径处理。
    """
    if profile is None:
        profile = unified_profile.load_unified_profile()
    index = unified_profile.sheet_index(profile)
    roles = sorted(
        {
            str((bindings.get(sheet.sheet_key) or {}).get("role"))
            for sheet in ir.sheets
            # 命中说明页的元数据 Sheet 由导入侧固定排除；这里只看用户绑定与明确排除，
            # 不看 `sheet.visible`（隐藏的业务表仍可由用户绑定并参与转换）。
            if not (bindings.get(sheet.sheet_key) or {}).get("excluded")
        }
        - {"None"}
    )
    if not roles:
        # 一张业务表都没有绑定：没有可转换的内容，明确报错而不是返回空候选。
        raise BusinessError(
            "IMPORT_NO_BOUND_SHEET",
            "本次导入没有绑定任何业务工作表，没有可转换的内容",
            details={"sheet_count": len(ir.sheets)},
        )
    unknown = [role for role in roles if role not in index]
    if unknown:
        raise BusinessError(
            "SHEET_ROLE_UNSUPPORTED",
            "本次导入绑定的 Sheet 用途不在统一模板协议里："
            + "、".join(f"“{item}”" for item in unknown)
            + "。请确认绑定结果",
            details={"roles": unknown},
        )
    from ..domain import financial_report, node_rulebook

    parts: list[tuple[ProposalBundle, dict[str, Any]]] = []
    if set(roles) & node_rulebook.OWNED_ROLES:
        parts.append(convert_node_rulebook(ir, bindings, profile=profile))
    if set(roles) & financial_report.OWNED_ROLES:
        parts.append(convert_financial_report(ir, bindings, profile=profile))
    if not parts:
        raise BusinessError(
            "SHEET_ROLE_UNSUPPORTED",
            "本次导入绑定的 Sheet 用途没有对应的确定性转换路径："
            + "、".join(f"“{item}”" for item in roles),
            details={"roles": roles},
        )
    if len(parts) == 1:
        return parts[0]
    return _merge_bundles(parts)


def _merge_bundles(
    parts: list[tuple[ProposalBundle, dict[str, Any]]],
) -> tuple[ProposalBundle, dict[str, Any]]:
    """合并多个按 Sheet 独立转换的结果。

    合并是**并集**：Sheet 候选、逐行覆盖对账、计算候选与待确认事项各自拼接。
    各解析器只产出自己那几张表的候选，因此不存在「同一张表被两处转换」的情况；
    真出现重复 sheet_key 说明绑定或分派有矛盾，明确报错而不是覆盖。
    """
    sheets: list[Any] = []
    coverage_sheets: list[dict[str, Any]] = []
    calculations: list[Any] = []
    notes: list[dict[str, Any]] = []
    seen_sheets: set[str] = set()
    for bundle, payload in parts:
        for sheet in bundle.sheets:
            if sheet.sheet_key in seen_sheets:
                raise BusinessError(
                    "IMPORT_SHEET_CONVERTED_TWICE",
                    "同一张工作表被转换了两次，绑定或分派存在矛盾",
                    status_code=500,
                    details={"sheet_key": sheet.sheet_key},
                )
            seen_sheets.add(sheet.sheet_key)
            sheets.append(sheet)
        coverage = payload.get("coverage") or {}
        coverage_sheets.extend(coverage.get("sheets") or [])
        calculations.extend(payload.get("calculations") or [])
        notes.extend(payload.get("node_notes") or [])
    coverage = {
        "sheets": coverage_sheets,
        "totals": coverage_totals(coverage_sheets),
    }
    merged = ProposalBundle.model_validate(
        {"sheets": sheets, "coverage": coverage, "calculations": calculations}
    )
    hashes = [str(payload.get("bundle_hash") or "") for _, payload in parts]
    payload = {
        "node_notes": notes,
        "coverage": coverage,
        "calculations": calculations,
        "bundle_hash": hashlib.sha256("|".join(sorted(hashes)).encode("utf-8")).hexdigest(),
        "merged_parts": len(parts),
    }
    return merged, payload


def convert_financial_report(
    ir: WorkbookIR,
    bindings: dict[str, dict[str, Any]],
    *,
    profile: dict[str, Any] | None = None,
) -> tuple[ProposalBundle, dict[str, Any]]:
    """计算类规则库的确定性转换入口。

    与合同审核路径同一收口方式：转换结果共用 `ProposalBundle` 契约，因此下游的
    引用校验、裁决、编译、发布与页面展示不需要第二套代码。差别在于这里的附加信息里
    多了 `calculations`（计算规则候选：定义 + 参数 + 判定区间）。

    列协议由导入链传入的场景有效 Profile 提供。
    """
    from ..domain import financial_report

    if profile is None:
        profile = unified_profile.load_unified_profile()
    payload = financial_report.build_convert_bundle(ir, bindings, profile)
    try:
        bundle = ProposalBundle.model_validate(
            {
                "sheets": payload["sheets"],
                "coverage": payload["coverage"],
                # 计算规则候选跨 Sheet（定义/参数/判定三张表按 rule_code 关联），
                # 因此放在顶层而不是挂在某一张 Sheet 上。
                "calculations": payload["calculations"],
            }
        )
    except ValidationError as exc:
        raise BusinessError(
            "FINANCIAL_REPORT_BUNDLE_INVALID",
            "计算类规则库的确定性解析结果不符合候选契约，请检查填写内容后重新上传",
            status_code=500,
        ) from exc
    return bundle, payload


def _reference_alias_index(
    items: list[Any],
    target_type: str,
) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    candidate_aliases: dict[str, str] = {}
    source_aliases: dict[tuple[str, str], str] = {}
    for item in items:
        candidate_ref = item.candidate_ref
        if candidate_ref in candidate_aliases:
            raise BusinessError(
                "LLM_DUPLICATE_REF",
                "同类候选不能使用重复的候选引用",
                details={"target_type": target_type, "candidate_ref": candidate_ref},
            )
        candidate_aliases[candidate_ref] = candidate_ref
        if not item.source_id:
            continue
        scope = (item.source.sheet_key, item.source_id)
        existing = source_aliases.get(scope)
        if existing is not None and existing != candidate_ref:
            raise BusinessError(
                "LLM_DUPLICATE_SOURCE_ID",
                "同一工作表内的同类来源编码不能重复",
                details={"target_type": target_type, "sheet_key": scope[0], "source_ref": scope[1]},
            )
        source_aliases[scope] = candidate_ref
    for (sheet_key, source_id), source_owner in source_aliases.items():
        candidate_owner = candidate_aliases.get(source_id)
        if candidate_owner is not None and candidate_owner != source_owner:
            raise BusinessError(
                "LLM_CANDIDATE_SOURCE_REF_CONFLICT",
                "候选引用不能与另一候选的来源编码冲突",
                details={
                    "target_type": target_type,
                    "sheet_key": sheet_key,
                    "source_ref": source_id,
                    "candidate_ref": candidate_owner,
                    "source_owner": source_owner,
                },
            )
    return candidate_aliases, source_aliases


def _resolve_reference(
    reference: str,
    owner_sheet_key: str,
    candidate_aliases: dict[str, str],
    source_aliases: dict[tuple[str, str], str],
    *,
    target_type: str,
    owner: str,
    error_code: str,
    error_message: str,
) -> str:
    candidate_ref = candidate_aliases.get(reference)
    if candidate_ref is not None:
        return candidate_ref
    source_ref = source_aliases.get((owner_sheet_key, reference))
    if source_ref is not None:
        return source_ref
    cross_sheet_matches = {
        candidate_ref
        for (_, source_id), candidate_ref in source_aliases.items()
        if source_id == reference
    }
    if len(cross_sheet_matches) == 1:
        return next(iter(cross_sheet_matches))
    if cross_sheet_matches:
        raise BusinessError(
            "LLM_SOURCE_SCOPE_INVALID",
            "来源编码在多个工作表中重复，无法跨工作表唯一解析，请使用候选引用关联",
            details={
                "target_type": target_type,
                "reference": reference,
                "owner": owner,
                "owner_sheet_key": owner_sheet_key,
            },
        )
    raise BusinessError(
        error_code,
        error_message,
        details={"target_type": target_type, "reference": reference, "owner": owner},
    )


def _validate_bundle_references(bundle: ProposalBundle) -> None:
    materials = [item for sheet in bundle.sheets for item in sheet.materials]
    fields = [item for sheet in bundle.sheets for item in sheet.fields]
    rules = [item for sheet in bundle.sheets for item in sheet.rules]
    report_sections = [item for sheet in bundle.sheets for item in sheet.report_sections]
    sheet_contracts: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {}
    for sheet in bundle.sheets:
        contract = (sheet.suggested_role, tuple(sorted(sheet.column_mapping.items())))
        previous = sheet_contracts.get(sheet.sheet_key)
        if previous is not None and previous != contract:
            raise BusinessError(
                "LLM_SHEET_BATCH_CONFLICT",
                "同一工作表不同分块返回的用途或列映射不一致",
                details={"sheet_key": sheet.sheet_key},
            )
        sheet_contracts[sheet.sheet_key] = contract
        for item in [*sheet.materials, *sheet.fields, *sheet.rules, *sheet.report_sections, *sheet.questions]:
            if item.source.sheet_key != sheet.sheet_key:
                raise BusinessError(
                    "LLM_SOURCE_REF_INVALID",
                    "大模型返回的候选来源工作表与所属工作表不一致",
                    details={"sheet_key": sheet.sheet_key, "source_sheet_key": item.source.sheet_key},
                )
    question_refs = [question.question_ref for sheet in bundle.sheets for question in sheet.questions]
    if len(question_refs) != len(set(question_refs)):
        raise BusinessError("LLM_DUPLICATE_REF", "大模型返回了重复的问题引用")
    report_section_refs = [section.candidate_ref for section in report_sections]
    if len(report_section_refs) != len(set(report_section_refs)):
        raise BusinessError("LLM_DUPLICATE_REF", "大模型返回了重复的报告章节候选引用")
    material_candidate_aliases, material_source_aliases = _reference_alias_index(materials, "MATERIAL")
    field_candidate_aliases, field_source_aliases = _reference_alias_index(fields, "FIELD")
    rule_candidate_aliases, rule_source_aliases = _reference_alias_index(rules, "RULE")
    for field in fields:
        _resolve_reference(
            field.material_ref,
            field.source.sheet_key,
            material_candidate_aliases,
            material_source_aliases,
            target_type="MATERIAL",
            owner=field.candidate_ref,
            error_code="LLM_MATERIAL_REF_INVALID",
            error_message="字段引用了不存在的材料",
        )
    for rule in rules:
        for item in rule.inputs:
            _resolve_reference(
                item.material_ref,
                rule.source.sheet_key,
                material_candidate_aliases,
                material_source_aliases,
                target_type="MATERIAL",
                owner=rule.candidate_ref,
                error_code="LLM_RULE_REF_INVALID",
                error_message="规则输入引用了不存在的材料",
            )
            _resolve_reference(
                item.field_ref,
                rule.source.sheet_key,
                field_candidate_aliases,
                field_source_aliases,
                target_type="FIELD",
                owner=rule.candidate_ref,
                error_code="LLM_RULE_REF_INVALID",
                error_message="规则输入引用了不存在的字段",
            )
        for reference in rule.output_field_refs:
            _resolve_reference(
                reference,
                rule.source.sheet_key,
                field_candidate_aliases,
                field_source_aliases,
                target_type="FIELD",
                owner=rule.candidate_ref,
                error_code="LLM_RULE_REF_INVALID",
                error_message="规则输出引用了不存在的字段",
            )
        for reference in rule.material_refs:
            _resolve_reference(
                reference,
                rule.source.sheet_key,
                material_candidate_aliases,
                material_source_aliases,
                target_type="MATERIAL",
                owner=rule.candidate_ref,
                error_code="LLM_RULE_REF_INVALID",
                error_message="规则引用了不存在的材料",
            )
        if rule.source_rule_ref:
            _resolve_reference(
                rule.source_rule_ref,
                rule.source.sheet_key,
                rule_candidate_aliases,
                rule_source_aliases,
                target_type="RULE",
                owner=rule.candidate_ref,
                error_code="LLM_RULE_REF_INVALID",
                error_message="风险规则引用了不存在的来源规则",
            )
    for section in report_sections:
        for reference in section.field_refs:
            _resolve_reference(
                reference,
                section.source.sheet_key,
                field_candidate_aliases,
                field_source_aliases,
                target_type="FIELD",
                owner=section.candidate_ref,
                error_code="LLM_REPORT_FIELD_REF_INVALID",
                error_message="报告章节引用了不存在的字段",
            )
    question_targets = {
        "MATERIAL": (material_candidate_aliases, material_source_aliases),
        "FIELD": (field_candidate_aliases, field_source_aliases),
        "RULE": (rule_candidate_aliases, rule_source_aliases),
    }
    question_properties = {
        "MATERIAL": {"name", "required"},
        "FIELD": {"name", "value_type", "unit", "currency", "material_ref", "required", "instruction"},
        "RULE": {"name", "node", "operator", "parameters", "source_rule_ref", "material_refs"},
    }
    question_target_properties: set[tuple[str, str, str]] = set()
    for question in [question for sheet in bundle.sheets for question in sheet.questions]:
        candidate_aliases, source_aliases = question_targets[question.target_type]
        candidate_ref = _resolve_reference(
            question.target_ref,
            question.source.sheet_key,
            candidate_aliases,
            source_aliases,
            target_type=question.target_type,
            owner=question.question_ref,
            error_code="LLM_QUESTION_REF_INVALID",
            error_message="大模型问题引用了不存在的候选对象",
        )
        if question.property not in question_properties[question.target_type]:
            raise BusinessError("LLM_QUESTION_PROPERTY_INVALID", "大模型问题包含不允许裁决的属性", details={"question": question.question_ref})
        target_property = (question.target_type, candidate_ref, question.property)
        if target_property in question_target_properties:
            raise BusinessError(
                "LLM_DUPLICATE_QUESTION_TARGET",
                "同一业务对象的同一属性不能生成多个问题",
                details={"question": question.question_ref, "target_ref": candidate_ref, "property": question.property},
            )
        question_target_properties.add(target_property)
        question.target_ref = candidate_ref


def _canonical_candidate_ref(
    candidate_type: str,
    item: Any,
    source_rows: dict[tuple[str, int], dict[str, Any]],
) -> str:
    source = item.source
    row_cells = source_rows.get((source.sheet_key, source.row_no))
    if row_cells is None:
        raise BusinessError(
            "LLM_SOURCE_REF_INVALID",
            "候选引用的来源行不存在",
            details={"sheet_key": source.sheet_key, "row_no": source.row_no},
        )
    row_hash = hashlib.sha256(
        json.dumps(row_cells, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return f"{candidate_type}:{source.sheet_key}:r{source.row_no}:{row_hash}"


def _build_canonical_reference_map(
    items: list[Any],
    candidate_type: str,
    source_rows: dict[tuple[str, int], dict[str, Any]],
) -> tuple[dict[str, str], dict[tuple[str, str], str], list[tuple[Any, str]]]:
    candidate_aliases: dict[str, str] = {}
    source_aliases: dict[tuple[str, str], str] = {}
    canonical_sources: set[str] = set()
    updates: list[tuple[Any, str]] = []
    for item in items:
        canonical_ref = _canonical_candidate_ref(candidate_type, item, source_rows)
        if canonical_ref in canonical_sources:
            raise BusinessError(
                "LLM_CANDIDATE_SOURCE_COLLISION",
                "同一工作表同一行不能生成多个同类候选",
                details={
                    "candidate_type": candidate_type,
                    "sheet_key": item.source.sheet_key,
                    "row_no": item.source.row_no,
                },
            )
        canonical_sources.add(canonical_ref)
        candidate_ref = item.candidate_ref
        existing = candidate_aliases.get(candidate_ref)
        if existing is not None and existing != canonical_ref:
            raise BusinessError(
                "LLM_REFERENCE_AMBIGUOUS",
                "候选引用指向多个同类候选",
                details={
                    "candidate_type": candidate_type,
                    "reference": candidate_ref,
                    "first_candidate_ref": existing,
                    "second_candidate_ref": canonical_ref,
                },
            )
        candidate_aliases[candidate_ref] = canonical_ref
        if item.source_id:
            scope = (item.source.sheet_key, item.source_id)
            source_existing = source_aliases.get(scope)
            if source_existing is not None and source_existing != canonical_ref:
                raise BusinessError(
                    "LLM_DUPLICATE_SOURCE_ID",
                    "同一工作表内的同类来源编码不能重复",
                    details={
                        "candidate_type": candidate_type,
                        "sheet_key": scope[0],
                        "source_ref": scope[1],
                    },
                )
            source_aliases[scope] = canonical_ref
        updates.append((item, canonical_ref))
    for (sheet_key, source_id), canonical_ref in source_aliases.items():
        candidate_owner = candidate_aliases.get(source_id)
        if candidate_owner is not None and candidate_owner != canonical_ref:
            raise BusinessError(
                "LLM_CANDIDATE_SOURCE_REF_CONFLICT",
                "候选引用不能与另一候选的来源编码冲突",
                details={
                    "candidate_type": candidate_type,
                    "sheet_key": sheet_key,
                    "reference": source_id,
                },
            )
    return candidate_aliases, source_aliases, updates


def _resolve_canonical_reference(
    reference: str,
    owner_sheet_key: str,
    candidate_aliases: dict[str, str],
    source_aliases: dict[tuple[str, str], str],
    *,
    candidate_type: str,
    owner: str,
) -> str:
    canonical_ref = candidate_aliases.get(reference)
    if canonical_ref is not None:
        return canonical_ref
    source_ref = source_aliases.get((owner_sheet_key, reference))
    if source_ref is not None:
        return source_ref
    cross_sheet_matches = {
        canonical_ref
        for (_, source_id), canonical_ref in source_aliases.items()
        if source_id == reference
    }
    if len(cross_sheet_matches) == 1:
        return next(iter(cross_sheet_matches))
    if cross_sheet_matches:
        raise BusinessError(
            "LLM_SOURCE_SCOPE_INVALID",
            "来源编码在多个工作表中重复，无法跨工作表唯一解析，请使用候选引用关联",
            details={
                "candidate_type": candidate_type,
                "reference": reference,
                "owner": owner,
                "owner_sheet_key": owner_sheet_key,
            },
        )
    raise BusinessError(
        "LLM_REFERENCE_NORMALIZATION_INVALID",
        "候选引用无法规范为已验证的内部引用",
        details={
            "candidate_type": candidate_type,
            "reference": reference,
            "owner": owner,
            "owner_sheet_key": owner_sheet_key,
        },
    )


def _canonicalize_report_section_references(
    sections: list[Any],
    source_rows: dict[tuple[str, int], dict[str, Any]],
) -> None:
    seen: set[str] = set()
    updates: list[tuple[Any, str]] = []
    for section in sections:
        canonical_ref = _canonical_candidate_ref("REPORT_SECTION", section, source_rows)
        if canonical_ref in seen:
            raise BusinessError(
                "LLM_CANDIDATE_SOURCE_COLLISION",
                "同一工作表同一行不能生成多个同类候选",
                details={
                    "candidate_type": "REPORT_SECTION",
                    "sheet_key": section.source.sheet_key,
                    "row_no": section.source.row_no,
                },
            )
        seen.add(canonical_ref)
        updates.append((section, canonical_ref))
    for section, canonical_ref in updates:
        section.candidate_ref = canonical_ref


def _canonicalize_question_option_references(
    question: Any,
    *,
    material_candidate_aliases: dict[str, str],
    material_source_aliases: dict[tuple[str, str], str],
    rule_candidate_aliases: dict[str, str],
    rule_source_aliases: dict[tuple[str, str], str],
) -> None:
    if question.target_type == "FIELD" and question.property == "material_ref":
        for option in question.options:
            if not isinstance(option.value, str):
                raise BusinessError(
                    "LLM_QUESTION_OPTION_INVALID",
                    "字段材料引用问题的选项必须是单个材料引用",
                    details={"question": question.question_ref, "property": question.property},
                )
            option.value = _resolve_canonical_reference(
                option.value,
                question.source.sheet_key,
                material_candidate_aliases,
                material_source_aliases,
                candidate_type="MATERIAL",
                owner=question.question_ref,
            )
        return
    if question.target_type != "RULE" or question.property not in {"source_rule_ref", "material_refs"}:
        return
    for option in question.options:
        if question.property == "source_rule_ref":
            if option.value is None:
                continue
            if not isinstance(option.value, str):
                raise BusinessError(
                    "LLM_QUESTION_OPTION_INVALID",
                    "规则来源引用问题的选项必须是规则引用或空值",
                    details={"question": question.question_ref, "property": question.property},
                )
            option.value = _resolve_canonical_reference(
                option.value,
                question.source.sheet_key,
                rule_candidate_aliases,
                rule_source_aliases,
                candidate_type="RULE",
                owner=question.question_ref,
            )
            continue
        if not isinstance(option.value, list) or any(not isinstance(reference, str) for reference in option.value):
            raise BusinessError(
                "LLM_QUESTION_OPTION_INVALID",
                "规则材料范围问题的选项必须是材料引用列表",
                details={"question": question.question_ref, "property": question.property},
            )
        option.value = [
            _resolve_canonical_reference(
                reference,
                question.source.sheet_key,
                material_candidate_aliases,
                material_source_aliases,
                candidate_type="MATERIAL",
                owner=question.question_ref,
            )
            for reference in option.value
        ]


def _canonicalize_candidate_references(bundle: ProposalBundle, ir: WorkbookIR) -> None:
    materials = [item for sheet in bundle.sheets for item in sheet.materials]
    fields = [item for sheet in bundle.sheets for item in sheet.fields]
    rules = [item for sheet in bundle.sheets for item in sheet.rules]
    report_sections = [item for sheet in bundle.sheets for item in sheet.report_sections]
    source_rows = {
        (sheet.sheet_key, row.row_no): row.cells
        for sheet in ir.sheets
        for row in sheet.rows
    }
    material_candidate_aliases, material_source_aliases, material_updates = _build_canonical_reference_map(
        materials,
        "MATERIAL",
        source_rows,
    )
    field_candidate_aliases, field_source_aliases, field_updates = _build_canonical_reference_map(
        fields,
        "FIELD",
        source_rows,
    )
    rule_candidate_aliases, rule_source_aliases, rule_updates = _build_canonical_reference_map(
        rules,
        "RULE",
        source_rows,
    )

    for field in fields:
        field.material_ref = _resolve_canonical_reference(
            field.material_ref,
            field.source.sheet_key,
            material_candidate_aliases,
            material_source_aliases,
            candidate_type="MATERIAL",
            owner=field.candidate_ref,
        )
    for rule in rules:
        for rule_input in rule.inputs:
            rule_input.material_ref = _resolve_canonical_reference(
                rule_input.material_ref,
                rule.source.sheet_key,
                material_candidate_aliases,
                material_source_aliases,
                candidate_type="MATERIAL",
                owner=rule.candidate_ref,
            )
            rule_input.field_ref = _resolve_canonical_reference(
                rule_input.field_ref,
                rule.source.sheet_key,
                field_candidate_aliases,
                field_source_aliases,
                candidate_type="FIELD",
                owner=rule.candidate_ref,
            )
        rule.output_field_refs = [
            _resolve_canonical_reference(
                reference,
                rule.source.sheet_key,
                field_candidate_aliases,
                field_source_aliases,
                candidate_type="FIELD",
                owner=rule.candidate_ref,
            )
            for reference in rule.output_field_refs
        ]
        rule.material_refs = [
            _resolve_canonical_reference(
                reference,
                rule.source.sheet_key,
                material_candidate_aliases,
                material_source_aliases,
                candidate_type="MATERIAL",
                owner=rule.candidate_ref,
            )
            for reference in rule.material_refs
        ]
        if rule.source_rule_ref:
            rule.source_rule_ref = _resolve_canonical_reference(
                rule.source_rule_ref,
                rule.source.sheet_key,
                rule_candidate_aliases,
                rule_source_aliases,
                candidate_type="RULE",
                owner=rule.candidate_ref,
            )
    for section in report_sections:
        section.field_refs = [
            _resolve_canonical_reference(
                reference,
                section.source.sheet_key,
                field_candidate_aliases,
                field_source_aliases,
                candidate_type="FIELD",
                owner=section.candidate_ref,
            )
            for reference in section.field_refs
        ]
    aliases_by_target_type = {
        "MATERIAL": (material_candidate_aliases, material_source_aliases),
        "FIELD": (field_candidate_aliases, field_source_aliases),
        "RULE": (rule_candidate_aliases, rule_source_aliases),
    }
    for sheet in bundle.sheets:
        for question in sheet.questions:
            candidate_aliases, source_aliases = aliases_by_target_type[question.target_type]
            question.target_ref = _resolve_canonical_reference(
                question.target_ref,
                question.source.sheet_key,
                candidate_aliases,
                source_aliases,
                candidate_type=question.target_type,
                owner=question.question_ref,
            )
            _canonicalize_question_option_references(
                question,
                material_candidate_aliases=material_candidate_aliases,
                material_source_aliases=material_source_aliases,
                rule_candidate_aliases=rule_candidate_aliases,
                rule_source_aliases=rule_source_aliases,
            )

    for item, canonical_ref in [*material_updates, *field_updates, *rule_updates]:
        item.candidate_ref = canonical_ref
    _canonicalize_report_section_references(report_sections, source_rows)
