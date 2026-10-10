"""规则库模型原始返回：先保存文件，再独立提交操作记录，不受业务校验回滚影响。"""

import logging
from uuid import uuid4

from sqlalchemy import select

from ..common.exceptions import BusinessError
from ..database import SessionLocal
from ..entity import BackgroundOperation
from .storage_service import resolve_storage_path


logger = logging.getLogger(__name__)


def save_raw_response(operation_no: str, content: str) -> None:
    """保存原始正文，不重新序列化；无效 JSON 也保留，供失败排查。"""
    with SessionLocal() as session:
        operation = session.scalar(
            select(BackgroundOperation).where(BackgroundOperation.operation_no == operation_no)
        )
        if operation is None:
            raise BusinessError(
                "OPERATION_NOT_FOUND", "保存模型返回时找不到对应后台操作", status_code=500,
                details={"operation_no": operation_no},
            )
        relative_path = f"llm_responses/{operation_no}/response_{uuid4().hex}.json"
        target = resolve_storage_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="") as output:
            output.write(content)
        # 文件先写完；数据库写入失败时，日志仍能定位已保存的原文，异常正常向外传播。
        logger.info("规则库模型原始返回已保存 operation_no=%s path=%s", operation_no, relative_path)
        operation.raw_response_path = relative_path
        session.commit()
