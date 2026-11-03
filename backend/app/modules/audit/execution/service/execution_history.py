"""执行结果与结果是否仍生效分别记录；失效不抹掉原执行结局。"""

from sqlalchemy.orm import Session

from app.modules.audit.execution.crud import AuditExecutionCRUD


class AuditExecutionHistoryService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def invalidate_for_task(self, task_id: int, reason: str) -> int:
        return AuditExecutionCRUD(self.db).invalidate_for_task(task_id, reason)

    def invalidate_by_ids(self, execution_ids: list[int], reason: str) -> int:
        return AuditExecutionCRUD(self.db).invalidate_by_ids(execution_ids, reason)


    def execution_status_view(self, status: str, invalidated_from: str | None) -> dict[str, object]:
        """展示原状态并标明是否已失效；失效原因由列单独返回。"""
        return {"status": invalidated_from or status, "invalidated": status == "INVALIDATED"}
