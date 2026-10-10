"""自由规则库停用来源：增量解析、预览，确认后追加到当前草稿。

预览文件只保存本次新增候选与配置摘要，不复制整个草稿；现有配置仅保存指纹。
模型调用不持有写锁，不在确认时再次生成提案，也不重导或覆盖已有配置。
"""

from copy import deepcopy
from collections import defaultdict
import hashlib
import json
from typing import Any
from uuid import uuid4

from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.exceptions import BusinessError
from ..domain import manifest
from ..domain.workbook import WorkbookRow, WorkbookSheet
from ..domain.workbook_proposals import PreparedSheet, existing_calculations
from ..entity import ScenarioDraft, ScenarioDraftMaterial, ScenarioDraftField, ScenarioDraftRule, ScenarioDraftSection
from ..repositories import configuration_store as config_store
from .llm_service import convert_prepared_sheets
from .storage_service import resolve_storage_path


def _fingerprint(config: dict[str, Any], schema: dict[str, Any], profile_version: str | None) -> str:
    content = json.dumps([config, schema, profile_version], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _revision(draft: ScenarioDraft, expected_revision: int) -> None:
    if draft.revision_no != expected_revision:
        raise BusinessError(
            "SCENARIO_DRAFT_REVISION_CONFLICT", "场景草稿已变化，请核对最新配置后重新解析",
            status_code=409, details={"expected": expected_revision, "actual": draft.revision_no},
        )


def _source_rows(config: dict[str, Any], row_keys: list[str]) -> list[dict[str, Any]]:
    by_key = {item["row_key"]: item for item in config["disabled_source_rows"]}
    rows = []
    for key in row_keys:
        if key not in by_key:
            raise BusinessError("DISABLED_SOURCE_ROW_NOT_FOUND", "所选停用来源行不存在", status_code=404)
        row = by_key[key]
        rows.append(row)
    if not rows or len(set(row_keys)) != len(row_keys):
        raise BusinessError("DISABLED_SOURCE_SELECTION_INVALID", "请选择不重复的停用来源行")
    return rows


def _sheets(rows: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> dict[str, PreparedSheet]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["source"]["sheet_key"]].append(row)
    result = {}
    for sheet_key, sources in grouped.items():
        cells_by_row: dict[int, dict[str, Any]] = defaultdict(dict)
        maximum_column = 0
        for source_row in sources:
            for address, text in source_row["source"]["texts"].items():
                column, row_no = coordinate_from_string(address)
                maximum_column = max(maximum_column, column_index_from_string(column))
                previous = cells_by_row[row_no].get(address)
                if previous is not None and previous != text:
                    raise BusinessError("DISABLED_SOURCE_CELL_CONFLICT", "所选来源对同一单元格保存了不同原文", status_code=409)
                cells_by_row[row_no][address] = text
        workbook_rows = [WorkbookRow(row_no=row_no, cells=cells, cell_details=[]) for row_no, cells in sorted(cells_by_row.items())]
        sheet = WorkbookSheet(
            sheet_id=sheet_key, sheet_key=sheet_key, name=sources[0]["sheet_name"],
            original_name=sources[0]["sheet_name"], visible=True, state="DATA",
            max_row=max(cells_by_row), max_column=maximum_column, non_empty_rows=len(workbook_rows),
            used_range=None, merged_ranges=[], header_candidates=[], rows=workbook_rows,
            preview_rows=[], rows_truncated=False, columns_truncated=False, cells_truncated=False,
        )
        result[sheet_key] = PreparedSheet(
            sheet, nodes, {row["source"]["row_no"]: row["columns"] for row in sources},
        )
    return result


def _reference_context(config: dict[str, Any]) -> dict[str, Any]:
    # 不把原模型提案、操作元数据或无关草稿内容重复发给模型。
    return {
        "materials": [{key: item.get(key) for key in ("material_key", "source_ref", "name", "required", "description")} for item in config["materials"]],
        "fields": [{key: item.get(key) for key in (
            "field_key", "source_ref", "material_key", "name", "value_type", "unit", "currency",
            "period_scope", "statement_scope", "extraction_strategy", "structure_schema_json", "node_code",
        )} for item in config["fields"]],
        "rules": [{key: item.get(key) for key in (
            "rule_key", "source_ref", "name", "node_code", "operator_code", "enabled",
        )} | {"definition": deepcopy(item["rule_json"])} for item in config["rules"]],
        "calculation_json": deepcopy(config["calculation_json"]),
    }


def _merge_calculations(existing: Any, additions: list[dict[str, Any]]) -> Any:
    if not additions:
        return deepcopy(existing)
    if isinstance(existing, dict):
        old = manifest.calculation_definitions_of_frozen(existing["frozen"])
    else:
        old = existing or []
    for key in ("rule_code", "result_code"):
        identifiers = [item[key] for item in [*old, *additions]]
        if len(set(identifiers)) != len(identifiers):
            raise BusinessError("DISABLED_CALC_CODE_DUPLICATE", "新增计算与已有计算的规则或结果编号重复", status_code=409)
    return [*deepcopy(old), *deepcopy(additions)]


def _assert_additions(config: dict[str, Any], delta: dict[str, Any]) -> None:
    for section, identity in (
        ("materials", "material_key"), ("fields", "field_key"), ("rules", "rule_key"), ("sections", "section_key"),
    ):
        old_keys = {item[identity] for item in config[section]}
        for item in delta[section]:
            if item[identity] in old_keys:
                raise BusinessError("DISABLED_SOURCE_CONFIG_DUPLICATE", "新增配置身份已存在，不能覆盖已有编辑", status_code=409,
                                    details={"name": item.get("name") or item.get("title"), "target_type": section})
            old_keys.add(item[identity])
            if section == "materials" and any(previous["name"] == item["name"] for previous in config[section]):
                raise BusinessError("DISABLED_MATERIAL_DUPLICATE", "材料名称已存在，请引用已有材料", status_code=409)
            if section == "fields" and any(
                (previous.get("source_ref") and previous.get("source_ref") == item.get("source_ref"))
                or (previous["material_key"] == item["material_key"] and previous["name"] == item["name"])
                for previous in config[section]
            ):
                raise BusinessError("DISABLED_FIELD_DUPLICATE", "字段编号或同材料下的字段名称已存在，请引用已有字段", status_code=409)
    _merge_calculations(config["calculation_json"], delta["calculation_json"] or [])


def _items(bundle: dict[str, Any], config: dict[str, Any]) -> list[dict[str, str]]:
    names = {item[key]: item["name"] for section, key in (("materials", "material_key"), ("fields", "field_key"), ("rules", "rule_key")) for item in config[section]}
    names.update({item["source_ref"]: item["name"] for item in config["fields"] if item.get("source_ref")})
    for sheet in bundle["sheets"]:
        for section in ("materials", "fields", "rules"):
            names.update({item["candidate_ref"]: item["name"] for item in sheet[section]})
        names.update({item["source_id"]: item["name"] for item in sheet["fields"] if item.get("source_id")})
    names.update({item["result_code"]: item["rule_name"] for item in existing_calculations(config)})
    names.update({item["result_code"]: item["rule_name"] for item in bundle["calculations"]})
    items = []
    for sheet in bundle["sheets"]:
        for material in sheet["materials"]:
            items.append({"kind": "材料", "name": material["name"], "description": ("必需；" if material["required"] else "可选；") + material["description"]})
        for field in sheet["fields"]:
            items.append({"kind": "字段", "name": field["name"], "description": "；".join([
                names[field["material_ref"]], field["value_type"], field["unit"] or "未声明单位",
                field["currency"] or "未声明币种", field["period_scope"], field["statement_scope"], field["instruction"],
                *(["结构：" + json.dumps(field["structure_schema"], ensure_ascii=False)] if field.get("structure_schema") else []),
            ])})
        for rule in sheet["rules"]:
            inputs = "、".join(names[item["field_ref"]] for item in rule["inputs"])
            sources = "、".join(names[ref] for ref in [*rule["source_rule_refs"], *rule["source_rule_codes"], *([rule["source_rule_ref"]] if rule["source_rule_ref"] else [])])
            items.append({"kind": "规则", "name": rule["name"], "description": "；".join([
                manifest.OPERATOR_LABELS.get(rule["operator"], rule["operator"]), rule["requirement"],
                inputs, sources, rule["risk_condition"] or "", json.dumps(rule["parameters"], ensure_ascii=False),
            ])})
        for section in sheet["report_sections"]:
            items.append({"kind": "报告章节", "name": section["section_name"], "description": "；".join([
                "、".join(names[ref] for ref in section["field_refs"]), section["content_instruction"],
            ])})
    for calculation in bundle["calculations"]:
        parameters = "；".join(
            parameter["parameter_code"] + "=" + (str(parameter.get("constant_value")) if parameter["source_type"] == "CONSTANT" else names.get(parameter["source_code"], parameter["source_code"]))
            for parameter in calculation["parameters"]
        )
        items.append({"kind": "计算", "name": calculation["rule_name"], "description": "；".join([
            "仅计算" if calculation["execution_mode"] == "VALUE_ONLY" else "计算并判定", calculation["formula"], parameters,
            "判断标准：" + calculation.get("judgement_instruction", ""),
            calculation["unit"] or "未声明单位", calculation["currency"] or "未声明币种",
            "区间：" + json.dumps([{key: value for key, value in band.items() if key != "source"} for band in calculation["bands"]], ensure_ascii=False),
        ])})
    return items


def preview(
    db: Session,
    scenario_code: str,
    row_keys: list[str],
    expected_revision: int,
    expected_config_fingerprint: str | None = None,
    *, operation_no: str,
) -> dict[str, Any]:
    from .scenario_admin_service import template_or_404
    template_or_404(db, scenario_code)
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == scenario_code))
    if draft is None:
        raise BusinessError("DISABLED_SOURCE_ROW_NOT_FOUND", "场景没有停用来源行", status_code=404)
    _revision(draft, expected_revision)
    config, schema, profile_version = deepcopy(draft.config_json), deepcopy(draft.schema_json), draft.profile_version
    sources = deepcopy(_source_rows(config, row_keys))
    fingerprint = _fingerprint(config, schema, profile_version)
    if expected_config_fingerprint is not None and expected_config_fingerprint != fingerprint:
        raise BusinessError(
            "SCENARIO_SOURCE_INPUT_STALE",
            "来源解析命令对应的草稿配置已变化，拒绝按过期配置调用模型",
            status_code=409,
            details={"scenario_code": scenario_code, "expected": expected_config_fingerprint, "actual": fingerprint},
        )
    db.commit()  # 模型调用期间不保留数据库事务或写锁。
    bundle, _ = convert_prepared_sheets(
        scenario_code, _sheets(sources, config["nodes"]), operation_no=operation_no,
        existing_config=_reference_context(config),
    )
    proposals = bundle.model_dump(mode="json")
    questions: list[str] = []
    if (any(sheet["disabled_rows"] for sheet in proposals["sheets"])
            or any(rule.get("config_status") == "DISABLED" for sheet in proposals["sheets"] for rule in sheet["rules"])):
        raise BusinessError("DISABLED_SOURCE_ACTIVATION_INCOMPLETE", "请求启用的来源仍被模型保留为停用，草稿未修改", status_code=502)
    coverage = {(sheet["sheet_key"], row["row_no"]): row for sheet in proposals["coverage"]["sheets"] for row in sheet["rows"]}
    for source in sources:
        row = coverage[(source["source"]["sheet_key"], source["source"]["row_no"])]
        if row["disposition"] not in {"CANDIDATE", "QUESTION"}:
            raise BusinessError("DISABLED_SOURCE_ACTIVATION_INCOMPLETE", "请求启用的业务行没有转换为候选或问题", status_code=502)
    preview_id = uuid4().hex
    rejected = proposals["coverage"]["rejected_candidates"]
    questions.extend(f"{item.get('name') or '未导入项目'}：{item['message']}" for item in rejected)
    selected_sheets = [{"sheet_key": sheet["sheet_key"], "sheet_name": next(item["sheet_name"] for item in sources if item["source"]["sheet_key"] == sheet["sheet_key"]),
                        "sheet_role": sheet["suggested_role"], "node_code": None} for sheet in proposals["sheets"]]
    delta = manifest.draft_config_of_import(
        proposals=proposals, decisions=[], namespace=scenario_code, selected_sheets=selected_sheets,
        import_job_no="", existing_config=config,
    )
    _assert_additions(config, delta)
    source_index = {(row["source"]["sheet_key"], row["source"]["row_no"]): row for row in sources}

    def mark_activation(origin: dict[str, Any]) -> None:
        origin["activation_preview_id"] = preview_id
        source_row = source_index.get((origin["sheet_key"], origin["row_no"]))
        if source_row is not None:
            origin["import_job_no"] = source_row.get("import_job_no") or ""
            origin["effective_columns"] = deepcopy(source_row["columns"])
            origin["activated_source_row_key"] = source_row["row_key"]

    for section in ("materials", "fields", "rules", "sections"):
        for item in delta[section]:
            mark_activation(item["origin_json"])
    for item in delta["calculation_json"] or []:
        mark_activation(item["origin"])
        for entry in [*item["parameters"], *item["bands"]]:
            mark_activation(entry["origin"])
    failed_rows = {(item["source"].get("sheet_key"), item["source"].get("row_no"))
                   for item in rejected if isinstance(item.get("source"), dict)}
    has_unlocated_failure = any(not isinstance(item.get("source"), dict)
                               or (item["source"].get("sheet_key"), item["source"].get("row_no")) not in coverage
                               for item in rejected)
    activated_row_keys = [row["row_key"] for row in sources
                          if not has_unlocated_failure
                          and (row["source"]["sheet_key"], row["source"]["row_no"]) not in failed_rows
                          and coverage[(row["source"]["sheet_key"], row["source"]["row_no"])]["disposition"] == "CANDIDATE"]
    has_additions = any(delta[section] for section in ("materials", "fields", "rules", "sections", "calculation_json"))
    content = {
        "preview_id": preview_id, "scenario_code": scenario_code, "revision_no": expected_revision,
        "config_hash": fingerprint, "row_keys": row_keys, "proposals": proposals,
        "delta": delta if has_additions else None, "questions": questions,
        "activated_row_keys": activated_row_keys,
    }
    path = resolve_storage_path(f"source-activations/{preview_id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(content, output, ensure_ascii=False)
    return {
        "preview_id": preview_id, "revision_no": expected_revision, "can_activate": has_additions,
        "rows": [{"row_key": row["row_key"], "sheet_role": row["sheet_role"], "sheet_name": row["sheet_name"], "row_no": row["source"]["row_no"], "source_id": row.get("source_id")} for row in sources],
        "items": _items(proposals, config), "questions": questions,
    }


def activate(db: Session, scenario_code: str, preview_id: str, expected_revision: int) -> ScenarioDraft:
    from .scenario_admin_service import _locked_draft, _save_draft
    path = resolve_storage_path(f"source-activations/{preview_id}.json")
    with path.open(encoding="utf-8") as source:
        preview_data = json.load(source)
    template, draft = _locked_draft(db, scenario_code)
    _revision(draft, expected_revision)
    if (preview_data["scenario_code"] != scenario_code or preview_data["revision_no"] != expected_revision
            or preview_data["config_hash"] != _fingerprint(draft.config_json, draft.schema_json, draft.profile_version)):
        raise BusinessError("DISABLED_SOURCE_PREVIEW_STALE", "解析预览与当前草稿不同，请重新解析", status_code=409)
    _source_rows(draft.config_json, preview_data["row_keys"])
    delta = preview_data["delta"]
    if delta is None:
        raise BusinessError("DISABLED_SOURCE_NO_CANDIDATE", "本次解析没有可追加的候选，具体失败原因见解析预览", status_code=409)
    _assert_additions(draft.config_json, delta)
    calculations = _merge_calculations(draft.calculation_json, delta["calculation_json"] or [])
    for section, model in (("materials", ScenarioDraftMaterial), ("fields", ScenarioDraftField), ("rules", ScenarioDraftRule), ("sections", ScenarioDraftSection)):
        sort_no = config_store.next_sort_no(db, model, draft.id)
        for item in delta[section]:
            config_store.add_mapping(db, model, draft.id, item, sort_no)
            sort_no += 1
    draft.calculation_json = calculations
    # 仅移除完整转成候选的来源；部分失败或无法定位错误的原始行继续保留。
    selected_keys = set(preview_data["activated_row_keys"])
    draft.disabled_source_rows = [row for row in draft.disabled_source_rows if row["row_key"] not in selected_keys]
    _save_draft(db, template, draft, operation_type="UPDATE", object_name=template.scenario_name,
                detail={"action": "ACTIVATE_RULE_LIBRARY_SOURCES", "preview_id": preview_id, "row_keys": sorted(selected_keys)})
    db.refresh(draft)
    return draft
