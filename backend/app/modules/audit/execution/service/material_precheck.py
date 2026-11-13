"""在外部材料处理之前，由现有 LLM/工具循环执行原文件质量规则。"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.status import AUDIT_LLM_CHANNEL_UNAVAILABLE
from app.core.transactions import commit_transaction
from app.domain.audit_state import RuleOutcome
from app.domain.execution_mode import MODE_MATERIAL_CHECK
from app.domain.llm_failure import channel_failure_detail, is_channel_failure
from app.domain.material_precheck import (
    PRECHECK_KEY,
    PRECHECK_PLANNING_PROMPT,
    PRECHECK_TOOLS,
    MaterialPrecheckPlan,
)
from app.integrations.llm.gateway import RequestLogContext, call_json
from app.modules.audit.execution.crud import AuditExecutionCRUD
from app.modules.audit.execution.service.flow.execution import execution_in_progress
from app.modules.audit.execution.service.flow.plan import evaluation_plan, rule_finished
from app.modules.audit.field.service.value_store import RECORD_RULE_RESULT, AuditFieldValueStoreService
from app.modules.audit.rule.crud import AuditRuleCRUD
from app.modules.audit.shared import _current_uploads
from app.modules.audit.task.service.lookup import AuditTaskLookupService


class AuditMaterialPrecheckService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def current_state(self, task) -> dict[str, Any] | None:
        execution = AuditExecutionCRUD(self.db).latest_for_task(task.id, task.current_node)
        state = (execution.result_json or {}).get(PRECHECK_KEY) if execution is not None else None
        return state if state is not None and state["input_revision"] == task.input_revision else None

    def retain_after_field_review(self, task, execution, *, previous_revision: int) -> None:
        """持有任务锁的字段核对事务只改事实，沿用同批次原文件的质量结论。

        只承接修改前仍有效的 READY 状态；材料替换或其他输入变化造成的旧状态不放行。
        """
        state = (execution.result_json or {}).get(PRECHECK_KEY)
        if (task.status != "WAITING_FIELD_REVIEW" or execution.status != "WAITING_FIELD_REVIEW"
                or execution.audit_task_id != task.id or execution.node_code != task.current_node
                or task.input_revision != previous_revision + 1
                or state is None or state["status"] != "READY" or state["input_revision"] != previous_revision):
            return
        execution.result_json = {**execution.result_json, PRECHECK_KEY: {**state, "input_revision": task.input_revision}}

    def assert_processing_allowed(self, task, upload) -> None:
        state = self.current_state(task)
        if state is None or state["status"] != "READY":
            raise BusinessError("MATERIAL_PRECHECK_REQUIRED", "材料质量前置检查尚未完成，不能开始材料处理", status_code=409)
        for item in state["blocked_materials"].values():
            if item["upload_id"] == upload.id:
                raise BusinessError("MATERIAL_QUALITY_BLOCKED", "该文件质量检查未通过，已停止后续处理", status_code=409,
                                    details={"material": item})

    def run(self, task_no: str, execution_id: int) -> dict[str, Any]:
        from app.graphs.audit_rule_runner import run_rule_evaluation
        from app.modules.audit.execution.service.flow.runner import audit_runner
        from app.modules.audit.rule.service.agent import AuditRuleAgentService

        task = AuditTaskLookupService(self.db).task_or_404(task_no, lock=True)
        execution = execution_in_progress(self.db, task, execution_id)
        plan = evaluation_plan(task, task.current_node)
        candidates = [entry for entry in plan if entry["rule"].get("execution_mode") == MODE_MATERIAL_CHECK]
        state = (execution.result_json or {}).get(PRECHECK_KEY)
        if state is None:
            revision = task.input_revision
            payload = {
                "rules": [AuditRuleAgentService()._without_origin(entry["rule"]) for entry in candidates],
                "original_file_tools": AuditRuleAgentService().tool_definitions(
                    [{"tool_id": key} for key in sorted(PRECHECK_TOOLS)]),
                "response_schema": MaterialPrecheckPlan.model_json_schema(),
            }
            commit_transaction(self.db)
            if candidates:
                try:
                    response = call_json(
                        payload, PRECHECK_PLANNING_PROMPT,
                        idempotency_key=f"material-precheck:{execution_id}:{revision}:{(execution.result_json or {})['attempt_no']}",
                        log_context=RequestLogContext(task_name=task.task_name, operation="材料质量检查时机识别"),
                    )
                except BusinessError as exc:
                    if not is_channel_failure(exc.code):
                        raise
                    failure = channel_failure_detail(exc.code, exc.message,
                        result_unknown=bool(exc.details.get("result_unknown")), details=exc.details)
                    execution.result_json = {**(execution.result_json or {}), "channel_failure": failure}
                    commit_transaction(self.db)
                    raise BusinessError(AUDIT_LLM_CHANNEL_UNAVAILABLE, exc.message, status_code=exc.status_code, details=failure) from exc
                try:
                    decisions = MaterialPrecheckPlan.model_validate(response).model_dump(mode="json")["decisions"]
                except ValidationError as exc:
                    raise BusinessError("MATERIAL_PRECHECK_PLAN_INVALID", "材料检查时机识别结果不符合契约", status_code=502,
                        details={"mismatches": exc.errors(include_url=False, include_input=False, include_context=False)}) from exc
            else:
                decisions = []
            keys = [item["rule_key"] for item in decisions]
            if len(keys) != len(set(keys)) or set(keys) != {entry["rule_key"] for entry in candidates}:
                raise BusinessError("MATERIAL_PRECHECK_PLAN_INVALID", "材料检查时机识别遗漏、重复或引用了不存在的规则", status_code=502)
            before = {item["rule_key"] for item in decisions if item["timing"] == "BEFORE_PROCESSING"}
            for entry in candidates:
                rule = entry["rule"]
                if entry["rule_key"] in before and any(rule.get(key) for key in (
                    "inputs", "outputs", "source_rule_key", "source_rule_keys", "source_rule_codes", "input_gaps", "dependency_error",
                )):
                    raise BusinessError("MATERIAL_PRECHECK_PLAN_INVALID", "前置材料检查仍依赖字段、产出或其他规则", status_code=502,
                        details={"rule_name": rule["name"]})
            task = AuditTaskLookupService(self.db).task_or_404(task_no, lock=True)
            if task.input_revision != revision:
                raise BusinessError("INPUT_REVISION_CONFLICT", "材料质量检查期间任务输入已更新", status_code=409)
            execution = execution_in_progress(self.db, task, execution_id)
            state = {"input_revision": revision, "decisions": decisions,
                     "rule_keys": [entry["rule_key"] for entry in candidates if entry["rule_key"] in before],
                     "blocked_materials": {}, "outcomes": [], "status": "RUNNING"}
            execution.result_json = {**(execution.result_json or {}), PRECHECK_KEY: state}
            commit_transaction(self.db)
        if state["input_revision"] != task.input_revision:
            raise BusinessError("INPUT_REVISION_CONFLICT", "前置质量检查属于旧材料输入，不能用于当前文件", status_code=409)

        selected = set(state["rule_keys"])
        saved = {item.rule_key: item for item in AuditRuleCRUD(self.db).results_for_execution(execution.id)}
        # 每次续跑重用已形成的质量结论，只补尚未完成项；全部检查结束后统一放行材料处理。
        for entry in plan:
            if entry["rule_key"] not in selected:
                continue
            record = saved.get(entry["rule_key"])
            if record is not None and rule_finished(record) and (record.result_json or {}).get(PRECHECK_KEY):
                continue
            outcome = run_rule_evaluation(self.db, audit_runner, task_no=task_no, node_code=task.current_node,
                execution_id=execution.id, rule_cursor=entry["cursor"],
                planned_rule={**entry["rule"], "material_precheck": True})
            if outcome.get("channel_failure") is not None:
                failure = outcome["channel_failure"]
                execution.result_json = {**(execution.result_json or {}), "channel_failure": failure}
                commit_transaction(self.db)
                raise BusinessError(AUDIT_LLM_CHANNEL_UNAVAILABLE, failure["message"], status_code=502, details=failure)

        saved = {item.rule_key: item for item in AuditRuleCRUD(self.db).results_for_execution(execution.id)}
        uploads = {document.material_key: (document, upload) for document, upload in _current_uploads(self.db, task)}
        blocked: dict[str, dict[str, Any]] = {}
        outcomes: list[RuleOutcome] = []
        for entry in plan:
            if entry["rule_key"] not in selected:
                continue
            record = saved[entry["rule_key"]]
            outcomes.append({"rule_key": record.rule_key, "rule_code": record.rule_code, "rule_name": record.rule_name,
                "status": record.status, "loop_status": record.loop_status, "loop_count": record.turn_count,
                "unresolved_items": list((record.result_json or {}).get("unresolved_items") or [])})
            value = AuditFieldValueStoreService(self.db).value_of(task, RECORD_RULE_RESULT, record.id)
            results = value.get("material_quality")
            if results is None:
                # 没有形成候选结论时保留无法判定，不把实际执行失败伪装为文件不合格。
                results = [{"material_key": key, "status": "EVIDENCE_INSUFFICIENT", "message": record.message}
                           for key in entry["rule"].get("materials") or uploads.keys()]
            for result in results:
                key = result["material_key"]
                document, upload = uploads[key]
                if upload is None or result["status"] in {"PASSED", "NOT_APPLICABLE"}:
                    continue
                item = blocked.setdefault(key, {"material_key": key, "material_name": document.material_name,
                    "filename": upload.original_filename, "upload_id": upload.id,
                    "status": result["status"], "reasons": []})
                if result["status"] == "ABNORMAL":
                    item["status"] = "ABNORMAL"
                item["reasons"].append({"rule_key": record.rule_key, "rule_name": record.rule_name, "message": result["message"]})
        state = {**state, "status": "READY", "blocked_materials": blocked, "outcomes": outcomes}
        execution.result_json = {**(execution.result_json or {}), PRECHECK_KEY: state}
        commit_transaction(self.db)
        return state
