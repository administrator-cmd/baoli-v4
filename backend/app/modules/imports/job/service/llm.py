"""规则库 Excel → 第一轮业务 JSON → 第二轮 LLM 整理 → 保存配置。"""

import hashlib
import json
import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.core.config import get_settings
from app.domain import field_strategy
from app.domain.workbook import WorkbookIR
from app.domain.workbook_proposals import PreparedSheet, WorkbookInventory, WorkbookProposals, prepare_sheets
from app.integrations.llm.gateway import call_json
from app.modules.imports.job.service.prompts import CONVERSION_PROMPT, REFINEMENT_PROMPT
from app.modules.imports.job.service.stage_cache import ImportsJobStageCacheService
from app.services.shared.llm_responses import save_raw_response

logger = logging.getLogger(__name__)


CONVERSION_MODE_FULL = "FULL"
CONVERSION_MODE_RESUME = "RESUME_REFINEMENT"


class ImportsJobLlmService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _validate_proposal_references(self, result: dict[str, Any]) -> None:
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
                source_type = parameter.get("source_type")
                if source_type not in {"FIELD", "CALC_RESULT", "CONSTANT"}:
                    problems.append({
                        "owner": owner, "kind": "calculation.parameter.source_type",
                        "reference": source_type,
                    })
                    continue
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
            for reference in calculation.get("depends_on_result_codes") or []:
                check(owner, "calculation.depends_on_result_codes", reference, result_codes)
        for section in result.get("report_sections") or []:
            owner = section.get("candidate_ref")
            for reference in section.get("field_refs") or []:
                check(owner, "report.field_refs", reference, allowed_report_refs)
            for reference in section.get("material_refs") or []:
                check(owner, "report.material_refs", reference, material_refs)
        self._check_field_identities(result, problems)
        result["binding_gaps"] = self._binding_gaps(result, problems)
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


    def _has_cjk(self, value: str) -> bool:
        return any("一" <= char <= "鿿" for char in value)

    def _name_text(self, value: str) -> str:
        """业务名称比较文本：去掉空白和常见分隔符，不做同义词改写。"""
        return "".join(char for char in value if not char.isspace() and char not in "（）()【】[]、，,；;：:/·-_")

    def _expected_period(self, parameter_code: str) -> str | None:
        """从参数名的期间字面推导规范期间；只用于核对声明，不解释公式业务。"""
        mapping = (
            ("本期", "CURRENT"), ("上期", "PRIOR"), ("去年同期", "PRIOR"),
            ("期初", "OPENING"), ("年初", "OPENING"),
            ("期末", "CLOSING"), ("年末", "CLOSING"),
            ("平均", "AVERAGE_OPENING_CLOSING"),
        )
        for hint, expected in mapping:
            if hint in parameter_code:
                return expected
        return None

    def _binding_gaps(self, result: dict[str, Any], problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """汇总模型声明的输入缺项，并补充程序可确认的重复绑定。

        程序只检查引用身份与参数结构，不解释公式或财务科目；业务含义仍由第二轮
        模型逐项核对。缺项只暴露配置不完整，不改变任何对象的启用状态。
        """
        fields: dict[str, dict[str, Any]] = {}
        for field in result.get("fields") or []:
            for key in (field.get("candidate_ref"), field.get("source_id")):
                if key:
                    fields[str(key)] = field
        gaps: list[dict[str, Any]] = []

        def add(gap: dict[str, Any]) -> None:
            gaps.append(gap)

        for calculation in result.get("calculations") or []:
            owner_ref = str(calculation.get("candidate_ref") or "")
            owner_name = calculation.get("rule_name")
            requirement = calculation.get("requirement") or calculation.get("formula") or ""
            parameters = calculation.get("parameters") or []
            if not parameters:
                add({
                    "owner_group": "calculations", "owner_ref": owner_ref, "owner_name": owner_name,
                    "parameter_code": None, "missing_input": "全部计算输入",
                    "reason": "公式没有声明任何输入参数，无法核对实际取数绑定",
                    "original_requirement": requirement,
                })
            by_source: dict[str, list[dict[str, Any]]] = {}
            for parameter in parameters:
                if parameter.get("source_type") != "FIELD":
                    continue
                code = str(parameter.get("source_code") or "")
                by_source.setdefault(code, []).append(parameter)
                if not parameter.get("parameter_code"):
                    problems.append({
                        "owner": owner_ref, "kind": "calculation.parameter.parameter_code",
                        "reference": code,
                    })
            for code, items in by_source.items():
                parameter_codes = {str(item.get("parameter_code") or "") for item in items}
                if len(items) <= 1 or len(parameter_codes) <= 1:
                    continue
                field = fields.get(code) or {}
                add({
                    "owner_group": "calculations", "owner_ref": owner_ref, "owner_name": owner_name,
                    "parameter_code": "、".join(sorted(parameter_codes)),
                    "missing_input": str(field.get("name") or code),
                    "reason": "同一字段被多个不同参数引用；每个公式输入必须有独立来源，不能只改参数名称",
                    "original_requirement": requirement,
                })
            for code, items in by_source.items():
                field = fields.get(code) or {}
                field_name = str(field.get("name") or "").strip()
                for parameter in items:
                    parameter_code = str(parameter.get("parameter_code") or "").strip()
                    if not parameter_code or not field_name:
                        continue
                    # 只比较中文业务名称；英文内部参数名不作为业务含义依据。
                    if not self._has_cjk(parameter_code) or not self._has_cjk(field_name):
                        continue
                    left = self._name_text(parameter_code)
                    right = self._name_text(field_name)
                    if left not in right and right not in left:
                        add({
                            "owner_group": "calculations", "owner_ref": owner_ref, "owner_name": owner_name,
                            "parameter_code": parameter_code,
                            "missing_input": field_name,
                            "reason": "参数名称与绑定字段名称不一致，需确认取的是同一科目，不能用相近科目替代",
                            "original_requirement": requirement,
                        })
                    if any(hint in parameter_code for hint in ("本期", "上期", "期初", "期末", "年初", "年末", "平均", "去年同期", "近两年", "一年内", "一年以内", "到期")) and field.get("period_scope") in (None, "", "NOT_APPLICABLE"):
                        add({
                            "owner_group": "calculations", "owner_ref": owner_ref, "owner_name": owner_name,
                            "parameter_code": parameter_code,
                            "missing_input": field_name,
                            "reason": "参数含期间限定，但绑定字段未声明取数期间，无法区分为哪一期",
                            "original_requirement": requirement,
                        })
                    expected_period = self._expected_period(parameter_code)
                    field_period = str(field.get("period_scope") or "").upper()
                    if (expected_period and field_period in {
                        "CURRENT", "PRIOR", "OPENING", "CLOSING", "AVERAGE_OPENING_CLOSING",
                    } and field_period != expected_period):
                        add({
                            "owner_group": "calculations", "owner_ref": owner_ref, "owner_name": owner_name,
                            "parameter_code": parameter_code,
                            "missing_input": field_name,
                            "reason": f"参数期间与字段声明的取数期间不一致（字段为 {field_period}）",
                            "original_requirement": requirement,
                        })
                    if any(hint in parameter_code for hint in ("合并", "母公司", "个别报表")) and field.get("statement_scope") in (None, "", "NOT_APPLICABLE"):
                        add({
                            "owner_group": "calculations", "owner_ref": owner_ref, "owner_name": owner_name,
                            "parameter_code": parameter_code,
                            "missing_input": field_name,
                            "reason": "参数含报表口径限定，但绑定字段未声明报表口径",
                            "original_requirement": requirement,
                        })
            for gap in calculation.get("input_gaps") or []:
                add({
                    "owner_group": "calculations", "owner_ref": owner_ref, "owner_name": owner_name,
                    "parameter_code": gap.get("parameter_code"),
                    "missing_input": gap.get("missing_input"),
                    "reason": gap.get("reason"),
                    "original_requirement": gap.get("original_requirement") or requirement,
                })
        rule_refs = {str(item.get("candidate_ref")) for item in result.get("rules") or []}
        calculation_refs = {str(item.get("candidate_ref")) for item in result.get("calculations") or []}
        for rule in result.get("rules") or []:
            mode = str(rule.get("execution_mode") or "UNKNOWN")
            if mode not in {"VALUE_COMPARE", "CROSS_SCOPE_COMPARE", "CALCULATE"}:
                continue
            has_refs = bool(rule.get("inputs") or rule.get("source_rule_refs") or rule.get("source_rule_ref"))
            parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
            has_parameters = any(
                key in parameters for key in ("field_ref", "field_key", "source_code", "material_ref", "input_field")
            )
            if has_refs or has_parameters:
                continue
            add({
                "owner_group": "rules", "owner_ref": str(rule.get("candidate_ref") or ""),
                "owner_name": rule.get("name"), "parameter_code": None,
                "missing_input": "比较或计算所需的输入",
                "reason": f"执行方式为 {mode}，但没有声明任何字段、来源规则或参数输入",
                "original_requirement": rule.get("requirement") or "",
            })
        for gap in result.get("binding_gaps") or []:
            group = gap.get("owner_group")
            owner_ref = str(gap.get("owner_ref") or "")
            allowed = rule_refs if group == "rules" else calculation_refs if group == "calculations" else set()
            if owner_ref not in allowed:
                problems.append({
                    "owner": owner_ref or gap.get("owner_name"), "kind": "binding_gap.owner_ref",
                    "reference": {"owner_group": group, "owner_ref": owner_ref},
                })
                continue
            add(dict(gap))
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for gap in gaps:
            identity = (
                gap.get("owner_group"), gap.get("owner_ref"), gap.get("parameter_code"),
                gap.get("missing_input"), gap.get("reason"),
            )
            if identity not in seen:
                seen.add(identity)
                deduped.append(gap)
        return deduped


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
        self._validate_proposal_references(result)
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
