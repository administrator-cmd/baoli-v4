"""规则库两轮模型原始返回：分阶段保存文件，独立提交索引，不受解析回滚影响。"""

import logging
from uuid import uuid4

from app.common.exceptions import BusinessError
from app.core.database import SessionLocal
from app.core.transactions import commit_transaction
from app.modules.system.operation.crud import SystemOperationCRUD
from app.services.shared.storage import resolve_storage_path

logger = logging.getLogger(__name__)


def save_raw_response(operation_no: str, content: str, *, stage: str) -> None:
    """保存原始正文，不重新序列化；无效 JSON 也保留，供失败排查。"""
    with SessionLocal() as session:
        operation = SystemOperationCRUD(session).by_operation_no(operation_no)
        if operation is None:
            raise BusinessError(
                "OPERATION_NOT_FOUND", "保存模型返回时找不到对应后台操作", status_code=500,
                details={"operation_no": operation_no},
            )
        relative_path = f"llm_responses/{operation_no}/response_{stage}_{uuid4().hex}.json"
        target = resolve_storage_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="") as output:
            output.write(content)
        # 文件先写完；数据库写入失败时，日志仍能定位已保存的原文，异常正常向外传播。
        logger.info("规则库模型原始返回已保存 operation_no=%s stage=%s path=%s", operation_no, stage, relative_path)
        operation.raw_response_path = relative_path
        commit_transaction(session)
