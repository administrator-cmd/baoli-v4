"""执行结果与结果是否仍生效分别记录；失效不抹掉原执行结局。"""

from datetime import UTC, datetime

from sqlalchemy import select

from ...entity.tasks import NodeExecution


def invalidate_executions(db, *conditions, reason: str) -> int:
    records = list(db.scalars(select(NodeExecution).where(*conditions, NodeExecution.status != "INVALIDATED")))
    now = datetime.now(UTC)
    for execution in records:
        execution.invalidated_from = execution.status
        execution.invalidated_at = now
        execution.invalidation_reason = reason
        execution.status = "INVALIDATED"
    db.flush()
    return len(records)


def execution_status_view(status: str, invalidated_from: str | None) -> dict[str, object]:
    """展示原状态并标明是否已失效；失效原因由列单独返回。"""
    return {"status": invalidated_from or status, "invalidated": status == "INVALIDATED"}
