"""CRUD 基类：绑定一个模型与一个会话，子类只写本实体的查询。

普通请求与后台执行编排管理事务；CRUD 只查询、写入和 flush。
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError


class CRUDBase[ModelType]:
    """单模型 CRUD；子类通过 model 指定实体。"""

    model: type[ModelType]

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, id: int) -> ModelType | None:
        return self.db.get(self.model, id)

    def get_of(self, model: type, id: int):
        """按主键读取任意实体；service 需要跨实体读取时走这里。"""
        return self.db.get(model, id)

    def get_or_404(self, id: int, message: str = "数据不存在", code: str = "NOT_FOUND") -> ModelType:
        obj = self.get(id)
        if obj is None:
            raise BusinessError(code, message, status_code=404, details={"id": id})
        return obj

    def count(self, *conditions: Any) -> int:
        statement = select(func.count()).select_from(self.model)
        if conditions:
            statement = statement.where(*conditions)
        return int(self.db.scalar(statement) or 0)

    def list(
        self,
        *conditions: Any,
        order_by: Any = None,
        limit: int | None = None,
    ) -> Sequence[ModelType]:
        statement = select(self.model)
        if conditions:
            statement = statement.where(*conditions)
        if order_by is not None:
            statement = statement.order_by(order_by)
        if limit is not None:
            statement = statement.limit(limit)
        return self.db.scalars(statement).all()

    def add(self, obj: ModelType) -> ModelType:
        self.db.add(obj)
        self.db.flush()
        return obj

    def attach(self, obj: ModelType) -> ModelType:
        """只加入会话，不立即 flush；调用方随后自行查询或提交。"""
        self.db.add(obj)
        return obj

    def flush(self) -> None:
        self.db.flush()

    def refresh(self, obj: ModelType) -> None:
        self.db.refresh(obj)

    def delete(self, obj: ModelType) -> None:
        self.db.delete(obj)
        self.db.flush()


class SessionCRUD(CRUDBase):
    """通用会话网关：没有固定实体的 shared 工具用它执行按主键读取。"""
