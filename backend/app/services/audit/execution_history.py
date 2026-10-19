"""执行结果与结果是否仍生效分别记录；失效不抹掉原执行结局。"""

from datetime import UTC, datetime
from sqlalchemy import select

from ...entity import NodeExecution


def invalidate_executions(db, *conditions, reason: str) -> int:
    records = list(db.scalars(select(NodeExecution).where(*conditions, NodeExecution.status != "INVALIDATED")))
    for execution in records:
        execution.result_json = {**(execution.result_json or {}), "superseded_execution_status": execution.status,
                                 "invalidated_at": datetime.now(UTC).isoformat(), "invalidation_reason": reason}
        execution.status = "INVALIDATED"
    db.flush()
    return len(records)


def execution_status_view(status, result_json):
    original = (result_json or {}).get("superseded_execution_status") if status == "INVALIDATED" else None
    return {"status": original or status, "invalidated": status == "INVALIDATED"}
