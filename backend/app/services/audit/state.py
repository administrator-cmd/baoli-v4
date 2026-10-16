"""任务材料状态与执行进度；图恢复数据由 LangGraph 保存，不维护重复检查点表。"""


from ...domain.audit_vocab import is_type_unavailable
from ...domain.rule_library import is_risk_rule
from ...common.exceptions import BusinessError
from ...entity import AuditTask, DocumentUpload, NodeExecution, ScenarioVersion, TaskDocument, TaskFieldValue
from .shared import _current_uploads, _scenario, _task_manifest
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from typing import Any



def _current_node_executions(db: Session, task: AuditTask) -> dict[str, NodeExecution]:
    """每个节点的“当前有效执行批次”。

    每个节点只取最新一条未失效的执行。DAG 的 ``depends_on`` 才是执行依据，
    ``sequence_no`` 只用于展示和确定性排序；如果允许高序号节点先于低序号节点执行，
    按当前节点序号截断会漏掉真实的依赖结果。因此这里保留任务场景中的所有节点，
    再按节点实例码选最新批次。

    同一节点被重跑但旧执行尚未失效时也能取到最新批次。最新执行若停留在 RUNNING，
    仍按该节点没有可用终态结果处理；这是执行中断时的显式状态，不用旧批次冒充当前结果。
    """
    _, nodes, _ = _scenario_nodes(db, task)
    allowed = {item["node_code"] for item in nodes}
    latest: dict[str, NodeExecution] = {}
    for execution in db.scalars(
        select(NodeExecution)
        .where(NodeExecution.audit_task_id == task.id, NodeExecution.status != "INVALIDATED")
        .order_by(NodeExecution.id)
    ).all():
        if execution.node_code in allowed:
            latest[execution.node_code] = execution
    return latest


def _material_states(db: Session, task: AuditTask) -> dict[str, str | None]:
    """材料位的当前状态，供算子判断依赖依据是否有效。

    TYPE_REVIEW=类型待复核（尚未由人定案）；TYPE_REJECTED=人工已确认与材料位不符；
    PREPARED=文件处理完成；ONLY_UPLOADED=只有上传、尚未形成处理产物；未上传返回 None
    （缺输入由字段状态表达）。

    这两种状态传给算子的后果不同：TYPE_REJECTED 是人工确定结论，依赖它的检查缺少有效依据；
    TYPE_REVIEW 不是对材料内容的否定（识别结论来自已下线为仅供参考的自动识别），依赖它的检查
    照常按材料中的真实原文判定，类型本身另行作为待确认事项暴露。
    """
    states: dict[str, str | None] = {}
    for document, upload in _current_uploads(db, task):
        status = document.status
        if is_type_unavailable(status):
            states[document.material_key] = status
        elif upload is None:
            states[document.material_key] = None
        elif status == "PREPARED":
            states[document.material_key] = "PREPARED"
        else:
            states[document.material_key] = "ONLY_UPLOADED"
    return states

def _task_field_value_of_current_upload(db: Session, task: AuditTask, field_value_id: int) -> TaskFieldValue | None:
    """取当前上传版本的当前字段事实。

    待复核项必须绑定当前事实版本。只校验上传版本而不校验 ``is_current``，
    会让已经被重新抽取或人工纠错替换的旧事实仍可被提交，随后覆盖新事实。
    历史事实仍保留给审计读取，但不能再作为待办裁决的写入目标。
    """
    row = db.scalar(
        select(TaskFieldValue)
        .join(DocumentUpload, DocumentUpload.id == TaskFieldValue.document_upload_id)
        .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
        .where(
            TaskFieldValue.id == field_value_id,
            TaskFieldValue.audit_task_id == task.id,
            TaskFieldValue.scenario_version_id == task.scenario_version_id,
            TaskFieldValue.is_current.is_(True),
            TaskDocument.audit_task_id == task.id,
            TaskDocument.material_key == TaskFieldValue.material_key,
            DocumentUpload.audit_task_id == task.id,
            DocumentUpload.is_current.is_(True),
            TaskDocument.current_upload_id == TaskFieldValue.document_upload_id,
        )
    )
    return row


def _result_material_keys(record: Any) -> set[str]:
    """一条检查/风险结果实际涉及的材料 key。

    来源是执行时保存的输入摘要（每条输入都带 material_key），而不是规则声明的适用材料：
    规则可能适用于三种材料，但这次异常只发生在其中一种，报告按实际发生的材料筛选。
    文件质量检查没有字段输入，它的归属写在同一结构里的 `inputs` 摘要中
    （由 `_quality_material_inputs` 按逐份材料的判定结果生成）。Agent 工具的逐份判定
    保存于 tool_calls.per_material；存在性检查显式保存 checked_material_keys。
    """
    payload = record.result_json if isinstance(record.result_json, dict) else {}
    keys = {
        str(item.get("material_key"))
        for item in (payload.get("inputs") or [])
        if isinstance(item, dict) and item.get("material_key")
    }
    # 材料工具没有字段事实，不从配置猜归属；只读取执行结果实际检查过的材料。
    for result in [payload, *(payload.get("tool_calls") or [])]:
        keys.update(str(key) for key in result.get("checked_material_keys", []))
        keys.update(
            str(item["material_key"])
            for item in result.get("per_material", [])
            if item.get("material_key") and item.get("status") != "NOT_APPLICABLE"
        )
    if not keys:
        # 风险规则本身没有输入字段：它的材料归属只能来自当前有效的来源检查实例。
        # 只用规则声明的适用材料会把范围外材料算进来，也说明不了这条风险实际发生在哪份材料。
        keys = {
            str(item.get("material_key"))
            for item in (payload.get("source_instances") or [])
            if isinstance(item, dict) and item.get("material_key")
        }
    return keys








def _scenario_nodes(db: Session, task: AuditTask) -> tuple[ScenarioVersion, list[dict[str, Any]], int]:
    scenario = _scenario(db, task)
    nodes = sorted(_task_manifest(task)["nodes"], key=lambda item: item["sequence_no"])
    current_index = next((index for index, item in enumerate(nodes) if item["node_code"] == task.current_node), None)
    if current_index is None:
        raise BusinessError("AUDIT_NODE_NOT_IN_SCENARIO", "当前节点不属于任务场景", status_code=500)
    return scenario, nodes, current_index

def _task_checkpoint(task: AuditTask) -> dict[str, Any]:
    """任务的流程进度标记：当前节点、阶段与节点序号。

    它不承载图状态；执行不再有等待点，也不存在可恢复的图实例。
    """
    if not isinstance(task.checkpoint_json, dict):
        raise BusinessError("AUDIT_PROGRESS_MISSING", "任务缺少流程进度标记", status_code=409)
    return task.checkpoint_json


def _set_task_checkpoint(
    task: AuditTask,
    *,
    node_index: int,
    phase: str,
) -> None:
    task.checkpoint_json = {
        "node_index": node_index,
        "current_node": task.current_node,
        "phase": phase,
    }
