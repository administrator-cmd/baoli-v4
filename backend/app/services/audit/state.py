"""任务材料状态与执行进度；图恢复数据由 LangGraph 保存，不维护重复检查点表。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...domain.audit_vocab import is_type_unavailable
from ...entity.scenarios import ScenarioVersion
from ...entity.tasks import AuditTask, NodeExecution
from .shared import _current_uploads, _scenario, _task_manifest


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



def _scenario_nodes(db: Session, task: AuditTask) -> tuple[ScenarioVersion, list[dict[str, Any]], int]:
    scenario = _scenario(db, task)
    nodes = sorted(_task_manifest(task)["nodes"], key=lambda item: item["sequence_no"])
    current_index = next((index for index, item in enumerate(nodes) if item["node_code"] == task.current_node), None)
    if current_index is None:
        raise BusinessError("AUDIT_NODE_NOT_IN_SCENARIO", "当前节点不属于任务场景", status_code=500)
    return scenario, nodes, current_index

def _task_phase(task: AuditTask) -> str:
    """任务的当前阶段；节点序号由 current_node 在冻结节点顺序中的位置算出。"""
    phase = task.current_phase
    if not isinstance(phase, str) or not phase:
        raise BusinessError("AUDIT_PROGRESS_MISSING", "任务缺少流程阶段标记", status_code=409)
    return phase


def _set_task_phase(task: AuditTask, phase: str) -> None:
    if not phase:
        raise BusinessError("AUDIT_PROGRESS_INVALID", "任务阶段不能为空", status_code=500)
    task.current_phase = phase
