"""规则库 Excel → 第一轮业务 JSON → 第二轮 LLM 整理 → 保存配置。"""

import hashlib
import json
import logging
from typing import Any

from pydantic import ValidationError

from ...common.exceptions import BusinessError
from ...core.config import get_settings
from ...domain import field_strategy
from ...domain.workbook import WorkbookIR
from ...domain.workbook_proposals import PreparedSheet, WorkbookInventory, WorkbookProposals, prepare_sheets
from ...integrations.llm.gateway import call_json
from ..shared.llm_responses import save_raw_response
from .prompts import CONVERSION_PROMPT, REFINEMENT_PROMPT
from .stage_cache import build_fingerprint, initial_stage_available, load_initial, save_initial

logger = logging.getLogger(__name__)


CONVERSION_MODE_FULL = "FULL"
CONVERSION_MODE_RESUME = "RESUME_REFINEMENT"


def _validate_proposal_references(result: dict[str, Any]) -> None:
    """检查模型返回配置的内部身份：全局唯一、引用可解析。

    模型漏写或抄错 candidate_ref 时，如果只等编译/执行阶段暴露，用户看到的是内部身份错误；
    这里在写入提案前给出具体对象和引用位置，不猜测、不自动补引用。
    """
    groups = ("materials", "fields", "rules", "calculations", "report_sections")
    refs_by_group = {
        group: [str(item.get("candidate_ref") or "") for item in result.get(group) or []]
        for group in groups
    }
    all_refs = [ref for refs in refs_by_group.values() for ref in refs]
    duplicates = sorted({ref for ref in all_refs if ref and all_refs.count(ref) > 1})
    empty = [ref for ref in all_refs if not ref]
    if duplicates or empty:
        raise BusinessError(
            "LLM_CONTRACT_INVALID",
            "模型返回的配置包含重复或空的内部引用，无法建立业务对象关系",
            status_code=502,
            details={"duplicates": duplicates[:20], "empty_count": len(empty)},
        )

    field_refs = set(refs_by_group["fields"])
    field_source_ids = {
        str(item.get("source_id")) for item in result.get("fields") or [] if item.get("source_id")
    }
    material_refs = set(refs_by_group["materials"])
    rule_refs = set(refs_by_group["rules"])
    result_codes = {
        str(item.get("result_code") or "") for item in result.get("calculations") or []
    }
    raw_result_codes = [
        str(item.get("result_code") or "") for item in result.get("calculations") or []
    ]
    calculation_refs = set(refs_by_group["calculations"])
    allowed_report_refs = field_refs | rule_refs | result_codes | calculation_refs

    problems: list[dict[str, Any]] = []
    if any(not code for code in raw_result_codes):
        problems.append({"owner": "calculations", "kind": "calculation.result_code", "reference": ""})
    duplicate_result_codes = sorted({
        code for code in raw_result_codes if code and raw_result_codes.count(code) > 1
    })
    if duplicate_result_codes:
        problems.append({
            "owner": "calculations", "kind": "calculation.result_code_duplicate",
            "reference": duplicate_result_codes[:20],
        })

    def check(owner: str, kind: str, reference: Any, allowed: set[str]) -> None:
        if reference not in allowed:
            problems.append({"owner": owner, "kind": kind, "reference": reference})

    for field in result.get("fields") or []:
        check(field.get("candidate_ref"), "field.material_ref", field.get("material_ref"), material_refs)
    for rule in result.get("rules") or []:
        owner = rule.get("candidate_ref")
        for item in rule.get("inputs") or []:
            check(owner, "rule.inputs.field_ref", item.get("field_ref"), field_refs)
            check(owner, "rule.inputs.material_ref", item.get("material_ref"), material_refs)
        for reference in rule.get("output_field_refs") or []:
            check(owner, "rule.output_field_refs", reference, field_refs)
        for reference in [rule.get("source_rule_ref"), *(rule.get("source_rule_refs") or [])]:
            if reference is not None:
                check(owner, "rule.source_rule_ref", reference, rule_refs)
    for calculation in result.get("calculations") or []:
        owner = calculation.get("candidate_ref")
        for parameter in calculation.get("parameters") or []:
            if parameter.get("source_type") == "FIELD":
                check(owner, "calculation.parameter.field", parameter.get("source_code"),
                      field_refs | field_source_ids)
            elif parameter.get("source_type") == "CALC_RESULT":
                check(owner, "calculation.parameter.result", parameter.get("source_code"), result_codes)
        for reference in calculation.get("depends_on_result_codes") or []:
            check(owner, "calculation.depends_on_result_codes", reference, result_codes)
    for section in result.get("report_sections") or []:
        owner = section.get("candidate_ref")
        for reference in section.get("field_refs") or []:
            check(owner, "report.field_refs", reference, allowed_report_refs)
        for reference in section.get("material_refs") or []:
            check(owner, "report.material_refs", reference, material_refs)
    if problems:
        raise BusinessError(
            "LLM_CONTRACT_INVALID",
            "模型返回的配置包含无法解析的业务引用",
            status_code=502,
            details={"references": problems[:20], "count": len(problems)},
        )


def initial_fingerprint(
    profile_version: str, schema_key: str, schema_version: str, source_sha256: str,
    scenario_code: str | None,
) -> str:
    """第一轮结果的输入指纹：源文件、场景、schema 与第一轮请求契约。

    第一轮请求实际发送的是 scenario_code、workbook、field_contract 和
    WorkbookInventory 的 JSON Schema，这四项都必须进入指纹；只改其中一项就
    不能复用旧的目录结果。
    """
    contract = json.dumps(field_strategy.contract_payload(), ensure_ascii=False, sort_keys=True)
    inventory_schema = json.dumps(WorkbookInventory.model_json_schema(), ensure_ascii=False, sort_keys=True)
    return build_fingerprint(
        profile_version, schema_key, schema_version, source_sha256,
        scenario_code or "", CONVERSION_PROMPT, contract, inventory_schema,
    )


def can_resume_initial(
    job_no: str, profile_version: str, schema_key: str, schema_version: str, source_sha256: str,
    scenario_code: str | None,
) -> bool:
    """当前作业是否存在可复用的第一轮结果（只核对输入层面，复用前还会核对完整指纹）。"""
    return initial_stage_available(
        job_no, initial_fingerprint(profile_version, schema_key, schema_version, source_sha256, scenario_code),
    )


def convert_workbook(
    ir: WorkbookIR,
    *,
    operation_no: str,
    job_no: str,
    fingerprint: str,
    mode: str = CONVERSION_MODE_FULL,
) -> dict[str, Any]:
    """完整发送已绑定表的原文，由模型生成配置；失败直接暴露，不使用本地转换兜底。

    两轮都读取全部非空工作表；第二轮对照原文整理第一轮 JSON 的业务关系。
    程序不做业务检查或逐项筛除，发布仍走草稿编译。
    """
    sheets = prepare_sheets(ir)
    return convert_prepared_sheets(
        ir.scenario_code, sheets, operation_no=operation_no, job_no=job_no,
        fingerprint=fingerprint, mode=mode,
    )


def convert_prepared_sheets(
    scenario_code: str | None, sheets: dict[str, PreparedSheet],
    *, operation_no: str, job_no: str, fingerprint: str, mode: str = CONVERSION_MODE_FULL,
) -> dict[str, Any]:
    """两轮模型请求；程序只附加真实来源行，不解析业务语义或提供业务检查结果。"""
    response_schema = WorkbookProposals.model_json_schema()
    reasoning_effort = get_settings().rulebook_llm_reasoning_effort
    workbook_payload = [sheet.model_input() for sheet in sheets.values()]
    payload = {
        "scenario_code": scenario_code,
        "workbook": workbook_payload,
        "field_contract": field_strategy.contract_payload(),
        "response_schema": WorkbookInventory.model_json_schema(),
    }
    # 完整指纹：输入指纹之外再绑定第一轮实际发送的工作簿原文。
    # 工作簿读取或请求体构造方式变化时，旧检查点不再被当作当前结果复用。
    payload_digest = hashlib.sha256(
        json.dumps(workbook_payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    ).hexdigest()
    resume_fingerprint = build_fingerprint(fingerprint, payload_digest)
    if mode == CONVERSION_MODE_RESUME:
        initial_response = load_initial(job_no, resume_fingerprint)
        if initial_response is None:
            raise BusinessError(
                "IMPORT_INITIAL_STAGE_UNAVAILABLE",
                "第一轮解析结果不可用（缺失、损坏或解析契约已变化），需要整份重新解析",
                status_code=409,
                details={"job_no": job_no},
            )
        logger.info("规则库复用第一轮解析结果 operation_no=%s job_no=%s", operation_no, job_no)
    else:
        logger.info(
            "规则库开始请求 LLM operation_no=%s stage=initial sheets=%s rows=%s cells=%s payload_bytes=%s",
            operation_no,
            len(sheets),
            sum(len(sheet.sheet.rows) for sheet in sheets.values()),
            sum(len(row.cells) for sheet in sheets.values() for row in sheet.sheet.rows),
            len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        )
        initial_response = call_json(
            payload,
            CONVERSION_PROMPT,
            reasoning_effort=reasoning_effort,
            idempotency_key=f"{operation_no}:initial",
            on_content=lambda content: save_raw_response(operation_no, content, stage="initial"),
        )
    # 第一轮目录既是检查点又被第二轮直接消费；形状不合法时不能存下来反复复用。
    try:
        WorkbookInventory.model_validate(initial_response)
    except ValidationError as exc:
        if mode == CONVERSION_MODE_RESUME:
            raise BusinessError(
                "IMPORT_INITIAL_STAGE_UNAVAILABLE",
                "第一轮解析结果不可用（结构不符合契约），需要整份重新解析",
                status_code=409,
                details={"job_no": job_no},
            ) from exc
        raise BusinessError(
            "LLM_CONTRACT_INVALID", "第一轮模型返回的目录无法读取为业务目录", status_code=502,
            details={"stage": "initial", "mismatches": exc.errors(
                include_url=False, include_input=False, include_context=False,
            )},
        ) from exc
    if mode != CONVERSION_MODE_RESUME:
        # 检查点写不出是真实的存储故障：原样上抛，不把一次写盘失败伪装成转换成功。
        save_initial(job_no, resume_fingerprint, fingerprint, initial_response)
    refinement_payload = {
        "previous_json": initial_response,
        "workbook": workbook_payload,
        "field_contract": field_strategy.contract_payload(),
        "response_schema": response_schema,
    }
    logger.info(
        "规则库开始请求 LLM operation_no=%s stage=refinement payload_bytes=%s",
        operation_no,
        len(json.dumps(refinement_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
    )
    response = call_json(
        refinement_payload,
        REFINEMENT_PROMPT,
        reasoning_effort=reasoning_effort,
        idempotency_key=f"{operation_no}:refinement",
        on_content=lambda content: save_raw_response(operation_no, content, stage="refinement"),
    )
    # 只读取最终 JSON 的接口数据形状，不检查业务身份、引用或公式，不筛除任何业务项。
    try:
        proposals = WorkbookProposals.model_validate(response)
    except ValidationError as exc:
        raise BusinessError(
            "LLM_CONTRACT_INVALID", "第二轮模型返回的 JSON 无法读取为业务配置", status_code=502,
            details={"stage": "refinement", "mismatches": exc.errors(
                include_url=False, include_input=False, include_context=False,
            )},
        ) from exc
    result = proposals.model_dump(mode="json")
    _validate_proposal_references(result)
    source_rows = {
        (sheet.sheet.name, row.row_no): row.cells
        for sheet in sheets.values() for row in sheet.sheet.rows
    }
    for group in ("materials", "fields", "rules", "calculations", "report_sections"):
        for item in result[group]:
            origin = item["origin"]
            references = [(origin["sheet_name"], origin["row_no"]), *(
                (ref["sheet_name"], ref["row_no"]) for ref in origin["related_rows"]
            )]
            rows = []
            for sheet_name, row_no in dict.fromkeys(references):
                if (sheet_name, row_no) not in source_rows:
                    raise BusinessError(
                        "LLM_SOURCE_REFERENCE_INVALID", "模型返回的规则库来源行不存在", status_code=502,
                        details={"group": group, "candidate_ref": item["candidate_ref"],
                                 "sheet_name": sheet_name, "row_no": row_no},
                    )
                rows.append({"sheet_name": sheet_name, "row_no": row_no,
                             "cells": dict(source_rows[(sheet_name, row_no)])})
            # 原始单元格来自文件；模型只提供引用，不能自行编造来源原文。
            origin["rows"] = rows
    return result
