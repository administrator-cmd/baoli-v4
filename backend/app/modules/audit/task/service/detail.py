"""审核任务详情、规则与字段状态的只读视图组装。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.execution import NODE_RULE_EXECUTION, ORIGIN_HUMAN_CORRECTION
from app.constants.status import EXECUTABLE_FIELD_STATUSES
from app.domain.execution_scope import result_state
from app.domain.manifest import enabled_extraction_field_keys
from app.domain.rule_library import active, execution_container_of
from app.modules.audit.artifact.service.artifacts import AuditArtifactArtifactsService
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.execution.service.execution_scope import current_executions, pending_rerun_by_execution
from app.modules.audit.execution.service.flow.lifecycle import incomplete_failure_message
from app.modules.audit.execution.service.flow.plan import evaluation_plan, field_review_boundary
from app.modules.audit.execution.service.resume import AuditExecutionResumeService
from app.modules.audit.field.service import value_store as task_value_service
from app.modules.audit.field.service.evidence import AuditFieldEvidenceService
from app.modules.audit.field.service.facts import AuditFieldFactsService
from app.modules.audit.field.service.field_review import AuditFieldReviewService
from app.modules.audit.rule.model import RuleResult
from app.modules.audit.shared import _current_uploads
from app.modules.audit.task import crud
from app.modules.audit.task.model import AuditTask
from app.modules.audit.task.service.processing import AuditTaskProcessingService
from app.modules.audit.task.service.rule_views import AuditTaskRuleViewsService
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_AUDIT_TASK


def _field_summary(
    field_view: list[dict[str, Any]], manifest: dict[str, Any],
) -> dict[str, Any]:
    """按材料、期间和单位汇总字段缺项，并保留受影响规则的展示顺序。"""
    rule_targets_by_field: dict[str, list[str]] = {}
    for rule in manifest.get("rules") or []:
        if not rule.get("enabled", True):
            continue
        name = str(rule.get("name") or rule.get("rule_key"))
        for entry in rule.get("inputs") or []:
            field_key = str(entry.get("field_key") or "")
            if field_key:
                rule_targets_by_field.setdefault(field_key, []).append(name)
    gap_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in field_view:
        if item.get("normalized_value") not in (None, ""):
            continue
        if item.get("status") == "NOT_APPLICABLE":
            continue
        key = (
            str(item.get("material_name") or ""),
            str(item.get("actual_period_scope") or (
                item.get("period_scope")
                if item.get("period_scope") not in (None, "", "NOT_APPLICABLE")
                else "未识别期间"
            )),
            str(item.get("actual_unit") or item.get("unit") or "单位未识别"),
        )
        group = gap_groups.setdefault(key, {
            "material_name": key[0], "period": key[1], "unit": key[2],
            "field_names": [], "affected_rules": [],
        })
        if item.get("name") not in group["field_names"]:
            group["field_names"].append(item.get("name"))
        for rule_name in rule_targets_by_field.get(str(item.get("field_key")), []):
            if rule_name not in group["affected_rules"]:
                group["affected_rules"].append(rule_name)
    field_summary = {
        "total": len(field_view),
        "valid": sum(1 for item in field_view if item.get("normalized_value") not in (None, "")),
        "missing": sum(
            1 for item in field_view
            if item.get("normalized_value") in (None, "")
            and item.get("status") not in {"NOT_EXECUTED", "PENDING", "NOT_APPLICABLE"}
        ),
        "not_executed": sum(1 for item in field_view if item.get("status") in {"NOT_EXECUTED", "PENDING"}),
        "gap_groups": list(gap_groups.values()),
    }
    return field_summary


class AuditTaskDetailService:
    """组装任务当前执行、材料、字段与规则的只读详情。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _execution_failure_message(self, execution: NodeExecution | None) -> str | None:
        """未完成批次按逐条结果汇总原因，旧记录也能显示清楚。"""
        if execution is None:
            return None
        if execution.failure_code == "AUDIT_RULE_EXECUTION_INCOMPLETE":
            return incomplete_failure_message(execution.result_json) or execution.failure_message
        return execution.failure_message


    def _task_display_structure(self, task: AuditTask) -> dict[str, Any]:
        """返回任务冻结的技术容器与字段，不补读旧快照。"""
        node_source = task.manifest_json["nodes"]
        return {
            "task_no": task.task_no,
            "nodes": [
                {
                    "node_code": str(node["node_code"]),
                    "node_type": node["node_type"],
                    "name": str(node.get("name") or node["node_code"]),
                    "sequence_no": int(node.get("sequence_no") or 0),
                    "depends_on": [str(code) for code in (node.get("depends_on") or [])],
                    "enabled": bool(node.get("enabled", True)),
                }
                for node in node_source
            ],
            "fields": [
                {
                    "field_key": str(field["field_key"]),
                    "name": str(field["name"]),
                    "value_type": str(field["value_type"]),
                    "unit": field.get("unit"),
                    "currency": field.get("currency"),
                    "material_key": str(field["material_key"]),
                }
                for field in task.task_schema_json["fields"]
            ],
        }


    def _execution_skip_reason(self, execution: NodeExecution | None) -> str | None:
        """节点被显式跳过时给出原因（例如“当前场景未配置此类检查”）。"""
        if execution is None or not isinstance(execution.result_json, dict):
            return None
        reason = execution.result_json.get("skip_reason")
        return reason if isinstance(reason, str) and reason else None


    def task_view(self, task: AuditTask) -> dict[str, Any]:
        """任务详情视图：节点、材料位、字段、规则、执行范围与最近一次后台操作。"""
        scenario = crud.AuditTaskCRUD(self.db).scenario_version(task.scenario_version_id)
        if not scenario:
            raise BusinessError("SCENARIO_VERSION_MISSING", "任务关联的场景版本不存在", status_code=500)
        manifest = task.manifest_json
        schema = task.task_schema_json
        nodes = sorted(manifest["nodes"], key=lambda item: item["sequence_no"])
        frozen_version_no = scenario.version_no
        # 当前批次只有一个口径：每节点最新未失效执行，见 services.audit.execution_scope。
        executions = current_executions(self.db, task)
        pairs = _current_uploads(self.db, task)
        documents = [document for document, _ in pairs]
        # 材料识别来自该材料当前上传的真实产物，不依赖文件处理业务阶段。
        current_by_document = {document.id: upload for document, upload in pairs if upload is not None}
        material_classify = AuditArtifactArtifactsService(self.db).material_classifications(
            [upload.id for upload in current_by_document.values()]
        )
        # 四项处理产物的状态一次汇总返回：页面据此显示识别进度并定位失败项，
        # 不再为每份材料发四个只读请求。
        artifact_summary = AuditTaskProcessingService(self.db).material_artifacts_summary(
             [upload.id for upload in current_by_document.values()]
        )
        # 规则结果只取当前有效执行批次：非 INVALIDATED 口径之上，再按节点取最新批次。
        # 人工复核节点允许形成多批成功执行记录；任务当前页展示最新批是明确的页面口径，
        # 历史批次仍保留在执行记录中追溯。
        current_execution_ids = {item.id for item in executions.values()}
        rule_results = crud.AuditTaskCRUD(self.db).rule_results(task.id, current_execution_ids)
        # 质量放行仍以当前文件的已保存结论为准；续跑范围不能将其抹成「尚未检查」。
        quality_rule_results = [item for item in rule_results if (item.result_json or {}).get("material_precheck")]
        # 待重跑的规则写着重跑前的旧结论，与风险、报告同一口径：它不是本轮结果，
        # 不能出现在任务详情里被当成已判定。
        pending_by_execution = pending_rerun_by_execution(self.db, current_execution_ids)
        rule_results = [
            item for item in rule_results
            if result_state(
                execution_id=item.node_execution_id,
                rule_key=item.rule_key,
                current_ids=current_execution_ids,
                pending_rerun_keys=pending_by_execution.get(item.node_execution_id, set()),
            ).current
        ]
        # 待办分两层，页面必须按本批提交：
        current_node_definition = next((item for item in nodes if item["node_code"] == task.current_node), None)
        if not current_node_definition:
            raise BusinessError("AUDIT_NODE_NOT_IN_SCENARIO", "任务当前节点不属于场景版本", status_code=500)
        # 冻结的规则快照：范围取自创建时写入的 task_schema_json，名称取自任务绑定的
        # 场景版本 manifest（不可变）。这里不读场景最新版本，避免让人以为任务会跟随升级。
        rule_name_by_key = {
            rule["rule_key"]: rule.get("name", rule["rule_key"]) for rule in manifest.get("rules", [])
        }
        scope_enabled_keys = list(task.task_schema_json["enabled_rule_keys"])
        scope_unenabled_keys = list(task.task_schema_json["unenabled_rule_keys"])
        current_execution = executions.get(current_node_definition["node_code"])
        from app.domain.material_precheck import material_blocks
        from app.modules.audit.task.service.processing import MATERIAL_ARTIFACT_TYPES, quality_block_message
        quality_blocks = material_blocks(current_execution.result_json if current_execution else None)
        precheck_state = (current_execution.result_json or {}).get("material_precheck") if current_execution else None
        if precheck_state is None or precheck_state["input_revision"] != task.input_revision:
            quality_blocks = {}
        quality_blocks = {key: block for key, block in quality_blocks.items()
                          if any(upload.id == block["upload_id"] for upload in current_by_document.values())}
        for block in quality_blocks.values():
            artifact_summary[block["upload_id"]] = {
                kind: {"status": "SKIPPED", "artifact_no": None, "deadline_at": None,
                       "message": quality_block_message(block)} for kind in MATERIAL_ARTIFACT_TYPES
            }
        # 执行计划在本请求内只编译一次：续跑范围、字段核对边界与页面数字都取自它。
        plan_entries = evaluation_plan(task, task.current_node, execution=current_execution)
        # 当前字段事实同样只配对一次：续跑范围、核对状态与字段视图共用这一份。
        current_field_pairs = AuditFieldFactsService(self.db).current_field_fact_pairs( task)
        # 能否原地继续、继续时保留多少项、重跑多少项，统一由 resume 模块判断；
        # 页面只展示服务端算出的范围，不自行推导依赖关系。
        resume = AuditExecutionResumeService(self.db).resume_state(
             task, current_execution, plan=plan_entries, pairs=current_field_pairs,
        )
        # 字段核对等待点的真实状态：本次计划是否有抽取段、当前字段版本是否已确认、
        # 还有哪些缺项、确认后还要执行多少条规则。页面据此显示入口，不自行判断。
        field_review_required = False
        after_review_rule_count = 0
        review_blocked_reason = None
        try:
            boundary = field_review_boundary(plan_entries, all_rules=manifest.get("rules") or [])
            field_review_required = boundary.required
            after_review_rule_count = len(boundary.after)
        except BusinessError as exc:
            # 抽取依赖了核对后的业务规则时，这里是唯一能说清原因的地方；
            # 其他错误照常上抛，不把配置问题伪装成「暂无核对」。
            if exc.code != "AUDIT_FIELD_REVIEW_DEPENDENCY_CONFLICT":
                raise
            review_blocked_reason = exc.message
        field_review = AuditFieldReviewService(self.db).confirmation_view(
             task, current_execution,
            required=field_review_required, after_review_rule_count=after_review_rule_count,
            pairs=current_field_pairs,
        )
        field_review["blocked_reason"] = review_blocked_reason
        current_node = {
            **current_node_definition,
            "status": current_execution.status if current_execution else "CURRENT",
            "failure_code": current_execution.failure_code if current_execution else None,
            "failure_message": self._execution_failure_message(current_execution),
            "resumable_execution_id": resume["execution_id"],
            "resume": resume,
            "field_review": field_review,
        }
        # 当前事实按字段+材料取 is_current 版本。
        current_field_facts = {key: pair[0] for key, pair in current_field_pairs.items()}
        current_task_facts = {key: pair[1] for key, pair in current_field_pairs.items()}
        # 取值在任务 CSV：字段原文/规范值与规则结论都按记录 id 取。
        field_value_of = {item.id: task_value_service.AuditFieldValueStoreService(self.db).value_of(task, task_value_service.RECORD_TASK_FIELD_VALUE, item.id)
                          for item in current_field_facts.values()}
        rule_value_of = {item.id: task_value_service.AuditFieldValueStoreService(self.db).value_of(task, task_value_service.RECORD_RULE_RESULT, item.id)
                         for item in rule_results}
        quality_views = AuditTaskProcessingService(self.db).material_quality_views(
            task, pairs=pairs, execution=current_execution, plan=plan_entries,
            rule_results=quality_rule_results,
            rule_values={item.id: rule_value_of[item.id] if item.id in rule_value_of else
                         task_value_service.AuditFieldValueStoreService(self.db).value_of(
                             task, task_value_service.RECORD_RULE_RESULT, item.id)
                         for item in quality_rule_results},
        )
        current_fact_value_of = {item.id: task_value_service.AuditFieldValueStoreService(self.db).value_of(task, task_value_service.RECORD_TASK_FACT, item.id)
                                 for item in current_task_facts.values()}
        documents_by_material_key = {item.material_key: item for item in documents}
        # 收集证据引用的历史上传与产物，供后续批量预读。
        referenced_upload_ids: set[int] = set()
        referenced_artifact_ids: set[int] = set()
        for payload in (*field_value_of.values(), *rule_value_of.values()):
            for item in payload.get("evidence") or []:
                if not isinstance(item, dict):
                    continue
                upload_id = item.get("document_upload_id")
                if isinstance(upload_id, int) and not isinstance(upload_id, bool):
                    referenced_upload_ids.add(upload_id)
                artifact_id = item.get("artifact_id")
                if isinstance(artifact_id, int) and not isinstance(artifact_id, bool):
                    referenced_artifact_ids.add(artifact_id)
        # 同一请求内复用已展开的证据链，多个结果引用同一上游时不重复递归。
        memo = AuditFieldEvidenceService().evidence_memo()
        # 证据可能引用历史上传与产物，它们不在本请求已加载的当前上传集合里。先按引用批量载入，
        # 后面逐条解析走会话身份映射，不再为每个证据项单独查询。列表必须持有强引用：
        # 会话身份映射对未修改对象只保留弱引用，丢掉列表会让预读失效。
        _prefetched_uploads = crud.AuditTaskCRUD(self.db).uploads_by_ids(referenced_upload_ids)
        _prefetched_artifacts = crud.AuditTaskCRUD(self.db).artifacts_by_ids(referenced_artifact_ids)
        # 原文及其来源随 CSV 业务记录保存，引用只解析到当时的字段/规则版本。
        rule_sources = {
            item.id: AuditFieldEvidenceService(self.db).evidence_source_views( task, AuditFieldEvidenceService(self.db).rule_evidence( task, item.id, memo))
            for item in rule_results
        }
        rule_definitions = {(item["node"], item["rule_key"]): item for item in manifest["rules"]}
        execution_node_by_id = {item.id: item.node_code for item in executions.values()}
        node_type_by_code = {
            str(item["node_code"]): str(item.get("node_type") or item["node_code"])
            for item in nodes
        }
        extraction_codes = [execution_container_of(manifest)["node_code"]]
        extraction_finished = any(
            executions[code].status == "SUCCEEDED" for code in extraction_codes if code in executions
        )
        # manifest 把每个字段的抽取归属冻结在节点契约里。字段状态必须看自己的
        # 归属节点；只要任意一个抽取节点完成就把所有尚未抽取字段标成 NOT_EXECUTED，
        # 会把后续节点尚未开始的字段提前显示成已执行失败。
        extraction_owner_by_field: dict[str, str] = {}
        for node in manifest.get("nodes") or []:
            if str(node.get("node_type") or node.get("node_code")) != NODE_RULE_EXECUTION:
                continue
            contract = node.get("execution_contract")
            if not isinstance(contract, dict) or not isinstance(contract.get("field_keys"), list):
                continue
            for field_key in contract["field_keys"]:
                extraction_owner_by_field[str(field_key)] = str(node["node_code"])
        extraction_field_keys = enabled_extraction_field_keys(
            manifest, task.task_schema_json["enabled_rule_keys"],
        )
        # 抽取规则没有形成 TaskFieldValue 时，字段页仍要展示该抽取规则的真实结果与原因，
        # 不能把“模型无法抽取/材料不足”统一伪装成“尚未执行”。
        extraction_result_by_field: dict[str, RuleResult] = {}
        for result in rule_results:
            definition = rule_definitions.get((execution_node_by_id.get(result.node_execution_id), result.rule_key))
            if not definition or not definition.get("outputs"):
                continue
            for field_key in definition.get("outputs") or []:
                extraction_result_by_field.setdefault(str(field_key), result)
        field_view = []
        field_correction_candidates = []
        # 原抽取值、人工前后值与原文依据归属：核对页按字段展示，不把修正值当成抽取结果。
        supplements = AuditFieldReviewService(self.db).field_review_supplement( task, current_field_pairs)
        for field in task.task_schema_json["fields"]:
            field_identity = (field["field_key"], field["material_key"])
            fact = current_field_facts.get(field_identity)
            task_fact = current_task_facts.get(field_identity)
            fact_is_current = fact is not None and fact.status != "NOT_EXECUTED"
            document = documents_by_material_key[field["material_key"]]
            supplement = supplements.get(field_identity, {})
            # 人工修正值本身不带引文；原文依据仍指向它替代的那一版抽取结果。
            evidence_value_id = supplement.get("evidence_value_id") if fact_is_current else None
            field_sources = (
                AuditFieldEvidenceService(self.db).evidence_source_views( task, AuditFieldEvidenceService(self.db).field_evidence( task, evidence_value_id, memo))
                if evidence_value_id else []
            )
            owner_code = extraction_owner_by_field.get(field["field_key"])
            owner_execution = executions.get(owner_code) if owner_code else None
            owner_finished = (
                owner_execution is not None and owner_execution.status == "SUCCEEDED"
                if owner_code
                else extraction_finished
            )
            # 来源文件必须来自该结果的实际证据与上传版本，不能用材料位当前文件名顶替历史来源。
            source_files = list(dict.fromkeys(
                str(item["document_name"]) for item in field_sources if item.get("document_name")
            ))
            fact_payload = current_fact_value_of.get(task_fact.id, {}) if fact_is_current and task_fact else {}
            extraction_enabled = field["field_key"] in extraction_field_keys
            extraction_result = extraction_result_by_field.get(field["field_key"])
            status = task_fact.status if task_fact else (
                extraction_result.status if extraction_result is not None
                else ("NOT_EXECUTED" if owner_finished or not extraction_enabled else "PENDING")
            )
            # 当前事实行会保留待复核候选，供人工查看；任务字段结果里的“标准化结果”
            # 只展示可执行事实，避免把 EVIDENCE_UNCLEAR / NORMALIZATION_FAILED 的
            # 候选误显示成已经生效的值。
            normalized_value = (
                current_fact_value_of[task_fact.id].get("value")
                if fact_is_current and task_fact is not None and task_fact.status in EXECUTABLE_FIELD_STATUSES
                else None
            )
            field_view.append(
                {
                    # 字段键与材料键是主动纠错的定位依据；页面据此对某个字段事实发起纠错。
                    "field_key": field["field_key"],
                    "material_key": field["material_key"],
                    "source_ref": field.get("source_ref"),
                    "name": field["name"],
                    "material_name": document.material_name,
                    "value_type": field.get("value_type"),
                    "instruction": field.get("instruction"),
                    "status": status,
                    "extraction_enabled": extraction_enabled,
                    "raw_value": field_value_of[fact.id].get("raw_value") if fact_is_current else None,
                    "normalized_value": normalized_value,
                    "evidence": field_sources,
                    # 材料未上传时字段属于“缺输入”；不适用与否由规则自己的适用条件决定。
                    "material_uploaded": document.id in current_by_document,
                    "material_required": document.required,
                    "fact_revision_no": fact.revision_no if fact_is_current else None,
                    "origin": fact.origin if fact_is_current else None,
                    # 人工修正过的事实：页面必须能与原抽取结果分开显示。
                    "corrected": bool(fact_is_current and fact.origin == ORIGIN_HUMAN_CORRECTION),
                    "extracted_raw_value": supplement.get("extracted_raw_value") if fact_is_current else None,
                    "extracted_normalized_value": (
                        supplement.get("extracted_normalized_value") if fact_is_current else None
                    ),
                    "extracted_status": supplement.get("extracted_status") if fact_is_current else None,
                    "manual_review": supplement.get("manual_review") if fact_is_current else None,
                    # 已声明的口径：单位、币种、取数期间与报表口径都只显示配置里写明的值。
                    "unit": field.get("unit"),
                    "currency": field.get("currency"),
                    "period_scope": field.get("period_scope"),
                    "statement_scope": field.get("statement_scope"),
                    "entity_scope": field.get("entity_scope"),
                    # 模型从正文实际识别到的期间/口径/主体/单位；未识别时为空。
                    "actual_period_scope": fact_payload.get("actual_period_scope"),
                    "actual_statement_scope": fact_payload.get("actual_statement_scope"),
                    "actual_entity_scope": fact_payload.get("actual_entity_scope"),
                    "actual_unit": fact_payload.get("actual_unit"),
                    # 结果实际引用的上传文件；没有证据时为空，不用材料位名称冒充。
                    "source_files": source_files,
                    "source_file": source_files[0] if source_files else None,
                    "value_source": fact.origin if fact_is_current and fact else None,
                    "extraction_message": extraction_result.message if extraction_result is not None else None,
                    "extraction_presentation_version": rule_value_of[extraction_result.id].get("presentation_version") if extraction_result is not None else None,
                }
            )
            if fact_is_current:
                field_correction_candidates.append(
                    {
                        "field_key": field["field_key"],
                        "material_key": field["material_key"],
                        "field_name": field["name"],
                        "material_name": document.material_name,
                        "status": status,
                        "raw_value": field_value_of[fact.id].get("raw_value"),
                        "normalized_value": normalized_value,
                    }
                )
        field_summary = _field_summary(field_view, manifest)
        return {
            "task_no": task.task_no,
            "task_name": task.task_name,
            # 任务备注是可编辑的基本资料；执行快照不随重命名或备注修改变化。
            "remark": task.remark,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            # 任务创建时冻结的方案身份。
            "plan": (
                {
                    "plan_code": schema["plan_code"],
                    "plan_name": schema["plan_name"],
                    "scenario_version_no": frozen_version_no,
                }
                if task.plan_id is not None
                else None
            ),
            "scenario_code": scenario.scenario_code,
            "scenario_name": scenario.scenario_name,
            "version_no": frozen_version_no,
            "status": task.status,
            "current_node": current_node,
            # 任务输入修订号：携带旧修订的命令会被后端明确拒绝，页面必须据此刷新。
            "input_revision": task.input_revision,
            # 本任务实际执行范围：未配置 / 未启用 的区分依据。
            # 没有「自动补入」这一类：范围就是创建任务时勾选的那些规则。
            "scope": {
                "enabled_rule_keys": scope_enabled_keys,
                "unenabled_rule_keys": scope_unenabled_keys,
                "enabled_rules": [
                    {"rule_key": key, "name": rule_name_by_key.get(key, key)} for key in scope_enabled_keys
                ],
                "unenabled_rules": [
                    {"rule_key": key, "name": rule_name_by_key.get(key, key)} for key in scope_unenabled_keys
                ],
                "enabled_calculation_rules": [
                    {"rule_key": item["rule_code"], "name": item.get("rule_name") or item["rule_code"]}
                    for item in manifest.get("calculations", [])
                    if active(item)
                ],
            },
            "nodes": [
                {
                    **node,
                    "status": executions[node["node_code"]].status if node["node_code"] in executions else ("CURRENT" if node["node_code"] == task.current_node else "PENDING"),
                    "failure_code": executions[node["node_code"]].failure_code if node["node_code"] in executions else None,
                    "failure_message": self._execution_failure_message(executions.get(node["node_code"])),
                    "skip_reason": self._execution_skip_reason(executions.get(node["node_code"])),
                    # 真实子步骤（8010/OCR 处理数量、印章是否按需启用、未配置的文件质量规则）保存在节点结果里。
                    "result_summary": executions[node["node_code"]].result_json if node["node_code"] in executions else None,
                }
                for node in nodes
            ],
            # 当前场景没有配置这类检查；页面和执行都必须明确说明，不能显示成通过。
            # 两个口径分开：类型级说明「哪一类检查没配」，实例级让页面判断
            # 展示结构由当前任务的场景配置动态决定，前端按它通用渲染。
            # 页面不按场景名称或固定条件分支写死展示内容；这里给出本任务真实使用的
            # 节点、Sheet、字段及其数据，统一协议里未被本场景选中的内容不出现。
            "display": self._task_display_structure(task),
            "documents": [
                {
                    "document_no": item.document_no,
                    # 材料键是材料位的稳定身份，不用中文名去匹配。
                    "material_key": item.material_key,
                    "material_name": item.material_name,
                    "required": item.required,
                    "status": item.status,
                    "filename": current_by_document[item.id].original_filename if item.id in current_by_document else None,
                    # 当前上传时选择的 8018 材料类型；页面用它回显上传类型，不依赖识别产物是否已生成。
                    "declared_material_type": (
                        current_by_document[item.id].declared_material_type
                        if item.id in current_by_document else None
                    ),
                    # 该材料位的 8018 识别结论（含置信度）：只作展示，不影响材料状态与后续执行。
                    "material_classify": (
                        material_classify.get(current_by_document[item.id].id) if item.id in current_by_document else None
                    ),
                    "quality_precheck": quality_views[item.material_key],
                    # 当前上传的四项产物状态；没有产物时为空对象。
                    "artifacts": artifact_summary.get(current_by_document[item.id].id, {}) if item.id in current_by_document else {},
                }
                for item in documents
            ],
            "fields": field_view,
            # 字段结果统计与缺项汇总：页面点击统计直接筛选对应字段，不另算一套口径。
            "field_summary": field_summary,
            # 可主动纠错的字段事实清单（供页面提供纠错入口）。
            "field_correction_candidates": field_correction_candidates,
            "rules": [
                AuditTaskRuleViewsService(self.db).rule_result_view(
                    item,
                    task=task,
                    rule_definitions=rule_definitions,
                    execution_node_by_id=execution_node_by_id,
                    node_type_by_code=node_type_by_code,
                    documents_by_material_key=documents_by_material_key,
                    rule_name_by_key=rule_name_by_key,
                    rule_value_of=rule_value_of,
                    rule_sources=rule_sources,
                )
                for item in rule_results
            ],
            "latest_operation": OperationBackgroundService().operation_view(latest) if (latest := OperationBackgroundService(self.db).latest_operation( OBJECT_AUDIT_TASK, task.task_no)) else None,
        }
