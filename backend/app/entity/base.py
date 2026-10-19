from datetime import datetime

from sqlalchemy import text
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ConfigurationShape(DeclarativeBase):
    """编辑器 JSON 属性形状；不属于数据库 Base，不创建或读写明细表。

    repositories.shared.configuration 只使用列名和默认值构造 JSON，实例不会加入 Session。
    """


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        MySQLDateTime(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQLDateTime(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )

