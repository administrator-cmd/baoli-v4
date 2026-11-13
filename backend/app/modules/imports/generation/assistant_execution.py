"""基于冻结版本编辑；模型调用不持锁，结果与消息及快照原子提交。"""

import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.core.config import get_settings
from app.core.transactions import commit_transaction, rollback_transaction
from app.domain.generation_assistant import (
    ASSISTANT_PROMPT_VERSION,
    AssistantReply,
    AssistantReview,
    apply_edits,
    apply_review_text_repairs,
    document_changes,
)
from app.domain.rulebook_generation import (
    GenerationDocument,
    collect_document_problems,
    normalize_business_codes,
    summarize_generation_problems,
)
from app.integrations.llm.gateway import call_json
from app.modules.imports.generation.crud import GenerationMessageCRUD
from app.modules.imports.generation.model import RulebookGenerationMessage
from app.modules.imports.generation.service.assistant_prompt import ASSISTANT_PROMPT, ASSISTANT_REVIEW_PROMPT
from app.modules.imports.generation.service.editing import GenerationEditingService
from app.modules.imports.generation.service.history import GenerationHistoryService
from app.modules.imports.generation.service.llm import GenerationLlmService
from app.modules.imports.generation.service.query import GenerationQueryService
from app.modules.system.operation.crud import SystemOperationCRUD
from app.modules.system.operation.service.background import PHASE_LLM_REQUEST, PHASE_RESULT_READY, CommandHandle
from app.modules.system.operation.service.operation_types import OBJECT_RULEBOOK_GENERATION

logger = logging.getLogger(__name__)


class GenerationAssistantExecution:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _current(self, command: CommandHandle, *, check_revision: bool = True):
        content = command.request
        job = GenerationQueryService(self.db).get_job(content["job_no"], lock=True)
        operation = SystemOperationCRUD(self.db).lock_by_operation_no(
            command.operation_no, object_type=OBJECT_RULEBOOK_GENERATION, object_no=job.job_no,
        )
        if operation is None or operation.status != "RUNNING":
            raise BusinessError("OPERATION_NOT_ACTIVE", "本次修改已结束或中断，未写入旧结果", status_code=409)
        if ((check_revision and job.revision != content["revision"]) or job.source_sha256 != content["source_sha256"]
                or content["prompt_version"] != ASSISTANT_PROMPT_VERSION):
            raise BusinessError("GENERATION_REVISION_CONFLICT", "本次修改依据的版本已变化，原文档保留，请重新发送要求", status_code=409)
        return job, operation

    def run(self, command: CommandHandle) -> dict:
        job, operation = self._current(command, check_revision=False)
        messages = GenerationMessageCRUD(self.db)
        # 结果已经原子提交而后台终态尚未写入时，按保存事实恢复，不重复调用模型。
        completed = messages.for_operation(command.operation_id, "ASSISTANT")
        if completed is not None:
            operation.phase = PHASE_RESULT_READY
            return {"job_no": job.job_no, "revision": completed.revision, "message": completed.content}
        job, _ = self._current(command)
        previous = GenerationHistoryService(self.db).document_at(job, command.request["revision"])
        baseline = normalize_business_codes(previous)
        current_message = messages.for_operation(command.operation_id, "USER")
        if current_message is None:
            raise BusinessError("ASSISTANT_MESSAGE_MISSING", "本次修改缺少已保存的用户要求", status_code=409)
        history = messages.page(job.id, before_id=current_message.id, limit=12)
        conversation = [
            {"role": item.role, "content": item.content, "revision": item.revision, "operation_status": op.status}
            for item, op in reversed(history)
        ]
        workbook_ir = GenerationEditingService(self.db).source_workbook(job.job_no)
        workbook, source_rows = GenerationLlmService()._workbook_payload(workbook_ir)
        issue_answer = None
        if issue_id := command.request.get("issue_id"):
            issue = next((item for item in previous.issues if item.issue_id == issue_id), None)
            if issue is None:
                raise BusinessError("GENERATION_ISSUE_NOT_FOUND", "本次补充对应的事项已不存在，原版本保留", status_code=409)
            issue_answer = {**issue.model_dump(mode="json"), "answer": command.request["message"]}
        payload = {
            "document": previous.model_dump(mode="json"), "workbook": workbook,
            "business_background": job.business_background, "generation_requirements": job.generation_requirements,
            "conversation": conversation, "instruction": command.request["message"],
            "issue_answer": issue_answer,
            "response_schema": AssistantReply.model_json_schema(),
        }
        rollback_transaction(self.db)
        command.phase(PHASE_LLM_REQUEST)
        candidate = None
        reply = None
        edit_base = previous
        for attempt in range(2):
            response = call_json(
                payload, ASSISTANT_PROMPT,
                idempotency_key=f"{command.operation_no}:edit:{attempt}",
                reasoning_effort=get_settings().rulebook_llm_reasoning_effort,
            )
            try:
                reply = AssistantReply.model_validate(response)
                if issue_answer and reply.kind == "EXPLAIN":
                    raise BusinessError("ASSISTANT_ANSWER_NOT_APPLIED", "用户提交的是业务补充，请实际修改；依据不足时明确提问，不能只解释而不处理", status_code=502)
                if edit_base != previous and reply.kind != "EDIT":
                    raise BusinessError("ASSISTANT_INVALID_EDIT", "纠正候选稿必须返回修改结果，不能用解释跳过修复", status_code=502)
                candidate = apply_edits(edit_base, reply)
                candidate = GenerationDocument.model_validate(candidate.model_dump(mode="json"))
                problems = collect_document_problems(candidate, source_rows)
                if issue_answer and not any(item.issue_id == issue_answer["issue_id"] for item in candidate.issues):
                    problems.append({"kind": "issue.answer_target_removed", "owner": issue_answer["issue_id"]})
                if issue_answer and reply.kind == "EDIT" and normalize_business_codes(candidate) == baseline:
                    problems.append({"kind": "issue.answer_not_applied", "owner": issue_answer["issue_id"]})
                if issue_answer and reply.kind == "EDIT" and any(item.issue_id == issue_answer["issue_id"] and item.status == "RESOLVED"
                                        and not item.resolution.strip() for item in candidate.issues):
                    problems.append({"kind": "issue.answer_evidence_missing", "owner": issue_answer["issue_id"]})
                if problems:
                    raise BusinessError("ASSISTANT_INVALID_EDIT", summarize_generation_problems(problems), status_code=502)
                if normalize_business_codes(candidate) != baseline:
                    edit_base = candidate
                    candidate, review = self._review(
                        command, previous, candidate, workbook=workbook, source_rows=source_rows,
                        issue_answer=issue_answer, conversation=conversation, attempt=attempt,
                    )
                    if review.problems:
                        # 结构已合法，语义纠正接着候选稿补齐；不能从原版重新应用补丁而丢失前轮修改。
                        edit_base = candidate
                        raise BusinessError("ASSISTANT_REVIEW_FAILED", "；".join(review.problems), status_code=502)
                break
            except (ValidationError, BusinessError) as exc:
                if isinstance(exc, BusinessError) and (not exc.code.startswith("ASSISTANT_") or exc.code == "ASSISTANT_REVIEW_INVALID"):
                    raise
                diagnostic = (
                    [{"field": ".".join(map(str, error["loc"])), "reason": error["type"]}
                     for error in exc.errors(include_input=False, include_context=False, include_url=False)]
                    if isinstance(exc, ValidationError) else [{"code": exc.code, "message": exc.message}]
                )
                logger.warning("助手修改校验失败 operation_no=%s attempt=%s problems=%s", command.operation_no, attempt, diagnostic)
                if attempt:
                    code = exc.code if isinstance(exc, BusinessError) else "ASSISTANT_INVALID_EDIT"
                    message = ("AI 修改后的内容仍未通过一致性复核，原版本保留。"
                               if code == "ASSISTANT_REVIEW_FAILED" else "AI 修改仍存在结构或来源问题，原版本保留。")
                    raise BusinessError(code, message + "可在助手中重试这条要求。", status_code=502,
                                        details={"problems": diagnostic}) from exc
                payload["document"] = edit_base.model_dump(mode="json")
                payload["correction"] = {
                    "mode": "REPAIR_CANDIDATE" if edit_base != previous else "REPLACE_INVALID_EDITS",
                    "instruction": ("document 已包含前轮有效修改；保留它们，只补充修复以下问题，不重放已完成操作。"
                                    if edit_base != previous else "前轮操作未通过校验，document 未应用这些操作；重新给出完整有效修改。"),
                    "problems": diagnostic,
                }
                if edit_base == previous:
                    payload["previous_response"] = response
                else:
                    # 候选稿已包含有效修改，不再重复发送前轮补丁，避免重放删除或新增。
                    payload.pop("previous_response", None)
        changed = normalize_business_codes(candidate) != baseline
        job, operation = self._current(command)
        if changed:
            job.revision += 1
            job.document_json = candidate.model_dump(mode="json")
            summary = self._summary(previous, candidate)
            GenerationHistoryService(self.db).snapshot(job, origin="AI", summary=summary, operation_id=command.operation_id)
            answer = summary + "。" + reply.message
        else:
            answer = reply.message if reply.kind != "EDIT" else "当前内容无需修改，已保留原版本。"
        messages.add(RulebookGenerationMessage(
            generation_job_id=job.id, revision=job.revision, operation_id=command.operation_id,
            role="ASSISTANT", content=answer,
        ))
        result = {"job_no": job.job_no, "revision": job.revision, "changed": changed, "reply_kind": reply.kind, "message": answer}
        operation.phase = PHASE_RESULT_READY
        commit_transaction(self.db)
        return result

    def _review(self, command, previous, candidate, *, workbook, source_rows, issue_answer, conversation, attempt):
        for repair_round in range(2):
            payload = {"before": previous.model_dump(mode="json"), "changes": document_changes(previous, candidate),
                       "instruction": command.request["message"], "workbook": workbook, "issue_answer": issue_answer,
                       "conversation": conversation, "response_schema": AssistantReview.model_json_schema()}
            for correction in range(2):
                response = call_json(
                    payload, ASSISTANT_REVIEW_PROMPT,
                    idempotency_key=f"{command.operation_no}:review:{attempt}:{repair_round}" + (":correction" if correction else ""),
                    reasoning_effort=get_settings().rulebook_llm_reasoning_effort,
                )
                try:
                    review = AssistantReview.model_validate(response)
                    if not review.text_repairs or repair_round:
                        # 修补后的文档也必须独立复核；不能以提供了修补操作冒充通过。
                        if review.text_repairs and not review.problems:
                            raise BusinessError("ASSISTANT_INVALID_REPAIR", "复核同时返回了通过与未完成修补", status_code=502)
                        return candidate, review
                    if not review.problems:
                        raise BusinessError("ASSISTANT_INVALID_REPAIR", "复核修补缺少对应问题", status_code=502)
                    repaired = apply_review_text_repairs(previous, candidate, review.text_repairs)
                    problems = collect_document_problems(repaired, source_rows)
                    if problems:
                        raise BusinessError("ASSISTANT_INVALID_REPAIR", summarize_generation_problems(problems), status_code=502)
                    break
                except (ValidationError, BusinessError) as exc:
                    if isinstance(exc, BusinessError) and exc.code != "ASSISTANT_INVALID_REPAIR":
                        raise
                    diagnostic = (exc.errors(include_input=False, include_context=False, include_url=False)
                                  if isinstance(exc, ValidationError) else [{"code": exc.code, "message": exc.message}])
                    if correction:
                        raise BusinessError("ASSISTANT_REVIEW_INVALID", "AI 复核响应纠正后仍无效，原版本保留。可重试这条要求。",
                                            status_code=502, details={"problems": diagnostic}) from exc
                    payload["previous_response"] = response
                    payload["correction"] = {"instruction": "只纠正本次复核响应及修补操作，不重新编写候选稿。", "problems": diagnostic}
            if repaired == candidate:
                return candidate, review
            logger.info("助手同步说明 operation_no=%s attempt=%s count=%s",
                        command.operation_no, attempt, len(review.text_repairs))
            candidate = repaired
        raise AssertionError("复核未返回结论")

    def _summary(self, previous: GenerationDocument, current: GenerationDocument) -> str:
        previous = normalize_business_codes(previous)
        old = {row.row_id: row.model_dump() for sheet in previous.sheets for row in sheet.rows}
        new = {row.row_id: row.model_dump() for sheet in current.sheets for row in sheet.rows}
        added = len(new.keys() - old.keys())
        removed = len(old.keys() - new.keys())
        updated = sum(old[key]["cells"] != new[key]["cells"] for key in old.keys() & new.keys())
        organized = sum(any(old[key][field] != new[key][field] for field in ("business_code", "entry_kind", "related_row_ids"))
                        for key in old.keys() & new.keys())
        parts = []
        for count, label in ((added, "新增"), (updated, "修改"), (removed, "删除")):
            if count:
                parts.append(f"{label} {count} 条")
        if organized:
            parts.append(f"整理 {organized} 条编号或关联")
        old_issues = {item.issue_id: item for item in previous.issues}
        new_issues = {item.issue_id: item for item in current.issues}
        issues = sum(old_issues.get(key) != new_issues.get(key) for key in old_issues.keys() | new_issues.keys())
        if issues:
            parts.append(f"更新 {issues} 项事项")
        old_sheets = {sheet.sheet_id: {**sheet.model_dump(exclude={"source", "rows"}), "row_order": [row.row_id for row in sheet.rows]}
                      for sheet in previous.sheets}
        new_sheets = {sheet.sheet_id: {**sheet.model_dump(exclude={"source", "rows"}), "row_order": [row.row_id for row in sheet.rows]}
                      for sheet in current.sheets}
        adjusted = sum(old_sheets.get(key) != new_sheets.get(key) for key in old_sheets.keys() | new_sheets.keys())
        if adjusted:
            parts.append(f"调整 {adjusted} 张表")
        return "已保存新版本：" + "，".join(parts or ["更新文档说明"])
