from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """应用使用带时区时间；MySQL DATETIME(6) 固定保存 UTC，不含时区。"""

    impl = MySQLDateTime(fsp=6)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("UTCDateTime 写入值必须包含时区")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class ConfigurationShape(DeclarativeBase):
    """编辑器 JSON 属性形状；不属于数据库 Base，不创建或读写明细表。

    services.shared.configuration 只使用列名和默认值构造 JSON，实例不会加入 Session。
    """


class TimestampMixin:
    # 数据库会话时区固定 UTC（见 core/database.py），CURRENT_TIMESTAMP 因此写入 UTC；
    # 读取时统一按 UTC 解释，前端无需再猜时区。
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )

