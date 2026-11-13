"""规则库 Excel → 第一轮业务 JSON → 第二轮 LLM 整理 → 保存配置。"""

import hashlib
import json
import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.core.config import get_settings
from app.domain.configuration import field_strategy
from app.domain.imports.workbook import WorkbookIR
from app.domain.imports.workbook_proposals import PreparedSheet, WorkbookInventory, WorkbookProposals, prepare_sheets
from app.integrations.llm.gateway import call_json
from app.modules.imports.job.response_execution import save_raw_response
from app.modules.imports.job.service.prompts import CONVERSION_PROMPT, REFINEMENT_PROMPT
from app.modules.imports.job.service.stage_cache import ImportsJobStageCacheService

logger = logging.getLogger(__name__)

PROPOSAL_GROUPS = ("materials", "fields", "rules", "calculations", "report_sections")


CONVERSION_MODE_FULL = "FULL"
CONVERSION_MODE_RESUME = "RESUME_REFINEMENT"


class ImportsJobLlmService:
    """调用模型生成规则配置，并核对结构与引用。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _validate_content_references(
        self, result: dict[str, Any], *, initial: dict[str, Any] | None = None,
    ) -> None:
        """只校验记录身份、资料归属与保留，不用代码判断哪条业务是真规则。"""
        refs = {group: [item["candidate_ref"] for item in result[group]] for group in PROPOSAL_GROUPS}
        all_refs = [reference for group in refs.values() for reference in group]
        support_refs = [item["support_ref"] for item in result["supporting_content"]]
        problems = []
        for label, identifiers in (("candidate_ref", all_refs), ("support_ref", support_refs)):
            seen: set[str] = set()
            for reference in identifiers:
                if not reference.strip() or reference in seen:
                    problems.append({"kind": label, "reference": reference})
                seen.add(reference)
        for item in result["supporting_content"]:
            for target in item["targets"]:
                if target["candidate_ref"] not in refs[target["group"]]:
                    problems.append({"kind": "supporting_content.target", "owner": item["support_ref"],
                                     "group": target["group"], "reference": target["candidate_ref"]})
        if initial is not None:
            for reference in {item["support_ref"] for item in initial["supporting_content"]} - set(support_refs):
                problems.append({"kind": "supporting_content.missing", "reference": reference})
        if problems:
            raise BusinessError(
                "LLM_CONTRACT_INVALID", "解析结果的对象身份或辅助资料归属不完整", status_code=502,
                details={"references": problems, "count": len(problems)},
            )

    def _source_rows(self, sheets: dict[str, PreparedSheet]) -> dict[tuple[str, int], dict[str, Any]]:
        return {(sheet.sheet.name, row.row_no): row.cells
                for sheet in sheets.values() for row in sheet.sheet.rows}

    def _validate_sources(
        self, result: dict[str, Any], source_rows: dict[tuple[str, int], dict[str, Any]], *, attach: bool,
    ) -> None:
        """配置和辅助资料都必须引用真实原行；最终保存时附上文件中的原始单元格。"""
        for group in (*PROPOSAL_GROUPS, "supporting_content"):
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
                            details={"group": group, "reference": item.get("candidate_ref", item.get("support_ref")),
                                     "sheet_name": sheet_name, "row_no": row_no},
                        )
                    if attach:
                        rows.append({"sheet_name": sheet_name, "row_no": row_no,
                                     "cells": dict(source_rows[(sheet_name, row_no)])})
                if attach:
                    origin["rows"] = rows

    def _attach_supporting_context(self, result: dict[str, Any]) -> None:
        """相关资料进入所属对象的业务上下文；全局资料在导入快照中完整保留。"""
        objects = {(group, item["candidate_ref"]): item for group in PROPOSAL_GROUPS for item in result[group]}
        related = {identity: [] for identity in objects}
        for item in objects.values():
            item["business_context"]["interpretation"] = item.pop("interpretation")
        for support in result["supporting_content"]:
            for target in support["targets"]:
                related[(target["group"], target["candidate_ref"])].append({
                    "kind": support["kind"], "title": support["title"], "content": support["content"],
                    "usage": target["usage"],
                    "origin": {key: value for key, value in support["origin"].items() if key != "rows"},
                })
        for identity, items in related.items():
            # 来源及归属只从已校验的顶层记录投影，不采纳模型另写的一份嵌套真值。
            objects[identity]["business_context"].pop("supporting_content", None)
            if items:
                objects[identity]["business_context"]["supporting_content"] = items

    def _validate_proposal_references(self, result: dict[str, Any]) -> None:
        """检查模型返回配置的内部身份：全局唯一、引用可解析。

        模型漏写或抄错 candidate_ref 时，如果只等编译/执行阶段暴露，用户看到的是内部身份错误；
        这里在写入提案前给出具体对象和引用位置，不猜测、不自动补引用。
        已声明缺项的空引用参数仅保留在缺项清单，不作为已绑定参数传入编译。
        """
        groups = PROPOSAL_GROUPS
        refs_by_group = {
            group: [str(item.get("candidate_ref") or "") for item in result.get(group) or []]
            for group in groups
        }
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
        result["binding_gaps"] = self._binding_gaps(result, problems)
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
            declared_gaps = {
                gap["parameter_code"] for gap in result["binding_gaps"]
                if gap["owner_group"] == "calculations" and gap["owner_ref"] == owner
                and gap.get("parameter_code")
            }
            parameters = []
            for parameter in calculation.get("parameters") or []:
                if not isinstance(parameter.get("parameter_code"), str) or not parameter["parameter_code"].strip():
                    problems.append({"owner": owner, "kind": "calculation.parameter.parameter_code"})
                source_type = parameter.get("source_type")
                if source_type not in {"FIELD", "CALC_RESULT", "CONSTANT"}:
                    problems.append({
                        "owner": owner, "kind": "calculation.parameter.source_type",
                        "reference": source_type,
                    })
                    continue
                if (source_type in {"FIELD", "CALC_RESULT"}
                        and parameter.get("source_code") in (None, "")
                        and parameter.get("parameter_code") in declared_gaps):
                    logger.info(
                        "计算未绑定参数按已声明缺项保留 owner=%s parameter_code=%s",
                        owner, parameter["parameter_code"],
                    )
                    continue
                parameters.append(parameter)
                if source_type == "FIELD":
                    check(owner, "calculation.parameter.field", parameter.get("source_code"),
                          field_refs | field_source_ids)
                elif source_type == "CALC_RESULT":
                    check(owner, "calculation.parameter.result", parameter.get("source_code"), result_codes)
                elif "constant_value" not in parameter:
                    problems.append({
                        "owner": owner, "kind": "calculation.parameter.constant_value",
                        "reference": parameter.get("parameter_code"),
                    })
            calculation["parameters"] = parameters
            for reference in calculation.get("depends_on_result_codes") or []:
                check(owner, "calculation.depends_on_result_codes", reference, result_codes)
        for section in result.get("report_sections") or []:
            owner = section.get("candidate_ref")
            for reference in section.get("field_refs") or []:
                check(owner, "report.field_refs", reference, allowed_report_refs)
            for reference in section.get("material_refs") or []:
                check(owner, "report.material_refs", reference, material_refs)
        self._check_field_identities(result, problems)
        if problems:
            raise BusinessError(
                "LLM_CONTRACT_INVALID",
                "模型返回的配置包含无法解析的业务引用",
                status_code=502,
                details={"references": problems[:20], "count": len(problems)},
            )


    def _check_field_identities(self, result: dict[str, Any], problems: list[dict[str, Any]]) -> None:
        """同业务编号、同材料、同口径的字段才能视为同一事实身份。"""
        identities: dict[tuple[Any, ...], str] = {}
        for field in result.get("fields") or []:
            owner = str(field.get("candidate_ref") or "")
            identity = (
                str(field.get("source_id") or owner),
                str(field.get("material_ref") or ""),
                str(field.get("period_scope") or "NOT_APPLICABLE"),
                str(field.get("statement_scope") or "NOT_APPLICABLE"),
                str(field.get("entity_scope") or "SELF"),
            )
            if identity in identities:
                problems.append({
                    "owner": owner, "kind": "field.identity_duplicate",
                    "reference": {"other": identities[identity], "identity": list(identity)},
                })
            else:
                identities[identity] = owner


    def _binding_gaps(self, result: dict[str, Any], problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """保留模型声明的缺项，仅核对归属引用，不推断业务含义。"""
        gaps = []
        for calculation in result["calculations"]:
            for gap in calculation.get("input_gaps") or []:
                gaps.append({
                    "owner_group": "calculations", "owner_ref": calculation["candidate_ref"],
                    "owner_name": calculation["rule_name"], **gap,
                })
        owners = {
            "rules": {item["candidate_ref"] for item in result["rules"]},
            "calculations": {item["candidate_ref"] for item in result["calculations"]},
        }
        for gap in result["binding_gaps"]:
            if gap["owner_ref"] not in owners[gap["owner_group"]]:
                problems.append({
                    "owner": gap["owner_ref"], "kind": "binding_gap.owner_ref",
                    "reference": {key: gap[key] for key in ("owner_group", "owner_ref")},
                })
            else:
                gaps.append(dict(gap))
        unique = {}
        for gap in gaps:
            identity = tuple(gap.get(key) for key in (
                "owner_group", "owner_ref", "parameter_code", "missing_input", "reason",
            ))
            unique.setdefault(identity, gap)
        return list(unique.values())


    def initial_fingerprint(self, profile_version: str, schema_key: str, schema_version: str, source_sha256: str,
        scenario_code: str | None,
    ) -> str:
        """第一轮结果的输入指纹：源文件、场景、schema 与第一轮请求契约。

        第一轮请求实际发送的是 scenario_code、workbook、field_contract 和
        WorkbookInventory 的 JSON Schema，这四项都必须进入指纹；只改其中一项就
        不能复用旧的目录结果。
        """
        contract = json.dumps(field_strategy.contract_payload(), ensure_ascii=False, sort_keys=True)
        inventory_schema = json.dumps(WorkbookInventory.model_json_schema(), ensure_ascii=False, sort_keys=True)
        return ImportsJobStageCacheService().build_fingerprint(
            profile_version, schema_key, schema_version, source_sha256,
            scenario_code or "", CONVERSION_PROMPT, contract, inventory_schema,
        )


    def can_resume_initial(self, job_no: str, profile_version: str, schema_key: str, schema_version: str, source_sha256: str,
        scenario_code: str | None,
    ) -> bool:
        """当前作业是否存在可复用的第一轮结果（只核对输入层面，复用前还会核对完整指纹）。"""
        return ImportsJobStageCacheService().initial_stage_available(
            job_no, self.initial_fingerprint(profile_version, schema_key, schema_version, source_sha256, scenario_code),
        )


    def convert_workbook(self, ir: WorkbookIR,
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
        return self.convert_prepared_sheets(
            ir.scenario_code, sheets, operation_no=operation_no, job_no=job_no,
            fingerprint=fingerprint, mode=mode,
        )


    def convert_prepared_sheets(self, scenario_code: str | None, sheets: dict[str, PreparedSheet],
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
        resume_fingerprint = ImportsJobStageCacheService().build_fingerprint(fingerprint, payload_digest)
        if mode == CONVERSION_MODE_RESUME:
            initial_response = ImportsJobStageCacheService().load_initial(job_no, resume_fingerprint)
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
            inventory = WorkbookInventory.model_validate(initial_response)
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
        initial_response = inventory.model_dump(mode="json")
        self._validate_content_references(initial_response)
        source_rows = self._source_rows(sheets)
        self._validate_sources(initial_response, source_rows, attach=False)
        if mode != CONVERSION_MODE_RESUME:
            # 检查点写不出是真实的存储故障：原样上抛，不把一次写盘失败伪装成转换成功。
            ImportsJobStageCacheService().save_initial(job_no, resume_fingerprint, fingerprint, initial_response)
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
        # 读取结构后核对真实引用与出处；不按规则名称或表名筛除业务项。
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
        self._validate_content_references(result, initial=initial_response)
        self._validate_proposal_references(result)
        self._validate_sources(result, source_rows, attach=True)
        self._attach_supporting_context(result)
        return result
