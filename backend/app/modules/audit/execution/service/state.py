from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.status import TASK_PHASE_AWAITING_CONFIRMATION, TASK_PHASE_COMPLETED, TASK_STATUS_COMPLETED
from app.domain.audit.audit_vocab import is_type_unavailable
from app.modules.audit.shared import _scenario, _task_manifest
from app.modules.audit.task.model import AuditTask
from app.modules.material.document.model import DocumentUpload, TaskDocument
from app.modules.scenario.template.model import ScenarioVersion


def _material_states(
    pairs: list[tuple[TaskDocument, DocumentUpload | None]],
) -> dict[str, str | None]:
    """材料位的当前状态，供算子判断依赖依据是否有效。

    TYPE_REVIEW=类型待复核（尚未由人定案）；TYPE_REJECTED=人工已确认与材料位不符；
    PREPARED=文件处理完成；ONLY_UPLOADED=只有上传、尚未形成处理产物；未上传返回 None
    （缺输入由字段状态表达）。

    这两种状态传给算子的后果不同：TYPE_REJECTED 是人工确定结论，依赖它的检查缺少有效依据；
    TYPE_REVIEW 不是对材料内容的否定（识别结论来自已下线为仅供参考的自动识别），依赖它的检查
    照常按材料中的真实原文判定，类型本身另行作为待确认事项暴露。
    """
    states: dict[str, str | None] = {}
    for document, upload in pairs:
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
    """任务的当前阶段；完成状态由任务状态派生，不单独落库。"""
    if task.status == TASK_STATUS_COMPLETED:
        return TASK_PHASE_COMPLETED
    return TASK_PHASE_AWAITING_CONFIRMATION
