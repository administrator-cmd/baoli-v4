"""业务操作日志：记录真实发生的写操作，并与后台命令对账。

只记录已成功提交的同步写操作（CRUD、启停、删除恢复、发布、复制、上传、替换、移除、复核提交、
报告备注归档）；失败的尝试不写日志，避免把「尝试」显示成「已发生」。

后台长命令（节点执行、复核恢复、导入转换）的记录在 background_operation，这里不复制它的内容，
只在操作日志查询时按 command_no / 编号并列展示。同一对象同一操作类型的幂等标识唯一：
响应丢失后的重发先查历史记录，输入一致返回原结果，不一致明确冲突，不重复创建。"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.common.pagination import PageResult, page_result
from app.domain.shared.identity import public_no
from app.modules.system.operation import crud
from app.modules.system.operation.model import OperationLog

logger = logging.getLogger(__name__)



class OperationLogService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def normalized_request_hash(self, payload: dict[str, Any]) -> str:
        """请求内容的稳定哈希：幂等比对用，同一内容必得同一值。"""
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


    def find_by_idempotency(self, *,
        object_type: str,
        operation_type: str,
        idempotency_key: str,
        lock: bool = False,
    ) -> OperationLog | None:
        """按对象、操作类型与幂等标识查找原操作记录；没有则返回 None。"""
        return crud.SystemOperationCRUD(self.db).idempotency_record(
            object_type=object_type,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            lock=lock,
        )


    def require_idempotent_replay(self, *,
        object_type: str,
        operation_type: str,
        idempotency_key: str | None,
        request: dict[str, Any],
        lock: bool = False,
    ) -> tuple[OperationLog | None, str]:
        """幂等命令的统一入口。

        返回 (已有记录, 规范化请求摘要)。已有记录且输入一致时必须直接返回原结果；
        输入不一致时明确冲突。调用方在真正执行写操作后用同一摘要落库。
        """
        request_hash = self.normalized_request_hash(request)
        if not idempotency_key:
            return None, request_hash
        existing = self.find_by_idempotency(
            object_type=object_type,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            lock=lock,
        )
        if existing is None:
            return None, request_hash
        stored = existing.request_hash or self.normalized_request_hash(dict(existing.detail_json or {}))
        if stored != request_hash:
            raise BusinessError(
                "OPERATION_IDEMPOTENCY_CONFLICT",
                "同一幂等标识已用于不同的请求内容，拒绝复用已有操作",
                status_code=409,
                details={"operation_no": existing.log_no, "object_no": existing.object_no},
            )
        return existing, request_hash


    def record_operation(self, *,
        object_type: str,
        object_no: str,
        object_name: str,
        operation_type: str,
        detail: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        command_no: str | None = None,
        permanent: bool = False,
        duration_ms: int | None = None,
        result: str = "SUCCEEDED",
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> OperationLog:
        """在业务事务内追加一条操作日志；调用方负责随业务一起提交。

        `result` 默认 SUCCEEDED：绝大多数记录的是已经真实完成的写操作。
        幂等命令必须显式传 FAILED——失败结果不落库的话，同幂等标识的重发会以为
        「什么都没发生」而重跑一次，把一次失败变成一次假成功。
        """
        log = OperationLog(
            log_no=public_no("LOG"),
            object_type=object_type,
            object_no=object_no,
            object_name=object_name[:255],
            operation_type=operation_type,
            result=result,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            command_no=command_no,
            permanent=permanent,
            duration_ms=duration_ms,
            detail_json=detail,
            failure_code=failure_code,
            failure_message=failure_message,
        )
        crud.SystemOperationCRUD(self.db).attach(log)
        return log


    def list_operations_page(self, *,
        object_type: str | None,
        operation_type: str | None,
        result: str | None,
        keyword: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
    ) -> PageResult[dict[str, Any]]:
        """操作日志分页：业务写操作与后台命令并列展示，各自的编号独立可查。"""
        rows, total = crud.SystemOperationCRUD(self.db).operation_log_page_rows(
            object_type=object_type,
            operation_type=operation_type,
            result=result,
            keyword=keyword,
            created_from=created_from,
            created_to=created_to,
            page=page,
            page_size=page_size,
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            raw_duration = row.duration_value
            duration_ms = None
            if raw_duration is not None:
                # 业务日志以毫秒存，命令的列为微秒差；按来源统一换算成毫秒。
                duration_ms = int(raw_duration) // 1000 if row.source == "COMMAND" else int(raw_duration)
            items.append(
                {
                    "entry_no": row.entry_no,
                    "source": row.source,
                    "object_type": row.object_type,
                    "object_no": row.object_no,
                    "object_name": row.object_name,
                    "operation_type": row.operation_type,
                    "result": row.result,
                    "duration_ms": duration_ms,
                    "command_no": row.command_no,
                    "failure_code": row.failure_code,
                    "failure_message": row.failure_message,
                    "created_at": row.created_at,
                    "finished_at": row.finished_at,
                }
            )
        return page_result(items, total, page, page_size)
