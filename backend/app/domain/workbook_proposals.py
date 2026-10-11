"""任意布局的规则库 → LLM 候选；校验真实来源与执行引用，不匹配固定表头。"""

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
import re
from typing import Any, Literal
from types import SimpleNamespace

from pydantic import Field, PrivateAttr, ValidationError

from ..common.exceptions import BusinessError
from .manifest import (
    draft_config_of_import,
    refresh_calculation_references,
)
from .proposals import (
    CalculationCandidate, FieldProposal, MaterialProposal,
    ProposalBundle, ReportSectionProposal, RuleProposal, SourceRef, StrictModel,
    CoverageRow, DisabledSourceColumn, DisabledSourceRow, coverage_totals,
)
from .workbook import WorkbookIR, WorkbookSheet


class ModelMaterial(MaterialProposal):
    config_status: Literal["CLEAR", "PENDING"]


class ModelField(FieldProposal):
    config_status: Literal["CLEAR", "PENDING", "DISABLED"]


class ModelRule(RuleProposal):
    config_status: Literal["CLEAR", "PENDING", "DISABLED"]


class ModelReportSection(ReportSectionProposal):
    config_status: Literal["CLEAR", "PENDING", "DISABLED"]


class ModelCalculationParameter(StrictModel):
    model_config = {"extra": "allow"}
    parameter_code: str
    source_type: str
    source_code: str | None = None
    source: SourceRef




class ModelCalculation(CalculationCandidate):
    parameters: list[ModelCalculationParameter] = Field(default_factory=list)


class ModelSheet(StrictModel):
    sheet_key: str
    materials: list[ModelMaterial] = Field(default_factory=list)
    fields: list[ModelField] = Field(default_factory=list)
    rules: list[ModelRule] = Field(default_factory=list)
    report_sections: list[ModelReportSection] = Field(default_factory=list)
    disabled_rows: list[DisabledSourceRow] = Field(default_factory=list)
    # 表头、说明、停用行、多行合并等处置由模型按内容声明；后端核对全部非空行。
    row_dispositions: list[CoverageRow] = Field(default_factory=list)


class WorkbookProposals(StrictModel):
    sheets: list[ModelSheet]
    calculations: list[ModelCalculation] = Field(default_factory=list)
    _candidate_origins: dict[int, tuple[Any, str]] = PrivateAttr(default_factory=dict)


_COLLECTION_MODELS = {
    "materials": (ModelMaterial, "MATERIAL"), "fields": (ModelField, "FIELD"),
    "rules": (ModelRule, "RULE"), "report_sections": (ModelReportSection, "REPORT_SECTION"),
    "disabled_rows": (DisabledSourceRow, "DISABLED_ROW"),
    "row_dispositions": (CoverageRow, "ROW_DISPOSITION"),
}


def _rejected(raw: Any, kind: str, path: str, exc: BusinessError | ValidationError) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    if isinstance(exc, ValidationError):
        details = {"mismatches": exc.errors(include_url=False, include_input=False, include_context=False)}
        message = "；".join(
            f"{'.'.join(str(part) for part in error['loc']) or '候选'}：{error['msg']}"
            for error in details["mismatches"]
        )
        code = "CANDIDATE_STRUCTURE_INVALID"
    else:
        code, message, details = exc.code, exc.message, exc.details
    return {"kind": kind, "path": path, "candidate_ref": payload.get("candidate_ref"),
            "name": payload.get("name") or payload.get("rule_name") or payload.get("section_name") or payload.get("title"),
            "source": payload.get("source"), "code": code, "message": message,
            "details": details, "raw": deepcopy(raw)}


def parse_workbook_proposals(response: Any) -> tuple[WorkbookProposals, list[dict[str, Any]]]:
    """外层响应必须可读；逐项接收候选，错误项明确记录，绝不补造或改写候选。"""
    if not isinstance(response, dict) or not isinstance(response.get("sheets"), list):
        raise _invalid("模型响应必须包含工作表列表")
    if "calculations" in response and not isinstance(response["calculations"], list):
        raise _invalid("模型响应的计算集合必须是列表")
    try:
        proposals = WorkbookProposals.model_validate({**response, "sheets": [], "calculations": []})
        rejected = []
        for sheet_index, raw_sheet in enumerate(response["sheets"]):
            if not isinstance(raw_sheet, dict):
                raise _invalid("模型工作表必须是对象", sheet_index=sheet_index)
            sheet = ModelSheet.model_validate({**raw_sheet, **{key: [] for key in _COLLECTION_MODELS}})
            for collection, (model, kind) in _COLLECTION_MODELS.items():
                raw_items = raw_sheet.get(collection, [])
                if not isinstance(raw_items, list):
                    raise _invalid("模型候选集合必须是列表", sheet_key=sheet.sheet_key, collection=collection)
                for index, raw in enumerate(raw_items):
                    try:
                        if isinstance(raw, dict) and raw.get("config_status") == "DISABLED":
                            sheet.disabled_rows.append(DisabledSourceRow(
                                source=SourceRef.model_validate(raw.get("source")), sheet_name=sheet.sheet_key,
                                source_id=raw.get("source_id") or raw.get("rule_code"), columns=[],
                                raw_configuration=deepcopy(raw),
                            ))
                            continue
                        candidate = model.model_validate(raw)
                    except (ValidationError, BusinessError) as exc:
                        rejected.append(_rejected(raw, kind, f"sheets.{sheet_index}.{collection}.{index}", exc))
                    else:
                        getattr(sheet, collection).append(candidate)
                        proposals._candidate_origins[id(candidate)] = (deepcopy(raw), f"sheets.{sheet_index}.{collection}.{index}")
            proposals.sheets.append(sheet)
        for index, raw in enumerate(response.get("calculations", [])):
            try:
                if isinstance(raw, dict) and raw.get("status") == "DISABLED":
                    source = SourceRef.model_validate(raw.get("source"))
                    sheet = next((item for item in proposals.sheets if item.sheet_key == source.sheet_key), None)
                    if sheet is None:
                        raise _invalid("停用计算的来源工作表不存在", source=source.model_dump())
                    sheet.disabled_rows.append(DisabledSourceRow(
                        source=source, sheet_name=sheet.sheet_key, source_id=raw.get("rule_code"),
                        columns=[], raw_configuration=deepcopy(raw),
                    ))
                    continue
                candidate = ModelCalculation.model_validate(raw)
            except (ValidationError, BusinessError) as exc:
                rejected.append(_rejected(raw, "CALC_RULE", f"calculations.{index}", exc))
            else:
                proposals.calculations.append(candidate)
                proposals._candidate_origins[id(candidate)] = (deepcopy(raw), f"calculations.{index}")
    except ValidationError as exc:
        raise _invalid("模型返回的工作表或响应外层结构无效", mismatches=exc.errors(
            include_url=False, include_input=False, include_context=False)) from exc
    return proposals, rejected


@dataclass
class PreparedSheet:
    sheet: WorkbookSheet
    edited_rows: dict[int, list[dict[str, Any]]] = dataclass_field(default_factory=dict)

    def model_input(self) -> dict[str, Any]:
        result = {
            "sheet_key": self.sheet.sheet_key,
            "sheet_name": self.sheet.name,
            "merged_ranges": self.sheet.merged_ranges,
            "rows": [{"row_no": row.row_no, "cells": row.cells} for row in self.sheet.rows],
        }
        if self.edited_rows:
            result["edited_rows"] = [
                {"row_no": row_no, "columns": columns, "activation_requested": True}
                for row_no, columns in sorted(self.edited_rows.items())
            ]
        return result


def existing_references(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """只读草稿身份供增量候选引用；不把已有对象当作本次生成候选。"""
    result = {}
    for section, kind, key in (
        ("materials", "MATERIAL", "material_key"),
        ("fields", "FIELD", "field_key"),
        ("rules", "RULE", "rule_key"),
    ):
        for item in config[section]:
            values = {**item, "candidate_ref": item[key]}
            if kind == "FIELD":
                values["material_ref"] = item["material_key"]
            result[item[key]] = {"kind": kind, "value": SimpleNamespace(**values)}
    return result


def existing_calculations(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config["calculation_json"]
    if not isinstance(raw, list):
        raise BusinessError("RULE_LIBRARY_CALCULATION_FORMAT_INVALID", "计算配置必须是原始定义列表")
    return raw


def _invalid(message: str, **details: Any) -> BusinessError:
    return BusinessError("LLM_CONTRACT_INVALID", message, status_code=502, details=details)


def prepare_sheets(ir: WorkbookIR, bindings: dict[str, dict[str, Any]]) -> dict[str, PreparedSheet]:
    sheets: dict[str, PreparedSheet] = {}
    for sheet in ir.sheets:
        binding = bindings[sheet.sheet_key]
        if not binding["excluded"]:
            sheets[sheet.sheet_key] = PreparedSheet(sheet)
    if not sheets:
        raise BusinessError("IMPORT_NO_BOUND_SHEET", "本次导入没有选入任何工作表")
    return sheets


def _source(source: SourceRef, sheets: dict[str, PreparedSheet], *, sheet_key: str | None = None) -> None:
    if source.sheet_key not in sheets or (sheet_key is not None and source.sheet_key != sheet_key):
        raise _invalid("候选引用了未选入的工作表", source=source.model_dump())
    sheet = sheets[source.sheet_key].sheet
    if source.row_no not in {row.row_no for row in sheet.rows}:
        raise _invalid("候选来源行不存在", source=source.model_dump())
    cells = {address: value for row in sheet.rows for address, value in row.cells.items() if value is not None and str(value).strip()}
    if len(source.cells) != len(set(source.cells)) or any(cell not in cells for cell in source.cells):
        raise _invalid("候选引用了不存在、空白或重复的单元格", source=source.model_dump())
    if not any(int(re.search(r"\d+$", cell).group()) == source.row_no for cell in source.cells):
        raise _invalid("候选主要来源行与引用单元格不一致", source=source.model_dump())
    source.texts = {cell: str(cells[cell]) for cell in source.cells}


def _validate_references(
    proposals: WorkbookProposals, candidates: dict[str, dict[str, Any]],
    references: dict[str, dict[str, Any]],
    sheets: dict[str, PreparedSheet],
) -> None:
    def resolve(ref: str, kind: str) -> Any:
        candidate = candidates.get(ref) or references.get(ref)
        if candidate is None or candidate["kind"] != kind:
            raise _invalid("模型候选引用不存在或类型不一致", candidate_ref=current_ref, reference=ref, target_type=kind)
        return candidate["value"]

    for entry in candidates.values():
        candidate, kind = entry["value"], entry["kind"]
        current_ref = candidate.candidate_ref
        if getattr(candidate, "enabled", True) is False or getattr(candidate, "config_status", None) == "DISABLED":
            continue
        if kind == "FIELD":
            resolve(candidate.material_ref, "MATERIAL")
        if kind in {"RULE", "REPORT_SECTION"}:
            for material_ref in candidate.material_refs:
                resolve(material_ref, "MATERIAL")
        if kind == "REPORT_SECTION":
            for field_ref in candidate.field_refs:
                field = resolve(field_ref, "FIELD")
        if kind != "RULE":
            continue
        for item in candidate.inputs:
            field = resolve(item.field_ref, "FIELD")
            resolve(item.material_ref, "MATERIAL")
        for ref in candidate.output_field_refs:
            resolve(ref, "FIELD")
        for ref in [*candidate.source_rule_refs, *([candidate.source_rule_ref] if candidate.source_rule_ref else [])]:
            resolve(ref, "RULE")

def _assemble_bundle(
    proposals: WorkbookProposals, sheets: dict[str, PreparedSheet],
    *, existing_config: dict[str, Any] | None = None, rejected: list[dict[str, Any]],
) -> ProposalBundle:
    references = existing_references(existing_config) if existing_config is not None else {}
    candidates: dict[str, dict[str, Any]] = {}
    counts: Counter = Counter()
    seen: set[str] = set()

    def register(candidate: Any, kind: str, sheet_key: str | None = None) -> None:
        try:
            _source(candidate.source, sheets, sheet_key=sheet_key)
        except BusinessError as exc:
            raise _invalid(exc.message, **{**exc.details, "candidate_ref": candidate.candidate_ref}) from exc
        if not candidate.candidate_ref or candidate.candidate_ref in candidates or candidate.candidate_ref in references:
            raise _invalid("候选引用为空或重复", candidate_ref=candidate.candidate_ref)
        candidates[candidate.candidate_ref] = {"kind": kind, "value": candidate}
        counts[(candidate.source.sheet_key, candidate.source.row_no)] += 1
        # source_id 只保留 Excel 中真实存在的业务编号；内部身份用 candidate_ref。
        source_id = getattr(candidate, "source_id", None)
        # 编辑值单独保存，不能伪装成 Excel 原文；编号也可来自管理员实际提交的编辑。
        texts = [*candidate.source.texts.values(), *(
            str(column["value"])
            for column in sheets[candidate.source.sheet_key].edited_rows.get(candidate.source.row_no, [])
        )]
        if kind == "RULE" and candidate.rule_code and not any(candidate.rule_code in text for text in texts):
            raise _invalid("规则业务编号没有真实单元格来源", candidate_ref=candidate.candidate_ref)
        if source_id and not any(source_id in text for text in texts):
            raise _invalid("业务编号没有真实单元格来源", candidate_ref=candidate.candidate_ref, source_id=source_id)

    for sheet in proposals.sheets:
        if sheet.sheet_key not in sheets or sheet.sheet_key in seen:
            raise _invalid("返回了未知或重复的工作表", sheet_key=sheet.sheet_key)
        seen.add(sheet.sheet_key)
        for collection, kind in (("materials", "MATERIAL"), ("fields", "FIELD"), ("rules", "RULE"), ("report_sections", "REPORT_SECTION")):
            for candidate in getattr(sheet, collection):
                register(candidate, kind, sheet.sheet_key)
        for row in sheet.disabled_rows:
            try:
                _source(row.source, sheets, sheet_key=sheet.sheet_key)
                row.sheet_name = sheets[sheet.sheet_key].sheet.name
                if row.raw_configuration is not None:
                    row.columns = [
                        DisabledSourceColumn(key=cell, label=cell, value=text, cell=cell)
                        for cell, text in row.source.texts.items()
                    ]
            except BusinessError as exc:
                raise _invalid(exc.message, **{**exc.details, "item_kind": "DISABLED_ROW"}) from exc
    if seen != set(sheets):
        raise _invalid("遗漏了选入的工作表", missing_sheet_keys=sorted(set(sheets) - seen))
    for candidate in proposals.calculations:
        register(candidate, "CALC_RULE")
        for collection in ("parameters",):
            for entry in getattr(candidate, collection):
                try:
                    _source(entry.source, sheets)
                except BusinessError as exc:
                    raise _invalid(exc.message, **{**exc.details, "candidate_ref": candidate.candidate_ref}) from exc
                counts[(entry.source.sheet_key, entry.source.row_no)] += 1
    _validate_references(proposals, candidates, references, sheets)
    calculations = refresh_calculation_references([item.model_dump(mode="json") for item in proposals.calculations])
    field_codes = {entry["value"].source_id or entry["value"].candidate_ref for entry in candidates.values() if entry["kind"] == "FIELD"}
    result_codes = {item["result_code"] for item in calculations}
    available_field_codes = field_codes | {
        str(code)
        for item in (existing_config["fields"] if existing_config is not None else [])
        for code in (item["field_key"], item.get("source_ref")) if code
    }
    available_result_codes = result_codes | {
        item["result_code"]
        for item in (existing_calculations(existing_config) if existing_config is not None else [])
        if item.get("status") != "DISABLED"
    }
    for entry in candidates.values():
        if entry["kind"] == "RULE" and getattr(entry["value"], "config_status", None) != "DISABLED":
            unknown = set(entry["value"].source_rule_codes) - available_result_codes
            if unknown:
                raise _invalid("风险规则引用的计算结果不存在", candidate_ref=entry["value"].candidate_ref, references=sorted(unknown))
    if len(result_codes) != len(calculations) or len({item["rule_code"] for item in calculations}) != len(calculations):
        raise _invalid("计算规则或结果编号重复")
    for calculation in calculations:
        if calculation.get("status") == "DISABLED":
            continue
        for parameter in calculation["parameters"]:
            allowed = available_field_codes if parameter["source_type"] == "FIELD" else available_result_codes
            if parameter["source_type"] != "CONSTANT" and parameter["source_code"] not in allowed:
                raise _invalid("计算引用的字段或上游结果不存在", candidate_ref=calculation["candidate_ref"], rule_code=calculation["rule_code"], parameter=parameter["parameter_code"])
    output = []
    coverage = []
    for model_sheet in proposals.sheets:
        raw = sheets[model_sheet.sheet_key].sheet
        rows = {row.row_no for row in raw.rows if any(value is not None and str(value).strip() for value in row.cells.values())}
        dispositions = {item.row_no: item for item in model_sheet.row_dispositions}
        for row_no in set(dispositions) - rows:
            raise _invalid("行处置引用了不存在的非空行", sheet_key=model_sheet.sheet_key,
                           row_no=row_no, item_kind="ROW_DISPOSITION")
        coverage_rows = []
        disabled = {item.source.row_no for item in model_sheet.disabled_rows}
        for row_no in sorted(rows):
            key = (model_sheet.sheet_key, row_no)
            declaration = dispositions.get(row_no)
            if counts[key]:
                disposition, reason = "CANDIDATE", None
            elif row_no in disabled:
                disposition, reason = "DISABLED", "原始停用配置已保留"
            elif declaration is not None and declaration.merged_into in candidates:
                disposition, reason = "CANDIDATE", declaration.reason
            elif declaration is not None and declaration.disposition == "EXCLUDED" and declaration.reason:
                disposition, reason = "EXCLUDED", declaration.reason
            else:
                disposition, reason = "UNPARSED", "该行未形成候选、合并对象或明确排除说明；保留原文，未导入该行配置"
                if not any(isinstance(item.get("source"), dict)
                           and item["source"].get("sheet_key") == raw.sheet_key
                           and item["source"].get("row_no") == row_no for item in rejected):
                    source_row = next(item for item in raw.rows if item.row_no == row_no)
                    rejected.append({"kind": "SOURCE_ROW", "path": f"workbook.{raw.sheet_key}.rows.{row_no}",
                                     "name": f"{raw.name} 第 {row_no} 行", "candidate_ref": None,
                                     "source": {"sheet_key": raw.sheet_key, "row_no": row_no,
                                                "cells": list(source_row.cells)},
                                     "code": "ROW_NOT_PARSED", "message": reason,
                                     "details": {}, "raw": dict(source_row.cells)})
            rejected_notes = [item["message"] for item in rejected
                              if isinstance(item.get("source"), dict)
                              and item["source"].get("sheet_key") == raw.sheet_key
                              and item["source"].get("row_no") == row_no]
            if rejected_notes and not counts[key]:
                disposition, reason = "UNPARSED", "该行有候选未导入，具体原因见未导入项目"
            coverage_rows.append({"row_no": row_no, "disposition": disposition, "reason": reason,
                                  "candidate_count": counts[key],
                                  "merged_into": declaration.merged_into if declaration else None,
                                  "notes": rejected_notes})
        output.append({**model_sheet.model_dump(mode="json", exclude={"row_dispositions"}), "suggested_role": "RULE_LIBRARY:" + raw.sheet_key, "column_mapping": {}})
        coverage.append({"sheet_key": raw.sheet_key, "sheet_name": raw.name, "business_rows": len(rows), "rows": coverage_rows})
    bundle = ProposalBundle.model_validate({"sheets": output, "calculations": calculations,
                                           "coverage": {"sheets": coverage, "totals": coverage_totals(coverage),
                                                        "rejected_candidates": rejected}})
    return bundle


def assemble_bundle(
    proposals: WorkbookProposals, sheets: dict[str, PreparedSheet],
    *, existing_config: dict[str, Any] | None = None,
    rejected: list[dict[str, Any]] | None = None,
) -> ProposalBundle:
    """逐项暴露配置错误并隔离依赖项；每次只移除已明确失败的候选，不修造业务内容。"""
    rejected = list(rejected or [])

    def entries() -> list[tuple[list[Any], Any, str, str]]:
        result = []
        for sheet_index, sheet in enumerate(proposals.sheets):
            for collection, (_, kind) in _COLLECTION_MODELS.items():
                items = getattr(sheet, collection)
                result.extend((items, item, kind, f"sheets.{sheet_index}.{collection}.{index}")
                              for index, item in enumerate(items))
        result.extend((proposals.calculations, item, "CALC_RULE", f"calculations.{index}")
                      for index, item in enumerate(proposals.calculations))
        return result

    def reject(items: list[tuple[list[Any], Any, str, str]], exc: BusinessError) -> None:
        for collection, item, kind, path in items:
            raw, path = proposals._candidate_origins.get(id(item), (item.model_dump(mode="json"), path))
            rejected.append(_rejected(raw, kind, path, exc))
            collection[:] = [candidate for candidate in collection if candidate is not item]

    # 同一身份的冲突项全部标错，不按返回顺序任意选一项当正确配置。
    groups: dict[tuple[Any, ...], list[tuple[list[Any], Any, str, str]]] = {}
    for entry in entries():
        collection, item, kind, path = entry
        keys = []
        if hasattr(item, "candidate_ref"):
            keys.append(("candidate_ref", item.candidate_ref))
        if kind == "CALC_RULE":
            keys.extend((key, getattr(item, key)) for key in ("rule_code", "result_code"))
        if kind == "ROW_DISPOSITION":
            keys.append(("row_disposition", path.split(".")[1], item.row_no))
        # 材料来源编号允许共享；具体对象始终由唯一 candidate_ref 引用。
        if kind in {"FIELD", "RULE"} and item.source_id:
            keys.append(("source_id", kind, item.source.sheet_key, item.source_id))
        for key in keys:
            groups.setdefault(key, []).append(entry)
    conflicts = {}
    for key, group in groups.items():
        if len(group) < 2:
            continue
        if key[0] == "source_id" and len({item.source.row_no for _, item, _, _ in group}) == 1:
            continue
        for entry in group:
            conflicts[id(entry[1])] = entry
    if conflicts:
        reject(list(conflicts.values()), _invalid("候选身份、业务编号或行处置重复，无法唯一定位；冲突项均未导入"))

    while True:
        try:
            bundle = _assemble_bundle(proposals, sheets, existing_config=existing_config, rejected=rejected)
            # 使用真正的草稿投影核对身份与引用，避免候选接收后在写草稿时整批失败。
            # 此内部命名空间只用于核对键是否冲突，不写入任何场景或生成业务默认值。
            draft_config_of_import(
                proposals=bundle.model_dump(mode="json"), namespace="candidate-acceptance",
                selected_sheets=[{"sheet_key": sheet.sheet_key, "sheet_name": sheets[sheet.sheet_key].sheet.name,
                                  "sheet_role": "RULE_LIBRARY:" + sheet.sheet_key, "node_code": None}
                                 for sheet in proposals.sheets],
                import_job_no="", existing_config=existing_config,
            )
            return bundle
        except BusinessError as exc:
            details = exc.details
            failed = []
            for entry in entries():
                _, item, kind, _ = entry
                if (details.get("candidate_ref") is not None
                        and getattr(item, "candidate_ref", None) == details["candidate_ref"]):
                    failed.append(entry)
                elif getattr(item, "candidate_ref", None) in details.get("candidate_refs", []):
                    failed.append(entry)
                elif any(getattr(item, "candidate_ref", None) in refs for refs in details.get("conflicting_refs", [])):
                    failed.append(entry)
                elif (kind == "DISABLED_ROW" and details.get("item_kind") == kind
                      and item.source.model_dump() == details.get("source")):
                    failed.append(entry)
                elif (kind == "ROW_DISPOSITION" and details.get("item_kind") == kind
                      and item.row_no == details.get("row_no")
                      and proposals.sheets[int(entry[3].split(".")[1])].sheet_key == details.get("sheet_key")):
                    failed.append(entry)
                elif (details.get("rule_code") is not None and kind == "CALC_RULE"
                      and item.rule_code == details["rule_code"]):
                    failed.append(entry)
                elif (details.get("source_ref") is not None
                      and getattr(item, "source_id", None) == details["source_ref"]
                      and (details.get("target_type") is None or kind == details["target_type"])
                      and item.source.sheet_key == details.get("sheet_key")):
                    failed.append(entry)
            if not failed:
                # 响应整体、工作表范围或程序错误仍直接暴露，不把未知异常当作单项错误吞掉。
                raise
            reject(failed, exc)
