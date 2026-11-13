"""字段抽取的产物预读：调用模型前按规则声明读取真实产物。

抽取规则已经知道材料与产物类型，不需要模型先决定读什么。预读与 TASK_DATA_READ 共用
`read_artifact_items`，不复制取数逻辑；同一批次内相同材料、相同产物只读取一次，由该批
字段共享。预读内容随消息进入模型上下文，正常抽取路径不再要求首轮工具调用。
"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.configuration.execution_mode import MODE_EXTRACT
from app.modules.audit.execution.service.flow.shared import agent_module
from app.modules.audit.field.service.tools.handlers import read_artifact_items
from app.modules.audit.shared import _scenario
from app.modules.audit.task.model import AuditTask

# 材料未上传或产物未就绪是既有业务状态：把真实原因交给模型按证据不足处理。
# 其余读取错误（材料身份不一致、产物内容损坏等）照常上抛，不在这里收窄。
_UNAVAILABLE_CODES = frozenset({"TOOL_DOCUMENT_NOT_UPLOADED", "TOOL_ARTIFACT_NOT_READY"})


def extraction_artifact_requests(rule: dict[str, Any]) -> list[tuple[str, str]]:
    """字段抽取规则要预读的（材料，产物类型）清单，按声明顺序去重。

    抽取目标字段的声明已经给出材料与产物类型；有输入事实时也照常预读，
    因为输入是判断依据，不能替代被抽取字段所在的材料正文。
    """
    if str(rule.get("execution_mode") or "") != MODE_EXTRACT:
        return []
    if not rule.get("output_definitions"):
        return []
    requests: list[tuple[str, str]] = []
    for definition in rule.get("output_definitions") or []:
        if not isinstance(definition, dict):
            continue
        material_key = definition.get("material_key")
        if not material_key:
            continue
        artifact_type = "SEAL" if str(definition.get("artifact_source") or "").upper() == "SEAL" else "OCR"
        request = (str(material_key), artifact_type)
        if request not in requests:
            requests.append(request)
    return requests


def preload_extraction_artifacts(
    db: Session,
    task: AuditTask,
    *,
    node_code: str,
    rule_instance_id: str,
    material_refs: tuple[str, ...],
    requests: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """按清单读取当前上传的产物；同一（材料，产物）只读取一次。"""
    if not requests:
        return []
    agent = agent_module().AuditRuleAgentService(db)
    context = agent.execution_context(
        task=task,
        scene_code=_scenario(db, task).scenario_code,
        node_code=node_code,
        rule_instance_id=rule_instance_id,
        material_refs=material_refs,
    )
    items: list[dict[str, Any]] = []
    for material_key, artifact_type in requests:
        try:
            items.extend(read_artifact_items(
                context, material_keys=[material_key], artifact_type=artifact_type,
            ))
        except BusinessError as exc:
            if exc.code not in _UNAVAILABLE_CODES:
                raise
            items.append({
                "material_key": material_key,
                "artifact_type": artifact_type,
                "artifact_id": None,
                "upload_id": None,
                "content": None,
                "unavailable": {"code": exc.code, "message": exc.message, "details": exc.details},
            })
    return items
