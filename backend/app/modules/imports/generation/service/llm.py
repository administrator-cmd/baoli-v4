"""AI 辅助生成规则库的两阶段模型调用与结构核对。"""

import logging
import re
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from app.common.exceptions import BusinessError
from app.core.config import get_settings
from app.domain.generation.assistant import AssistantReply, apply_edits
from app.domain.generation.document import (
    GENERATION_DOCUMENT_SCHEMA_VERSION,
    GENERATION_PROMPT_VERSION,
    GenerationContentReview,
    GenerationDocument,
    GenerationIssue,
    GenerationSource,
    GenerationRequirementInventory,
    collect_document_problems,
    collect_requirement_inventory_problems,
    is_business_code_column,
    normalize_business_codes,
    requirement_identity_problems,
    source_record_coverage_problems,
    summarize_generation_problems,
)
from app.domain.imports.workbook import WorkbookIR
from app.domain.imports.workbook_proposals import prepare_sheets
from app.integrations.llm.gateway import call_json
from app.modules.imports.generation.service.prompts import (
    GENERATION_DOCUMENT_PROMPT,
    GENERATION_REQUIREMENTS_PROMPT,
    GENERATION_REVIEW_PROMPT,
    GENERATION_REPAIR_PROMPT,
)

logger = logging.getLogger(__name__)


class GenerationLlmService:
    """固定两阶段编制：业务要求目录 → 完整规则文档。"""

    def _normalize_source_rows(self, response: Any, *, stage: str) -> Any:
        """仅整理模型放错一层的关联行；随后仍走完整结构及真实来源校验。"""
        if not isinstance(response, dict):
            return response
        payload = deepcopy(response)
        owners = []
        for section in ("requirements", "unclear_items", "sheets", "issues"):
            items = payload.get(section)
            if not isinstance(items, list):
                continue
            owners.extend(items)
            if section == "sheets":
                for sheet in items:
                    if isinstance(sheet, dict) and isinstance(sheet.get("rows"), list):
                        owners.extend(sheet["rows"])
        corrected = 0
        for owner in owners:
            source = owner.get("source") if isinstance(owner, dict) else None
            if not isinstance(source, dict) or "related_rows" not in source:
                continue
            origin = source.get("origin")
            misplaced = source["related_rows"]
            if not isinstance(origin, dict) or not isinstance(misplaced, list):
                continue
            if not {"sheet_name", "row_no"} <= origin.keys():
                continue
            existing = origin.get("related_rows", [])
            if not isinstance(existing, list):
                continue
            # 合并两处引用，不覆盖原有关联行，也不凭空补主来源。
            rows = list(existing)
            for reference in misplaced:
                if reference not in rows:
                    rows.append(reference)
            origin["related_rows"] = rows
            del source["related_rows"]
            corrected += 1
        if corrected:
            logger.warning(
                "编制来源结构已整理 stage=%s count=%s source.related_rows→source.origin.related_rows",
                stage, corrected,
            )
        return payload

    def _workbook_payload(self, ir: WorkbookIR) -> tuple[list[dict[str, Any]], set[tuple[str, int]]]:
        sheets = prepare_sheets(ir)
        payload = [sheet.model_input() for sheet in sheets.values()]
        source_rows = {
            (sheet.sheet.name, row.row_no)
            for sheet in sheets.values() for row in sheet.sheet.rows
        }
        return payload, source_rows

    def _source_records(self, ir: WorkbookIR) -> list[dict[str, Any]]:
        """只从明示编号栏目记录原始身份，不按表名、编号前缀分类。"""
        records = []
        for sheet in ir.sheets:
            identity_columns = []
            header_cells = None
            previous_row_no = 0
            for row in sheet.rows:
                # WorkbookIR 的键是 A1/A2 等坐标，同一列跨行不能直接沿用表头坐标。
                cells = {re.sub(r"\d+$", "", key): value for key, value in row.cells.items()}
                headers = [key for key, value in cells.items() if isinstance(value, str) and is_business_code_column(value)]
                codes = [str(cells[key]).strip() for key in identity_columns if cells.get(key) is not None]
                codes = [value for value in codes if not is_business_code_column(value)
                         and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value)]
                # 连续表区固定编号列；只有完整重复表头或空行分隔的新表区可以重选。
                # 即使新表区首行含“编号/ID”，也先保留已有编号列中的真实数据。
                separated = row.row_no > previous_row_no + 1
                previous_row_no = row.row_no
                normalized_cells = {key: re.sub(r"\s+", "", str(value)).casefold() for key, value in cells.items()}
                if headers and (not identity_columns or normalized_cells == header_cells or (separated and not codes)):
                    identity_columns = headers
                    header_cells = normalized_cells
                    continue
                if codes:
                    records.append({"sheet_name": sheet.name, "row_no": row.row_no, "codes": codes})
        return records

    def _raise_contract_error(self, stage: str, problems: list[dict[str, Any]]) -> None:
        """结构错误与来源错误分开报，details 保留具体对象。"""
        source_related = any(
            str(problem.get("kind", "")).startswith("source.")
            or "origin" in str(problem.get("kind", ""))
            for problem in problems
        )
        if source_related:
            code, message = "LLM_SOURCE_REFERENCE_INVALID", "模型返回的编制结果包含不存在或无法解析的来源与引用："
        elif any(problem.get("kind") == "content.review_failed" for problem in problems):
            code, message = "LLM_CONTENT_REVIEW_FAILED", "AI 编制内容纠正后仍未通过复核："
        else:
            code, message = "LLM_CONTRACT_INVALID", "模型返回的编制结果不符合结构契约："
        raise BusinessError(
            code, message + summarize_generation_problems(problems), status_code=502,
            details={"stage": stage, "problems": problems[:30], "count": len(problems)},
        )

    def _checked_call(self, payload, prompt, schema, check, *, operation_no: str, stage: str, reasoning_effort, review=None):
        """结构/来源错误只补一次明确纠正；网络错误交由既有网关处理。"""
        request = dict(payload)
        for attempt in range(2):
            response = call_json(
                request, prompt, idempotency_key=f"{operation_no}:{stage}:{attempt}",
                reasoning_effort=reasoning_effort,
            )
            try:
                result = schema.model_validate(self._normalize_source_rows(response, stage=stage))
                problems = check(result)
                if not problems and review is not None:
                    problems = review(result, attempt)
            except ValidationError as exc:
                problems = [{"owner": ".".join(map(str, error["loc"])), "kind": error["type"]}
                            for error in exc.errors(include_input=False, include_context=False, include_url=False)]
            if not problems:
                return result
            if attempt:
                self._raise_contract_error(stage, problems)
            request["previous_response"] = response
            request["correction"] = {
                "instruction": "纠正下列结构、来源、遗漏或内容一致性问题，保留完整业务含义，重新返回完整 JSON。不得补造来源或业务口径。",
                "problems": problems[:50],
            }
            logger.warning("编制校验失败，进行一次纠正 stage=%s count=%s problems=%s", stage, len(problems), problems)

    def build_document(
        self, ir: WorkbookIR,
        *,
        business_background: str,
        generation_requirements: str,
        operation_no: str,
        job_no: str,
    ) -> dict[str, Any]:
        """两阶段调用；任一阶段结构、来源或身份不合契约都整体失败，不截取半份结果。"""
        workbook_payload, source_rows = self._workbook_payload(ir)
        source_records = self._source_records(ir)
        business_input = {
            "business_background": business_background,
            "generation_requirements": generation_requirements,
        }
        reasoning_effort = get_settings().rulebook_llm_reasoning_effort
        requirements_payload = {
            **business_input,
            "workbook": workbook_payload,
            "source_records": source_records,
            "response_schema": GenerationRequirementInventory.model_json_schema(),
        }
        logger.info(
            "编制开始第一阶段 operation_no=%s job_no=%s sheets=%s rows=%s",
            operation_no, job_no, len(workbook_payload),
            sum(len(sheet["rows"]) for sheet in workbook_payload),
        )
        inventory = self._checked_call(
            requirements_payload,
            GENERATION_REQUIREMENTS_PROMPT,
            GenerationRequirementInventory,
            lambda value: collect_requirement_inventory_problems(value, source_rows)
            + source_record_coverage_problems(value.requirements, source_records),
            operation_no=operation_no, stage="requirements",
            reasoning_effort=reasoning_effort,
        )

        document_payload = {
            **business_input,
            "previous_json": inventory.model_dump(mode="json"),
            "workbook": workbook_payload,
            "source_records": source_records,
            "response_schema": GenerationDocument.model_json_schema(),
        }
        logger.info(
            "编制开始第二阶段 operation_no=%s job_no=%s requirements=%s unclear=%s",
            operation_no, job_no, len(inventory.requirements), len(inventory.unclear_items),
        )
        document = self._checked_call(
            document_payload,
            GENERATION_DOCUMENT_PROMPT,
            GenerationDocument,
            lambda value: requirement_identity_problems(inventory, value)
            + collect_document_problems(value, source_rows, require_coverage=True, require_codes=True)
            + source_record_coverage_problems([row for sheet in value.sheets for row in sheet.rows], source_records, require_code=True),
            operation_no=operation_no, stage="document",
            reasoning_effort=reasoning_effort,
        )

        def check_document(value):
            return (requirement_identity_problems(inventory, value)
                + collect_document_problems(value, source_rows, require_coverage=True, require_codes=True)
                + source_record_coverage_problems([row for sheet in value.sheets for row in sheet.rows], source_records, require_code=True))

        document = self._repair_document(document_payload, document, check_document,
                                         operation_no=operation_no, reasoning_effort=reasoning_effort)

        identity_problems = requirement_identity_problems(inventory, document)
        if identity_problems:
            self._raise_contract_error("document", identity_problems)
        document.code_counters = {}
        document = normalize_business_codes(document)
        document_problems = collect_document_problems(document, source_rows, require_coverage=True, require_codes=True)
        if document_problems:
            self._raise_contract_error("document", document_problems)
        if document.schema_version != GENERATION_DOCUMENT_SCHEMA_VERSION:
            raise BusinessError(
                "LLM_CONTRACT_INVALID", "模型返回的编制契约版本与当前版本不一致", status_code=502,
                details={
                    "expected": GENERATION_DOCUMENT_SCHEMA_VERSION,
                    "actual": document.schema_version,
                    "prompt_version": GENERATION_PROMPT_VERSION,
                },
            )
        logger.info(
            "编制完成 operation_no=%s job_no=%s sheets=%s entries=%s issues=%s",
            operation_no, job_no, len(document.sheets),
            sum(len(sheet.rows) for sheet in document.sheets), len(document.issues),
        )
        return document.model_dump(mode="json")

    def _repair_document(self, payload, document, check, *, operation_no: str, reasoning_effort):
        """局部补修合法候选稿，并保留仍未解决的复核意见。"""
        problems = self._review_document(payload, document, operation_no=operation_no, attempt=0, reasoning_effort=reasoning_effort)
        history, diagnostics, seen = [], [], set()
        for attempt in range(3):
            if not problems:
                return document
            signature = tuple(sorted(str(p.get("reference", "")) for p in problems))
            if signature in seen:
                break
            seen.add(signature)
            history.append(problems)
            request = {**payload, "document": document.model_dump(mode="json"), "review_problems": problems,
                       "review_history": history, "previous_diagnostics": diagnostics,
                       "response_schema": AssistantReply.model_json_schema()}
            response = call_json(request, GENERATION_REPAIR_PROMPT,
                                 idempotency_key=f"{operation_no}:document-repair:{attempt}", reasoning_effort=reasoning_effort)
            try:
                reply = AssistantReply.model_validate(response)
                candidate = apply_edits(document, reply)
                invalid = check(candidate)
                if invalid:
                    diagnostics = invalid[:50]
                    seen.discard(signature)
                    continue
            except (ValidationError, BusinessError) as exc:
                diagnostics = [{"kind": "repair.invalid", "reference": str(exc)}]
                seen.discard(signature)
                continue
            if candidate == document:
                break
            document = candidate
            diagnostics = []
            problems = self._review_document(payload, document, operation_no=operation_no,
                                             attempt=attempt + 1, reasoning_effort=reasoning_effort)
        if problems:
            existing = {item.issue_id for item in document.issues}
            for index, problem in enumerate(problems, 1):
                issue_id = f"REVIEW-{index}"
                while issue_id in existing:
                    issue_id += "-1"
                existing.add(issue_id)
                document.issues.append(GenerationIssue(issue_id=issue_id, title="编制内容待核对",
                    detail="自动复核仍有疑点，请结合原始资料核对：" + str(problem.get("reference", "")),
                    source=GenerationSource(kind="DERIVED", note="自动复核疑点，原文位置未确定")))
            invalid = check(document)
            if invalid:
                self._raise_contract_error("document", invalid)
            logger.warning("编制保留复核疑点 operation_no=%s problems=%s", operation_no, problems)
        return document

    def _review_document(self, payload, document, *, operation_no: str, attempt: int, reasoning_effort):
        request = {**payload, "document": document.model_dump(mode="json"),
                   "response_schema": GenerationContentReview.model_json_schema()}
        for correction in range(2):
            response = call_json(
                request, GENERATION_REVIEW_PROMPT,
                idempotency_key=f"{operation_no}:document-review:{attempt}" + (":correction" if correction else ""),
                reasoning_effort=reasoning_effort,
            )
            try:
                review = GenerationContentReview.model_validate(response)
                break
            except ValidationError as exc:
                problems = [{"owner": ".".join(map(str, error["loc"])), "kind": error["type"]}
                            for error in exc.errors(include_input=False, include_context=False, include_url=False)]
                if correction:
                    raise BusinessError("LLM_REVIEW_INVALID", "AI 内容复核响应纠正后仍不符合结构契约，未保存编制文档",
                                        status_code=502, details={"problems": problems}) from exc
                request["previous_response"] = response
                request["correction"] = {"instruction": "只纠正复核响应，按 response_schema 返回结论，不重写编制文档。", "problems": problems}
        return [{"owner": "编制内容", "kind": "content.review_failed", "reference": problem} for problem in review.problems]
