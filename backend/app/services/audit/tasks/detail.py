"""审核任务详情、规则与字段状态的只读视图组装。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....common.exceptions import BusinessError
from ....constants.execution import NODE_RULE_EXECUTION
from ....constants.status import EXECUTABLE_FIELD_STATUSES
from ....domain.manifest import enabled_extraction_field_keys
from ....domain.rule_library import active, execution_container_of, is_risk_rule
from ....entity.scenarios import ScenarioVersion
from ....entity.tasks import (
    AuditTask,
    DocumentUpload,
    NodeExecution,
    RuleResult,
    StoredArtifact,
    TaskDocument,
    TaskFieldValue,
)
from ...system.background import latest_operation, operation_view
from ...system.operation_types import OBJECT_AUDIT_TASK
from .. import value_store as task_value_service
from ..artifacts import _material_classify_content
from ..evidence import evidence_source_views, field_evidence, result_content_view, rule_evidence
from ..facts import _current_field_fact_pairs
from ..flow.lifecycle import incomplete_failure_message
from ..resume import resumable_execution
from .processing import material_artifacts_summary


def _execution_failure_message(execution: NodeExecution | None) -> str | None:
    """未完成批次按逐条结果汇总原因，旧记录也能显示清楚。"""
    if execution is None:
        return None
    if execution.failure_code == "AUDIT_RULE_EXECUTION_INCOMPLETE":
        return incomplete_failure_message(execution.result_json) or execution.failure_message
    return execution.failure_message


def _named_rule_result(value: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """把历史结果里的内部规则键替换成规则名再展示。

    组合汇总的来源规则在早期记录里只保存了 `rule_...` 键，页面无法据字面理解；
    名称取自任务冻结的 manifest，只影响展示，不改写已保存的结果。
    """
    def named(text: Any) -> Any:
        if not isinstance(text, str) or "rule_" not in text:
            return text
        for key, name in names.items():
            if key in text:
                text = text.replace(key, name)
        return text

    result = dict(value)
    basis = result.get("basis")
    if isinstance(basis, list):
        result["basis"] = [
            {**row, "label": named(row.get("label"))} if isinstance(row, dict) else row
            for row in basis
        ]
    missing = result.get("missing_items")
    if isinstance(missing, list):
        result["missing_items"] = [named(item) for item in missing]
    return result


def _task_display_structure(task: AuditTask) -> dict[str, Any]:
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


def _execution_skip_reason(execution: NodeExecution | None) -> str | None:
    """节点被显式跳过时给出原因（例如“当前场景未配置此类检查”）。"""
    if execution is None or not isinstance(execution.result_json, dict):
        return None
    reason = execution.result_json.get("skip_reason")
    return reason if isinstance(reason, str) and reason else None


def task_view(db: Session, task: AuditTask) -> dict[str, Any]:
    """任务详情视图：节点、材料位、字段、规则、执行范围与最近一次后台操作。"""
    scenario = db.get(ScenarioVersion, task.scenario_version_id)
    if not scenario:
        raise BusinessError("SCENARIO_VERSION_MISSING", "任务关联的场景版本不存在", status_code=500)
    manifest = task.manifest_json
    schema = task.task_schema_json
    nodes = sorted(manifest["nodes"], key=lambda item: item["sequence_no"])
    frozen_version_no = scenario.version_no
    executions = {
        item.node_code: item
        for item in db.scalars(
            select(NodeExecution)
            .where(NodeExecution.audit_task_id == task.id, NodeExecution.status != "INVALIDATED")
            .order_by(NodeExecution.id)
        ).all()
    }
    documents = db.scalars(select(TaskDocument).where(TaskDocument.audit_task_id == task.id).order_by(TaskDocument.id)).all()
    uploads = {
        item.id: item
        for item in db.scalars(
            # document_upload 与 task_document 之间有两条外键，必须显式给出连接条件。
            select(DocumentUpload)
            .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
            .where(TaskDocument.audit_task_id == task.id, DocumentUpload.is_current.is_(True))
        ).all()
    }
    # 材料识别来自该材料当前上传的真实产物，不依赖文件处理业务阶段。
    material_classify = {
        item.material_key: _material_classify_content(db, uploads[item.current_upload_id])
        for item in documents if item.current_upload_id in uploads
    }
    # 四项处理产物的状态一次汇总返回：页面据此显示识别进度并定位失败项，
    # 不再为每份材料发四个只读请求。
    artifact_summary = material_artifacts_summary(db, list(uploads))
    field_facts = db.scalars(
        select(TaskFieldValue)
        .join(DocumentUpload, DocumentUpload.id == TaskFieldValue.document_upload_id)
        .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
        .where(
            TaskFieldValue.audit_task_id == task.id,
            TaskFieldValue.scenario_version_id == task.scenario_version_id,
            TaskDocument.audit_task_id == task.id,
            # 只看当前上传版本的事实版本；是否当前修订由 is_current 判定（同字段可能有多版历史）。
            DocumentUpload.is_current.is_(True),
            TaskDocument.current_upload_id == TaskFieldValue.document_upload_id,
        )
        .order_by(TaskFieldValue.id)
    ).all()
    # 规则结果只取当前有效执行批次：非 INVALIDATED 口径之上，再按节点取最新批次。
    # 人工复核节点允许形成多批成功执行记录；任务当前页展示最新批是明确的页面口径，
    # 历史批次仍保留在执行记录中追溯。
    current_execution_ids = {item.id for item in executions.values()}
    rule_results = (
        db.scalars(
            select(RuleResult)
            .join(NodeExecution, NodeExecution.id == RuleResult.node_execution_id)
            .where(
                RuleResult.audit_task_id == task.id,
                NodeExecution.status != "INVALIDATED",
                RuleResult.node_execution_id.in_(current_execution_ids),
            )
            .order_by(RuleResult.id)
        ).all()
        if current_execution_ids
        else []
    )
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
    # 服务重启中断的批次可以原地继续；页面据此提供「继续执行」，不必整轮重跑。
    resumable = resumable_execution(task, current_execution)
    current_node = {
        **current_node_definition,
        "status": current_execution.status if current_execution else "CURRENT",
        "failure_message": _execution_failure_message(current_execution),
        "resumable_execution_id": resumable.id if resumable else None,
    }
    # 当前事实按字段+材料取 is_current 版本。
    current_field_pairs = _current_field_fact_pairs(db, task)
    current_field_facts = {key: pair[0] for key, pair in current_field_pairs.items()}
    current_task_facts = {key: pair[1] for key, pair in current_field_pairs.items()}
    # 取值在任务 CSV：字段原文/规范值与规则结论都按记录 id 取。
    field_value_of = {item.id: task_value_service.value_of(db, task, task_value_service.RECORD_TASK_FIELD_VALUE, item.id)
                      for item in field_facts}
    rule_value_of = {item.id: task_value_service.value_of(db, task, task_value_service.RECORD_RULE_RESULT, item.id)
                     for item in rule_results}
    current_fact_value_of = {item.id: task_value_service.value_of(db, task, task_value_service.RECORD_TASK_FACT, item.id)
                             for item in current_task_facts.values()}
    documents_by_material_key = {item.material_key: item for item in documents}
    # 证据可能引用历史上传与产物，它们不在本请求已加载的当前上传集合里。先按引用批量载入，
    # 后面逐条解析走会话身份映射，不再为每个证据项单独查询。
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
    if referenced_upload_ids:
        _ = db.scalars(select(DocumentUpload).where(DocumentUpload.id.in_(referenced_upload_ids))).all()
    if referenced_artifact_ids:
        _ = db.scalars(select(StoredArtifact).where(StoredArtifact.id.in_(referenced_artifact_ids))).all()
    # 原文及其来源随 CSV 业务记录保存，引用只解析到当时的字段/规则版本。
    rule_sources = {
        item.id: evidence_source_views(db, task, rule_evidence(db, task, item.id))
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
    for field in task.task_schema_json["fields"]:
        field_identity = (field["field_key"], field["material_key"])
        fact = current_field_facts.get(field_identity)
        task_fact = current_task_facts.get(field_identity)
        fact_is_current = fact is not None and fact.status != "NOT_EXECUTED"
        document = documents_by_material_key[field["material_key"]]
        field_sources = (
            evidence_source_views(db, task, field_evidence(db, task, fact.id))
            if fact_is_current else []
        )
        owner_code = extraction_owner_by_field.get(field["field_key"])
        owner_execution = executions.get(owner_code) if owner_code else None
        owner_finished = (
            owner_execution is not None and owner_execution.status == "SUCCEEDED"
            if owner_code
            else extraction_finished
        )
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
                "material_uploaded": document.current_upload_id is not None,
                "material_required": document.required,
                "fact_revision_no": fact.revision_no if fact_is_current else None,
                "origin": fact.origin if fact_is_current else None,
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
                "failure_message": _execution_failure_message(executions.get(node["node_code"])),
                "skip_reason": _execution_skip_reason(executions.get(node["node_code"])),
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
        "display": _task_display_structure(task),
        "documents": [
            {
                "document_no": item.document_no,
                # 材料键是材料位的稳定身份，不用中文名去匹配。
                "material_key": item.material_key,
                "material_name": item.material_name,
                "required": item.required,
                "status": item.status,
                "filename": uploads[item.current_upload_id].original_filename if item.current_upload_id in uploads else None,
                # 该材料位的 8018 识别结论（含置信度）：只作展示，不影响材料状态与后续执行。
                "material_classify": material_classify.get(item.material_key),
                # 当前上传的四项产物状态；没有产物时为空对象。
                "artifacts": artifact_summary.get(item.current_upload_id, {}),
            }
            for item in documents
        ],
        "fields": field_view,
        # 可主动纠错的字段事实清单（供页面提供纠错入口）。
        "field_correction_candidates": field_correction_candidates,
        "rules": [
            {
                "rule_key": item.rule_key,
                "name": item.rule_name,
                "source_ref": rule_definitions.get((execution_node_by_id[item.node_execution_id], item.rule_key), {}).get("source_ref"),
                "kind": rule_definitions.get((execution_node_by_id[item.node_execution_id], item.rule_key), {}).get("kind"),
                # 执行方式决定模型上下文与允许动作；旧快照没有该字段时前端显示未判定。
                "execution_mode": rule_definitions.get((execution_node_by_id[item.node_execution_id], item.rule_key), {}).get("execution_mode"),
                "is_risk_rule": is_risk_rule(rule_definitions.get((execution_node_by_id[item.node_execution_id], item.rule_key), {})),
                # 规则来源工作表：页面按它分组，不同场景使用各自的 Sheet 名。
                "source_sheet": (rule_definitions.get((execution_node_by_id[item.node_execution_id], item.rule_key), {}).get("origin") or {}).get("sheet_name"),
                "material_names": [
                    documents_by_material_key[key].material_name
                    for key in rule_definitions.get((execution_node_by_id[item.node_execution_id], item.rule_key), {}).get("materials") or []
                    if key in documents_by_material_key
                ],
                "status": item.status,
                "message": item.message,
                "turn_count": item.turn_count,
                "result": _named_rule_result({
                    # 诊断来自数据库，业务结论来自任务 CSV。
                    **rule_value_of[item.id],
                    **result_content_view(item.result_json if isinstance(item.result_json, dict) else {}),
                    # 规则结果的节点归属必须来自实际执行记录；node 使用能力类型供页面阶段筛选，
                    # node_code 保留用户配置的实例码，两个字段都不由规则名称推断。
                    "node": node_type_by_code.get(execution_node_by_id[item.node_execution_id]),
                    "node_type": node_type_by_code.get(execution_node_by_id[item.node_execution_id]),
                    "node_code": execution_node_by_id[item.node_execution_id],
                }, rule_name_by_key),
                "evidence": rule_sources[item.id],
                # 旧结果没有 standard 快照时，单独返回任务绑定配置的要求，不改写执行结果。
                "configured_requirement": rule_definitions.get(
                    (execution_node_by_id[item.node_execution_id], item.rule_key), {},
                ).get("requirement"),
            }
            for item in rule_results
        ],
        "latest_operation": operation_view(latest) if (latest := latest_operation(db, OBJECT_AUDIT_TASK, task.task_no)) else None,
    }
